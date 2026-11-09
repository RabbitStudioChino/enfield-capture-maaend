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
        os.environ.update(MAAFW_BINARY_PATH=str(self.install / "maafw"), PI_USER_DATA_DIR=str(run))

    def cancelled(self):
        return (self.run / ".worker/stop.json").exists()

    def stop_task(self):
        if self.session is not None and self.session.tasker is not None and not self.stop_requested:
            self.stop_requested = True
            self.session.tasker.post_stop()

    def wait_task(self, job, timeout, check_window=True):
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
                    self.stop_task()
                    raise TimeoutError("Native navigation operation exceeded its deadline")
                time.sleep(0.05)
            require(job.succeeded, "Native navigation operation failed")
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
        self.wait_task(job, 170, check_window=not self.query_only)
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
        self.wait_task(self.session.tasker.post_task(name), timeout)

    def teleport(self, entry):
        require(isinstance(entry, str) and entry.startswith("SceneEnterWorld"), "Invalid teleport entry")
        self.wait_task(self.session.tasker.post_task(entry), 120)
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
        require((output / "result.json").is_file(), failure or "Native photography produced no receipt")
        result = read_json(output / "result.json")
        result["receipt_uri"] = (output / "result.json").relative_to(self.run).as_posix()
        if failure and result.get("ok"):
            result.update(ok=False, error=failure)
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
                result = {"ok": False, "connected": False, "error": f"{type(error).__name__}: {error}"}
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
