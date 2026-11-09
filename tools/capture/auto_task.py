"""Automatic region tasks built on the immutable capture task store."""

import hashlib
import math
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from . import task
from .navigation_worker import NavigationWorker, native_install, verify_resource_mirror
from .validate import check_record, read_verified, require, safe_path

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def verify_plan(plan, workspace=None, install=None):
    require(
        isinstance(plan, dict) and plan.get("schema_version") == "capture.auto-plan/1.0",
        "Invalid automatic capture plan",
    )
    points = plan.get("waypoints")
    require(isinstance(points, list) and 0 < len(points) <= 10000, "Automatic plan must contain 1-10000 views")
    require(isinstance(plan.get("resources"), list) and bool(plan["resources"]), "Plan has no resource hash snapshot")
    for entry in plan["resources"]:
        path = task.check_path(SOURCE_ROOT / entry["uri"])
        require(path.is_relative_to(SOURCE_ROOT), "Plan resource escapes repository")
        read_verified(path, entry["sha256"], False)
    for index, point in enumerate(points):
        require(point.get("waypoint_id") == f"point-{index + 1:05d}", "Automatic observation IDs must be sequential")
        xy = point.get("position_xy")
        require(
            isinstance(xy, list)
            and len(xy) == 2
            and all(type(value) in (int, float) and math.isfinite(value) for value in xy),
            "Invalid BaseNav target",
        )
        require(
            type(point.get("target_deck_y")) in (int, float) and math.isfinite(point["target_deck_y"]),
            "Missing planned deck height",
        )
        require(
            type(point.get("yaw_degrees")) in (int, float) and 0 <= point["yaw_degrees"] < 360,
            "Invalid camera yaw intent",
        )
    coordinates = plan["map_coordinates"]
    require(coordinates["coordinate_frame_id"] == plan["region"]["zone_id"], "Plan coordinate frame mismatch")
    if workspace is not None:
        actual = verify_resource_mirror(workspace, plan["resources"], install=install)
        require(plan.get("runtime_resources") == actual, "Native runtime resources differ from planned resources")
    return plan


def runtime_snapshot(workspace, install):
    files = []
    for directory in (install / "agent", install / "maafw", install / "resource", install / "data"):
        for path in sorted(directory.rglob("*")):
            task.check_path(path)
            if path.is_file():
                files.append(
                    {
                        "uri": path.relative_to(workspace).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
    return files


def freeze_all(root):
    workspace = root.parents[2]
    exports = []
    for session in sorted((root / "sessions").iterdir()):
        if task.session_data(session, task.load_task(root), task.make_validators()) is not None:
            output = task.export(workspace, root.name, session.name)
            exports.append(str(output))
            print(f"Frozen bundle: {output}", flush=True)
    task.write_json(root / "automatic-exports.json", {"bundles": exports}, replace=True)
    return exports


def start_auto(workspace, request_or_plan, task_id=None, start_delay=5):
    workspace = task.check_path(workspace)
    if request_or_plan.get("schema_version") == "capture.auto-plan/1.0":
        plan = verify_plan(request_or_plan, workspace)
    else:
        from .planner import build_plan

        plan = verify_plan(build_plan(workspace, request_or_plan), workspace)
    install = native_install(workspace)
    sdk_version = version("maafw").removeprefix("v")
    require(sdk_version == "5.14.3", "Automatic capture requires locked maafw 5.14.3")
    task_id = task_id or "auto-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    region = plan["region"]
    metadata = {
        "execution_mode": "auto",
        "auto_plan": plan,
        "region_id": "region-" + str(region["id"]),
        "map_coordinates": plan["map_coordinates"],
        "runtime_root": install.relative_to(workspace).as_posix(),
        "runtime_files": runtime_snapshot(workspace, install),
        "sdk_version": sdk_version,
        "source": {
            "kind": "game_capture",
            "upstream_commit": task.UPSTREAM,
            "collector_version": "capture.auto/1.0-maafw-" + sdk_version,
            "submodule_commits": {},
            "game_version": None,
            "description": "MaaEnd MapNavigateAction and native CaptureViewAction. BaseNav navigation observations are coarse priors. "
            "Camera pose, pitch, FOV, native render timestamp and geometric quality remain unknown.",
        },
    }
    root = task.create_task(workspace, task_id, len(plan["waypoints"]), 0.25, region["name"], metadata)
    with task.exclusive(root / ".task.lock"), task.exclusive(root.parent / ".controller.lock"):
        return AutoTaskRunner(root, task.load_task(root), start_delay).run()


class AutoTaskRunner(task.TaskRunner):
    def __init__(self, root, metadata, start_delay):
        self.workspace = root.parents[2]
        self.native_clock = None
        super().__init__(root, metadata, NavigationWorker, start_delay)

    def verify_resources(self):
        install = task.check_path(self.workspace / self.task["runtime_root"])
        verify_plan(self.task["auto_plan"], self.workspace, install=install)
        require(
            version("maafw").removeprefix("v") == self.task["sdk_version"], "Capture SDK changed after task creation"
        )
        require(
            runtime_snapshot(self.workspace, install) == self.task["runtime_files"],
            "Native runtime file set or contents changed after task creation",
        )

    def preflight(self):
        self.verify_resources()
        if self.session is not None:
            self.new_session()
            self.checkpoint["session_id"] = self.session_id
            self.save("preflight")
        self.worker_root = self.root / "runtime" / ("navigation-" + uuid.uuid4().hex)
        self.worker_root.mkdir()
        for name in ("images", "logs", "sdk", ".worker"):
            (self.worker_root / name).mkdir()
        self.worker = NavigationWorker(
            self.worker_root, self.workspace, install=task.check_path(self.workspace / self.task["runtime_root"])
        )
        result = self.worker.request("initialize", timeout=75, should_cancel=self.should_cancel)
        self.event("connection", result=result, runtime_uri=self.worker_root.relative_to(self.root).as_posix())
        require(result.get("connected"), result.get("error", "Automatic capture initialization failed"))
        self.native_clock = result["clock"]
        seconds, remainder = divmod(int(self.native_clock["utc_anchor_unix_ns"]), 1_000_000_000)
        utc = (
            datetime.fromtimestamp(seconds, UTC)
            .replace(microsecond=remainder // 1000)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.clock = {
            "clock_id": self.native_clock["clock_id"],
            "utc_anchor": utc,
            "monotonic_anchor_ns": self.native_clock["monotonic_anchor_ns"],
            "utc_uncertainty_ns": None,
        }
        image = result["preflight"]["image"]
        self.verify_image(image)
        self.session = {
            "schema_version": "capture.session/1.0",
            "session_id": self.session_id,
            "clock": self.clock,
            "source": self.task["source"],
            "controller_kind": "win32-navigation-capture",
            "calibration_groups": [
                {
                    "calibration_group_id": "calibration-" + uuid.uuid4().hex,
                    "image_width": image["width"],
                    "image_height": image["height"],
                    "camera_mode": "closed_loop_yaw_unknown_pitch",
                    "fov_degrees": None,
                    "intrinsics_prior": None,
                    "image_coordinate_convention": "top_left_x_right_y_down_pixel_centers",
                }
            ],
        }
        check_record(self.validators, "session", self.session, "automatic session")
        task.write_json(self.session_root / "session.json", self.session)
        entry = self.task["auto_plan"]["region"].get("teleport_entry")
        require(entry, "Named automatic region has no teleport entry")
        teleported = self.worker.request("teleport", timeout=140, entry=entry, should_cancel=self.should_cancel)
        self.event("teleport", result=teleported)
        require(teleported.get("ok"), teleported.get("error", "Region teleport failed"))
        self.save("running")

    def verify_image(self, image):
        path = task.check_path(safe_path(self.worker_root, image["uri"]))
        read_verified(path, image["sha256"], False)
        from PIL import Image

        with Image.open(path) as png:
            png.load()
            require(
                png.format == "PNG" and png.mode == "RGB" and png.size == (image["width"], image["height"]),
                "Invalid raw photography PNG",
            )
        return path

    def capture_next(self):
        waypoint = self.task["auto_plan"]["waypoints"][self.next_index]
        frame_id = "frame-" + uuid.uuid4().hex
        result = self.worker.request(
            "navigate_capture",
            timeout=240,
            should_cancel=self.should_cancel,
            session_id=self.session_id,
            frame_id=frame_id,
            generation=self.checkpoint["generation"],
            waypoint=waypoint,
            zone_id=self.task["auto_plan"]["region"]["zone_id"],
        )
        self.event("native_attempt", result=result)
        require(result.get("ok"), result.get("error", "Automatic navigation or photography failed"))
        require(
            result.get("session_id") == self.session_id and result.get("frame_id") == frame_id,
            "Photography identity mismatch",
        )
        require(result.get("clock") == self.native_clock, "Native capture clock changed within session")
        require(result.get("schema_version") == "capture.view.result/1.0", "Unsupported native photography receipt")
        return result

    def commit(self, result):
        image = result["image"]
        path = self.verify_image(image)
        group = self.session["calibration_groups"][0]
        require(
            (image["width"], image["height"]) == (group["image_width"], group["image_height"]),
            "Raw screenshot dimensions changed",
        )
        completion = result["completion"]
        require(
            completion["session_id"] == self.session_id and completion["frame_id"] == result["frame_id"],
            "Native completion identity mismatch",
        )
        require(completion["clock_id"] == self.clock["clock_id"], "Native completion clock mismatch")
        waypoint = next(
            point for point in self.plan["waypoints"] if point["waypoint_id"] == f"point-{self.next_index + 1:05d}"
        )
        actual = completion.get("position_after_normalization")
        require(isinstance(actual, dict), "Photography has no normalized position observation")
        normalization = completion.get("normalization_id")
        zone = self.task["auto_plan"]["region"]["zone_id"]
        require(
            isinstance(normalization, str)
            and normalization.startswith("basenav:")
            and normalization.endswith(":" + zone),
            "Native coordinate normalization disagrees with plan",
        )
        arrival = result["arrival"]
        require(
            arrival["target"] == waypoint["position_xy"] and arrival["actual"] == [actual["x"], actual["y"]],
            "Arrival receipt disagrees with final observation",
        )
        require(
            type(arrival["distance"]) in (int, float) and 0 <= arrival["distance"] <= arrival["tolerance"] <= 3,
            "Final observation did not reach its target",
        )
        yaw = result["yaw"]
        require(
            yaw["target"] == waypoint["camera_intent"]["yaw_degrees"] and type(yaw["actual"]) in (int, float),
            "Final camera heading is missing",
        )
        require(
            abs((yaw["actual"] - yaw["target"] + 180) % 360 - 180) <= yaw["tolerance"] <= 3,
            "Final camera heading missed its target",
        )
        located = completion.get("locate_result") or {}

        def heading(value):
            if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 360:
                return {"status": "available", "value": value}
            return {"status": "unavailable", "value": None}

        navigation = {
            "region_id": self.task["region_id"],
            "coordinate_frame_id": self.task["map_coordinates"]["coordinate_frame_id"],
            "position_unit": "map_pixel",
            "position_xy": {"status": "available", "value": [actual["x"], actual["y"]]},
            "floor_height": {"status": "not_run", "value": None},
            "floor_height_unit": "navigation_unit",
            "character_heading": heading(located.get("character_heading")),
            "camera_yaw": heading((located.get("camera_heading") or {}).get("rot")),
            "heading_unit": "degree",
            "heading_convention": "clockwise_from_map_up",
            "raw_zone_id": actual.get("zone_id"),
            "normalization_id": normalization,
        }
        timing = completion["timing"]
        frame_id = result["frame_id"]
        frame = {
            "schema_version": "capture.frame/1.0",
            "session_id": self.session_id,
            "segment_id": waypoint["segment_id"],
            "frame_id": frame_id,
            "waypoint_id": waypoint["waypoint_id"],
            "calibration_group_id": group["calibration_group_id"],
            "timing": {
                "clock_id": self.clock["clock_id"],
                "request_monotonic_ns": timing["request_monotonic_ns"],
                "complete_monotonic_ns": timing["screencap_completed_monotonic_ns"],
                "locate_complete_monotonic_ns": timing["locate_completed_monotonic_ns"],
                "native_render_timestamp": None,
                "timestamp_source": "host_capture_api",
            },
            "image": {
                "status": "captured",
                "uri": f"images/raw/{frame_id}.png",
                "sha256": image["sha256"],
                "width": image["width"],
                "height": image["height"],
                "pixel_format": "RGB8",
                "backend": "MaaFramework/Win32Controller",
            },
            "navigation_observation": navigation,
            "camera_estimate_ref": None,
            "control": {"owner": "capture", "state": "arrived_yaw_verified_stable"},
            "quality": {"selection_status": "pending", "rejection_reasons": []},
            "native_attempt_id": result.get("native_attempt_id"),
        }
        self.persist_frame(frame, path)

    def run(self, kickoff=None):
        code = super().run(kickoff)
        try:
            bundles = freeze_all(self.root)
            self.checkpoint["bundle_uris"] = bundles
            self.save()
        except Exception as error:
            self.save("failed", f"Automatic export failed: {error}")
            self.event("export_failed", error=str(error))
            return 1
        return code
