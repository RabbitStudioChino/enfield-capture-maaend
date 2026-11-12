"""Isolated MaaFramework navigation and native same-frame photography."""

import hashlib
import importlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

from .task import check_path, read_json, write_json
from .validate import read_verified, require
from .win32_adapter import Win32Worker, WindowsAdapter, WorkerCancelled, release_game_inputs

SOURCE_ROOT = Path(__file__).resolve().parents[2]


class NativeTaskError(RuntimeError):
    def __init__(self, message, diagnostic):
        super().__init__(message)
        self.diagnostic = diagnostic


def native_status(status):
    return next((name for name in ("pending", "running", "succeeded", "failed") if getattr(status, name)), "unknown")


def native_install(workspace):
    workspace = check_path(workspace)
    candidates = (workspace / "local/runtime/capture-native", SOURCE_ROOT / "install", SOURCE_ROOT)
    for root in candidates:
        root = check_path(root)
        if not root.is_relative_to(workspace):
            continue
        if (root / "agent/cpp-algo.exe").is_file() and (root / "maafw/MaaFramework.dll").is_file():
            for relative in ("agent/cpp-algo.exe", "agent/go-service.exe", "maafw", "resource"):
                require(
                    check_path(root / relative).exists(), f"Native capture runtime is incomplete: {root / relative}"
                )
            return root
    raise ValueError(
        "Native Windows capture runtime is missing. Use the complete Windows capture package, "
        "or build with build-and-install --cpp-algo --ci and install the matching MaaFramework DLLs."
    )


def verify_resource_mirror(workspace, resources, install=None):
    workspace = check_path(workspace)
    install = check_path(install) if install is not None else native_install(workspace)
    mirrored = []
    for entry in resources:
        if not entry["uri"].startswith("assets/"):
            continue
        path = check_path(install / entry["uri"].removeprefix("assets/"))
        require(path.is_relative_to(install) and path.is_relative_to(workspace), "Runtime resource escapes workspace")
        read_verified(path, entry["sha256"], False)
        mirrored.append({"uri": path.relative_to(workspace).as_posix(), "sha256": entry["sha256"]})
    source_nogo = check_path(SOURCE_ROOT / "assets/data/MapNavigator/nogo_zones.json")
    runtime_nogo = check_path(install / "data/MapNavigator/nogo_zones.json")
    require(source_nogo.is_file() == runtime_nogo.is_file(), "Source and native NoGo resource existence differs")
    if source_nogo.is_file():
        digest = hashlib.sha256(source_nogo.read_bytes()).hexdigest()
        read_verified(runtime_nogo, digest, False)
        entry = {"uri": runtime_nogo.relative_to(workspace).as_posix(), "sha256": digest}
        if entry not in mirrored:
            mirrored.append(entry)
    return mirrored


class NavigationWorker(Win32Worker):
    worker_module = "tools.capture.navigation_worker"

    def __init__(self, run, workspace, query_only=False, install=None):
        install = check_path(install) if install is not None else native_install(workspace)
        write_json(
            Path(run) / ".worker/config.json",
            {"workspace": str(check_path(workspace)), "query_only": query_only, "install": str(install)},
        )
        self.bound_window = None
        super().__init__(run)

    def request(self, operation, timeout, should_cancel=None, **parameters):
        result = super().request(operation, timeout, should_cancel, **parameters)
        if operation == "initialize":
            self.bound_window = result.get("window")
        return result

    def close(self):
        forced = False
        try:
            if self.process.poll() is None:
                write_json(self.run / ".worker/stop.json", {"stop": True})
                with suppress(OSError, ValueError):
                    self.process.stdin.write(b'{"operation":"shutdown","sequence":0}\n')
                    self.process.stdin.flush()
                try:
                    self.process.wait(timeout=18)
                except subprocess.TimeoutExpired:
                    forced = True
            super().close()
        finally:
            report = self.run / ".worker/shutdown.json"
            clean = report.is_file() and read_json(report).get("ok")
            if forced or not clean:
                released = release_game_inputs(self.bound_window)
                write_json(self.run / ".worker/emergency-release.json", {"posted": released, "confirmed": False})
                raise RuntimeError(f"Navigation cleanup did not finish cleanly; inspect {self.run / 'logs/worker.log'}")


def query_navigation(workspace, operation, **params):
    workspace = check_path(workspace)
    install = native_install(workspace)
    for key in ("out_file", "navmesh_file"):
        if params.get(key):
            path = check_path(params[key])
            require(path.is_relative_to(workspace), "Navigation query path escapes workspace")
            params[key] = str(path)
    parent = workspace / "local/navigation-queries"
    check_path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    run = parent / uuid.uuid4().hex
    run.mkdir()
    for directory in ("logs", "images", "sdk", ".worker"):
        (run / directory).mkdir()
    worker = NavigationWorker(run, workspace, query_only=True, install=install)
    try:
        initialized = worker.request("initialize", timeout=75)
        require(initialized.get("connected"), initialized.get("error", "Native query initialization failed"))
        return worker.request("query", timeout=180, query_operation=operation, parameters=params)
    finally:
        worker.close()


class NavigationAdapter(WindowsAdapter):
    def __init__(self, run, config):
        super().__init__(run, input_enabled=True, activate_window=True)
        self.workspace = check_path(config["workspace"])
        self.query_only = config["query_only"]
        self.install = check_path(config["install"])
        require(self.install.is_relative_to(self.workspace), "Native runtime escapes workspace")
        self.session = None
        self.current_job = None
        self.last_target = None
        self.stop_requested = False
        self.sdk_log_configured = False
        os.environ.update(MAAFW_BINARY_PATH=str(self.install / "maafw"), PI_USER_DATA_DIR=str(run))

    def cancelled(self):
        return (self.run / ".worker/stop.json").exists()

    def stop_task(self):
        if self.session is not None and self.session.tasker is not None and not self.stop_requested:
            self.stop_requested = True
            self.session.tasker.post_stop()

    def task_failure(self, job, operation, entry, reason, timed_out=False):
        diagnostic = {"operation": operation, "task_entry": entry}
        report = {**diagnostic, "error": reason, "timed_out": timed_out, "nodes": []}
        if operation == "teleport":
            diagnostic["teleport_entry"] = report["teleport_entry"] = entry
        if timed_out:
            try:
                self.stop_task()
            except Exception as error:
                report["stop_error"] = str(error)
        try:
            report["job_id"] = job.job_id
            report["job_status"] = native_status(job.status)
            detail = self.session.tasker.get_task_detail(job.job_id)
            if detail is None:
                report["detail_error"] = "MaaFramework returned no task detail"
            else:
                report["task"] = {
                    "task_id": detail.task_id,
                    "entry": detail.entry,
                    "status": native_status(detail.status),
                    "node_id_list": detail.node_id_list,
                }
                # Keep the final reported nodes, without treating the last node as the failure cause.
                for node_id in detail.node_id_list[-8:]:
                    record = {"node_id": node_id}
                    report["nodes"].append(record)
                    try:
                        node = self.session.tasker.get_node_detail(node_id)
                        if node is None:
                            record["detail_error"] = "MaaFramework returned no node detail"
                            continue
                        record.update(name=node.name, completed=node.completed)
                        diagnostic["last_node"] = node.name
                        for kind, item in (("recognition", node.recognition), ("action", node.action)):
                            if item is None:
                                record[kind] = None
                                continue
                            raw = json.dumps(item.raw_detail, ensure_ascii=False)
                            record[kind] = {
                                "name": item.name,
                                "raw_detail_json": raw[:16384],
                                "raw_detail_truncated": len(raw) > 16384,
                            }
                            if kind == "recognition":
                                record[kind].update(reco_id=item.reco_id, algorithm=str(item.algorithm), hit=item.hit)
                            else:
                                record[kind].update(
                                    action_id=item.action_id, action=str(item.action), success=item.success
                                )
                    except Exception as error:
                        record["detail_error"] = f"{type(error).__name__}: {error}"
        except Exception as error:
            report["detail_error"] = f"{type(error).__name__}: {error}"
        try:
            report["logs"] = {
                "worker_uri": (self.run / "logs/worker.log").relative_to(self.workspace).as_posix(),
                "sdk_directory_uri": (self.run / "sdk").relative_to(self.workspace).as_posix(),
                "sdk_log_configured": self.sdk_log_configured,
                "agent_directory_uri": (self.run / "debug").relative_to(self.workspace).as_posix(),
            }
            directory = check_path(self.run / "diagnostics")
            directory.mkdir(exist_ok=True)
            path = directory / f"task-{job.job_id}-{uuid.uuid4().hex}.json"
            report["image"] = {"source": "controller.cached_image", "capture_time": "unknown", "status": "unavailable"}
            try:
                image = self.controller.cached_image
                if (
                    image is None
                    or str(image.dtype) != "uint8"
                    or image.ndim != 3
                    or image.shape[2] != 3
                    or image.size == 0
                ):
                    raise RuntimeError("The controller has no usable cached navigation screenshot")
                from PIL import Image

                height, width = image.shape[:2]
                target = check_path(path.with_suffix(".png"))
                with target.open("xb") as stream:
                    Image.fromarray(image[:, :, ::-1]).save(stream, format="PNG")
                report["image"].update(
                    status="saved",
                    uri=target.relative_to(self.workspace).as_posix(),
                    width=width,
                    height=height,
                    pixel_format="RGB8",
                    source_format="BGR8",
                    sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                    scaled_navigation_image=True,
                )
            except Exception as error:
                report["image"]["error"] = f"{type(error).__name__}: {error}"
            report["last_node"] = diagnostic.get("last_node")
            write_json(path, report)
            diagnostic["diagnostic_uri"] = path.relative_to(self.workspace).as_posix()
        except Exception as error:
            diagnostic["diagnostic_error"] = f"{type(error).__name__}: {error}"
        message = f"{reason}; task={entry}; last recorded node={diagnostic.get('last_node', 'unavailable')}"
        if diagnostic.get("diagnostic_uri"):
            message += f"; diagnostic={diagnostic['diagnostic_uri']}"
        else:
            message += f"; diagnostic unavailable: {diagnostic.get('diagnostic_error')}"
        return NativeTaskError(message, diagnostic)

    def wait_task(self, job, timeout, check_window=True, *, operation, entry):
        self.current_job = job
        deadline = time.monotonic() + timeout
        try:
            while not job.done:
                if self.cancelled():
                    self.stop_task()
                    raise WorkerCancelled("Navigation was stopped")
                if check_window and not self.query_only:
                    try:
                        self.check_window()
                    except Exception:
                        self.stop_task()
                        raise
                if self.session.agent_exit_code is not None:
                    raise RuntimeError(f"Native navigation agent exited with {self.session.agent_exit_code}")
                if time.monotonic() >= deadline:
                    raise self.task_failure(
                        job, operation, entry, "Native navigation operation exceeded its deadline", timed_out=True
                    )
                time.sleep(0.05)
            if not job.succeeded:
                raise self.task_failure(job, operation, entry, "Native navigation operation failed")
        finally:
            if job.done:
                self.current_job = None

    def open_session(self):
        sys.path.insert(0, str(SOURCE_ROOT / "tools/MapNavigator"))
        runtime = importlib.import_module("runtime")
        runtime.INSTALL_DIR = self.install
        runtime.AGENT_DIR = self.install / "agent"
        runtime.MAAFW_BIN_DIR = self.install / "maafw"
        runtime.CPP_AGENT_EXE = self.install / "agent/cpp-algo.exe"
        runtime.RESOURCE_DIR = self.install / "resource"
        runtime.configure_runtime_env()
        session_module = importlib.import_module("agent_session")
        sdk = runtime.load_maa_runtime()
        require(sdk is not None, "MaaFramework Python runtime is unavailable")
        with suppress(Exception):
            self.sdk_log_configured = sdk.Tasker.set_log_dir(check_path(self.run / "sdk"))
        adapter = self

        class GameConnector:
            def connect(self):
                return adapter.controller

            def attach_resource(self, resource):
                return None

        self.session = session_module.AgentSession(sdk)
        connector = importlib.import_module("navmesh_backend")._NullConnector() if self.query_only else GameConnector()
        self.session.open(
            connector,
            agent_name="WorldCapture",
            resource_dirs=[self.install / "resource"],
            aux_agents=[] if self.query_only else [self.install / "agent/go-service.exe"],
        )
        require(str(sdk.Library.version()).removeprefix("v") == "5.14.3", "Native runtime must use MaaFramework 5.14.3")

    def initialize(self):
        if self.cancelled():
            raise WorkerCancelled("Capture was stopped before initialization")
        if self.query_only:
            self.open_session()
            return {"connected": True, "query_only": True}
        connection = super().initialize()
        if not connection.get("connected"):
            return connection
        preflight = self.capture(0)
        require(
            preflight.get("image", {}).get("status") == "captured",
            preflight.get("error", "Preflight screenshot failed"),
        )
        require(self.controller.set_screenshot_use_raw_size(False), "Cannot restore navigation screenshot scaling")
        require(self.controller.set_screenshot_target_short_side(720), "Cannot select 720p navigation screenshots")
        self.open_session()
        clock = self.query("capture_clock", {})
        require(clock.get("ok"), clock.get("error", "Native capture clock is unavailable"))
        return {**connection, "preflight": preflight, "clock": clock["clock"]}

    def query(self, operation, parameters):
        node = "WorldCaptureQuery"
        self.session.resource.override_pipeline(
            {
                node: {
                    "recognition": "Custom",
                    "custom_recognition": "MapNavmeshQuery",
                    "custom_recognition_param": {"op": operation, **parameters},
                    "pre_delay": 0,
                    "post_delay": 0,
                    "rate_limit": 0,
                }
            }
        )
        job = self.session.tasker.post_task(node)
        self.wait_task(job, 170, check_window=not self.query_only, operation="query", entry=node)
        detail = self.session.tasker.get_task_detail(job.job_id)
        record = detail.nodes[0] if detail and detail.nodes else None
        raw = (
            record.recognition.best_result.detail
            if record and record.recognition and record.recognition.best_result
            else None
        )
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        require(isinstance(raw, dict), "Native navigation query returned no detail")
        return raw

    def run_node(self, name, body, timeout):
        self.session.resource.override_pipeline({name: body})
        self.wait_task(self.session.tasker.post_task(name), timeout, operation="navigate_capture", entry=name)

    def teleport(self, entry):
        require(isinstance(entry, str) and entry.startswith("SceneEnterWorld"), "Invalid teleport entry")
        self.wait_task(self.session.tasker.post_task(entry), 120, operation="teleport", entry=entry)
        self.last_target = None
        return {"ok": True, "teleport_entry": entry}

    def navigate_capture(self, request):
        self.check_window()
        target = request["waypoint"]["position_xy"]
        deck = request["waypoint"]["target_deck_y"]
        zone = request["zone_id"]
        key = (zone, *target, deck)
        if self.last_target != key:
            self.run_node(
                "WorldCaptureNavigate",
                {
                    "recognition": "DirectHit",
                    "action": "Custom",
                    "custom_action": "MapNavigateAction",
                    "custom_action_param": {
                        "map_name": zone,
                        "heading_source": "character",
                        "zip": False,
                        "enable_sprint": False,
                        "arrival_timeout": 90000,
                        "path": [{"action": "NAVMESH", "target": target, "target_deck_y": deck, "required": True}],
                    },
                    "pre_delay": 0,
                    "post_delay": 0,
                },
                180,
            )
        output = self.run / "views" / request["frame_id"]
        check_path(output)
        require(output.is_relative_to(self.run), "Invalid view output path")
        output.mkdir(parents=True)
        params = {
            "session_id": request["session_id"],
            "frame_id": request["frame_id"],
            "expected_zone": zone,
            "target": target,
            "yaw_degrees": request["waypoint"]["yaw_degrees"],
            "output_dir": str(output),
            "arrival_radius": 3,
            "timeout_ms": 30000,
        }
        failure = None
        failure_diagnostic = {}
        try:
            self.run_node(
                "WorldCaptureView",
                {
                    "recognition": "DirectHit",
                    "action": "Custom",
                    "custom_action": "CaptureViewAction",
                    "custom_action_param": params,
                    "pre_delay": 0,
                    "post_delay": 0,
                },
                45,
            )
        except (RuntimeError, ValueError, TimeoutError) as error:
            failure = str(error)
            if isinstance(error, NativeTaskError):
                failure_diagnostic = error.diagnostic
        require((output / "result.json").is_file(), failure or "Native photography produced no receipt")
        result = read_json(output / "result.json")
        result["receipt_uri"] = (output / "result.json").relative_to(self.run).as_posix()
        if failure and result.get("ok"):
            result.update(ok=False, error=failure)
        elif failure and failure_diagnostic:
            result["error"] = f"{result.get('error', 'Native photography failed')}; {failure}"
        result.update(failure_diagnostic)
        if result.get("ok"):
            self.last_target = key
            result["image"]["uri"] = (output / result["image"]["uri"]).relative_to(self.run).as_posix()
            image = check_path(self.run / result["image"]["uri"])
            result["image"]["sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
        return result

    def close(self):
        errors = []
        if self.session is not None:
            try:
                self.stop_task()
                deadline = time.monotonic() + 6
                while self.session.tasker and self.session.tasker.running and time.monotonic() < deadline:
                    time.sleep(0.05)
                require(
                    not self.session.tasker or not self.session.tasker.running,
                    "Native task did not release inputs before shutdown",
                )
            except Exception as error:
                errors.append(str(error))
            try:
                if self.controller is not None:
                    release_deadline = time.monotonic() + 2
                    for job in [
                        *(
                            self.controller.post_key_up(key)
                            for key in (ord("W"), ord("A"), ord("S"), ord("D"), ord("F"), 0x10, 0x11, 0x12, 0x20)
                        ),
                        self.controller.post_touch_up(1),
                    ]:
                        while not job.done and time.monotonic() < release_deadline:
                            time.sleep(0.025)
                        require(job.done and job.succeeded, "Input release was not confirmed")
            except Exception as error:
                errors.append(str(error))
            processes = [self.session._process, *self.session._aux_processes]
            for process in processes:
                if process is None:
                    continue
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                except Exception as error:
                    errors.append(str(error))
            self.session.tasker = None
            self.session.resource = None
            self.session._client = None
            self.session._aux_clients = []
            self.session._controller = None
        write_json(self.run / ".worker/shutdown.json", {"ok": not errors, "errors": errors})


def worker_main(run):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    adapter = None
    try:
        adapter = NavigationAdapter(run, read_json(run / ".worker/config.json"))
        for line in sys.stdin:
            request = json.loads(line)
            try:
                operation = request["operation"]
                if operation == "shutdown":
                    break
                if operation == "initialize":
                    result = adapter.initialize()
                elif operation == "query":
                    result = adapter.query(request["query_operation"], request["parameters"])
                elif operation == "teleport":
                    result = adapter.teleport(request["entry"])
                elif operation == "navigate_capture":
                    result = adapter.navigate_capture(request)
                else:
                    raise ValueError("Unknown navigation worker operation")
            except Exception as error:
                result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
                if request.get("operation") == "initialize":
                    result["connected"] = False
                if isinstance(error, NativeTaskError):
                    result.update(error.diagnostic)
            write_json(run / ".worker" / f"response-{request['sequence']:04d}.json", result)
            if adapter.cancelled():
                break
    finally:
        if adapter is not None:
            adapter.close()
        else:
            write_json(run / ".worker/shutdown.json", {"ok": False, "errors": ["Native adapter initialization failed"]})


if __name__ == "__main__":
    worker_main(Path(sys.argv[1]))
