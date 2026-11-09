"""Durable, manually positioned Windows capture tasks without a network listener."""

import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

from .probe import utc_now
from .validate import (
    check_frames,
    check_record,
    file_index,
    load_json,
    make_validators,
    read_frames,
    read_verified,
    require,
    safe_path,
    validate_bundle,
)
from .win32_adapter import Win32Worker, WorkerCancelled

UPSTREAM = "8cccaaec8f59f4b4689b59db84a39d25066ff5ec"
CONTROL_ACTIONS = {"pause", "stop", "resume"}


def check_path(path):
    path = Path(path).absolute()
    require(".." not in path.parts, f"parent traversal is forbidden: {path}")
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        require(
            not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
            f"linked path is forbidden: {component}",
        )
    return path


def valid_id(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value), "invalid task/command ID"
    )
    require(
        value.upper()
        not in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))},
        "reserved Windows ID",
    )
    return value


def encoded(record):
    return (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=4, allow_nan=False) + "\n").encode("utf-8")


def atomic_bytes(path, data, replace=False):
    check_path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    require(replace or not path.exists(), f"refusing to overwrite {path}")
    temporary.replace(path)


def write_json(path, record, replace=False):
    atomic_bytes(path, encoded(record), replace)


def read_json(path):
    return load_json(check_path(path).read_bytes(), str(path))


@contextmanager
def exclusive(path):
    """Use an OS-owned lock so a crashed process cannot strand ownership."""
    check_path(path)
    stream = path.open("a+b")
    locked = False
    try:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise BlockingIOError(f"capture lock is held: {path}") from error
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def task_path(workspace, task, create=False):
    workspace = check_path(workspace)
    require(workspace.is_dir(), "workspace must be an existing real directory")
    parent = workspace / "local" / "capture"
    if create:
        for directory in (workspace / "local", parent):
            check_path(directory).mkdir(exist_ok=True)
    return check_path(parent / valid_id(task))


def create_task(workspace, task, count, interval, region_label, metadata=None):
    require(type(count) is int and 1 <= count <= 10000, "count must be between 1 and 10000")
    require(isinstance(interval, (int, float)) and 0.25 <= interval <= 3600, "interval must be between 0.25 and 3600")
    require(isinstance(region_label, str) and bool(region_label.strip()), "region label is required")
    root = task_path(workspace, task, create=True)
    root.mkdir()
    for name in ("sessions", "commands", "acks", "events", "runtime", "exports"):
        (root / name).mkdir()
    region = hashlib.sha256(region_label.encode("utf-8")).hexdigest()[:24]
    record = {
        "schema_version": "capture.task/1.0",
        "task_id": task,
        "count": count,
        "interval_seconds": interval,
        "region_label": region_label,
        "region_id": f"region-{region}",
        "map_coordinates": {
            "coordinate_frame_id": f"manual-map-{region}",
            "unit": "map_pixel",
            "axes": "x_right_y_down",
            "heading_unit": "degree",
            "heading_convention": "clockwise_from_map_up",
            "scale_status": "arbitrary",
        },
        "source": {
            "kind": "game_capture",
            "upstream_commit": UPSTREAM,
            "collector_version": "capture.manual/1.0",
            "submodule_commits": {},
            "game_version": None,
            "description": "User-controlled movement and camera; MaaFramework ScreenDC with Null input. "
            "No navigation, pose, FOV, image freshness or geometric quality is measured.",
        },
    }
    if metadata is not None:
        record.update(metadata)
    write_json(root / "task.json", record)
    write_json(
        root / "checkpoint.json",
        {
            "schema_version": "capture.checkpoint/1.0",
            "task_id": task,
            "generation": None,
            "session_id": None,
            "status": "created",
            "next_index": 0,
            "count": count,
            "error": None,
            "updated_utc": utc_now(),
        },
    )
    return root


def load_task(root):
    task = read_json(root / "task.json")
    require(
        task.get("schema_version") == "capture.task/1.0" and task.get("task_id") == root.name, "invalid task metadata"
    )
    require(type(task.get("count")) is int and 1 <= task["count"] <= 10000, "invalid task count")
    require(0.25 <= task["interval_seconds"] <= 3600, "invalid task interval")
    return task


def session_data(root, task, validators):
    """Read only committed batches. Orphan images/records and partial writes stay untouched."""
    batches = sorted(check_path(root / "batches").glob("*.json"))
    if not batches:
        return None
    session = read_json(root / "session.json")
    plan = read_json(root / "plan.json")
    check_record(validators, "session", session, "session")
    check_record(validators, "plan", plan, "plan")
    require(session["session_id"] == root.name == plan["session_id"], "session identity mismatch")
    require(session["source"] == task["source"], "task source changed")
    require(
        plan["region_id"] == task["region_id"] and plan["map_coordinates"] == task["map_coordinates"],
        "task region changed",
    )
    expected_plan = "capture.plan/1.0" if task.get("execution_mode") == "auto" else "capture.plan/1.1"
    require(plan["schema_version"] == expected_plan, "task plan execution mode mismatch")
    identity = {key: session[key] for key in ("session_id", "clock", "source", "controller_kind")}
    frames, files, batch_uris = {}, {}, []
    indices = set()
    for path in batches:
        batch = read_json(path)
        check_record(validators, "batch", batch, path.name)
        require(batch["batch_id"] == path.stem, "batch file identity mismatch")
        require(batch["session_identity"] == identity and batch["session_id"] == root.name, "batch identity mismatch")
        require(
            batch["region_id"] == plan["region_id"] and batch["input_revision"] == plan["revision"],
            "batch plan mismatch",
        )
        indexed = file_index(batch["files"], path.name)
        contents = {}
        for uri, sha in indexed.items():
            contents[uri] = read_verified(check_path(safe_path(root, uri)), sha, uri.endswith((".json", ".jsonl")))
            require(uri not in files or files[uri] == sha, "conflicting committed file hash")
            files[uri] = sha
        require(
            "plan.json" in indexed and contents["plan.json"] == (root / "plan.json").read_bytes(), "batch plan changed"
        )
        shard_uri = batch["frames_uri"]
        require(
            shard_uri.startswith("records/") and shard_uri.endswith(".jsonl") and shard_uri in contents,
            "invalid frame shard",
        )
        shard = read_frames(contents[shard_uri], validators, shard_uri)
        require(len(shard) == 1 and list(shard) == batch["frame_ids"], "capture batch must contain exactly one frame")
        frame = next(iter(shard.values()))
        require(frame["frame_id"] not in frames and frame["image"]["status"] == "captured", "invalid committed frame")
        require(frame["quality"]["selection_status"] == "pending", "capture image quality must remain pending")
        require(set(indexed) == {"plan.json", shard_uri, frame["image"]["uri"]}, "batch file coverage mismatch")
        require(batch["calibration_groups"] == session["calibration_groups"], "immutable calibration mismatch")
        match = re.fullmatch(r"point-([0-9]{5})", frame["waypoint_id"])
        require(match is not None, "invalid logical observation point")
        index = int(match[1]) - 1
        require(0 <= index < task["count"] and index not in indices, "duplicate or invalid logical observation point")
        indices.add(index)
        frames.update(shard)
        batch_uris.append(path.relative_to(root).as_posix())
    ordered = dict(sorted(frames.items(), key=lambda pair: pair[1]["waypoint_id"]))
    check_frames(ordered, session, plan, files)
    return {
        "session": session,
        "plan": plan,
        "frames": ordered,
        "files": files,
        "batch_uris": batch_uris,
        "indices": indices,
    }


def recover(root, task, validators):
    completed = set()
    for session in sorted(check_path(root / "sessions").iterdir()):
        check_path(session)
        require(session.is_dir(), "invalid session entry")
        data = session_data(session, task, validators)
        if data is not None:
            require(not completed.intersection(data["indices"]), "logical observation committed twice")
            completed.update(data["indices"])
    require(completed == set(range(len(completed))), "committed observations contain a gap")
    return len(completed)


def acknowledge(root, command, status, checkpoint, reason=None):
    path = root / "acks" / f"{command['command_id']}.json"
    if not path.exists():
        write_json(
            path,
            {
                "schema_version": "capture.ack/1.0",
                **command,
                "result": status,
                "reason": reason,
                "checkpoint": dict(checkpoint),
                "acknowledged_utc": utc_now(),
            },
        )
    return read_json(path)


def publish_command(root, action, command_id=None, generation=None):
    valid_id(command_id) if command_id else None
    command_id = command_id or uuid.uuid4().hex
    with exclusive(root / ".commands.lock"):
        path = root / "commands" / f"{command_id}.json"
        if path.exists():
            old = read_json(path)
            require(
                old["action"] == action and (generation is None or old["generation"] == generation),
                "command ID reused with different payload",
            )
            return old
        checkpoint = read_json(root / "checkpoint.json")
        command = {"command_id": command_id, "generation": generation or checkpoint["generation"], "action": action}
        write_json(path, command)
        return command


def status(workspace, task):
    root = task_path(workspace, task)
    result = read_json(root / "checkpoint.json")
    try:
        with exclusive(root / ".task.lock"):
            result["active"] = False
    except BlockingIOError:
        result["active"] = True
    if not result["active"] and result["status"] in ("preflight", "running", "paused"):
        result["status"] = "interrupted"
    if not result["active"]:
        result["next_index"] = recover(root, load_task(root), make_validators())
        if (
            result["next_index"] == result["count"]
            and result["status"] == "interrupted"
            and result.get("error") is None
        ):
            result["status"] = "completed"
    return result


class TaskRunner:
    def __init__(self, root, task, worker_type, start_delay):
        self.root, self.task, self.worker_type = root, task, worker_type
        self.validators = make_validators()
        self.next_index = recover(root, task, self.validators)
        require(self.next_index < task["count"], "task is already completed")
        self.worker = None
        self.worker_root = None
        self.worker_index = 0
        self.session = None
        self.deadline = time.monotonic() + start_delay
        self.clock = {
            "clock_id": "clock-" + uuid.uuid4().hex,
            "utc_anchor": utc_now(),
            "monotonic_anchor_ns": str(time.monotonic_ns()),
            "utc_uncertainty_ns": None,
        }
        self.new_session()
        self.checkpoint = {
            "schema_version": "capture.checkpoint/1.0",
            "task_id": root.name,
            "generation": uuid.uuid4().hex,
            "session_id": self.session_id,
            "status": "preflight",
            "next_index": self.next_index,
            "count": task["count"],
            "error": None,
        }
        self.save()

    def new_session(self):
        self.session_id = "session-" + uuid.uuid4().hex
        self.session_root = self.root / "sessions" / self.session_id
        self.session_root.mkdir()
        for directory in ("images", "images/raw", "records", "batches"):
            (self.session_root / directory).mkdir()
        self.session = None
        self.plan = {
            "schema_version": "capture.plan/1.1",
            "execution_mode": "manual",
            "plan_id": "plan-" + uuid.uuid4().hex,
            "revision": 1,
            "session_id": self.session_id,
            "region_id": self.task["region_id"],
            "map_coordinates": self.task["map_coordinates"],
            "waypoints": [
                {
                    "waypoint_id": f"point-{index + 1:05d}",
                    "segment_id": f"segment-{self.session_id}-{index + 1:05d}",
                    "position_xy": None,
                    "camera_intent": {"yaw_degrees": None, "pitch_degrees": None, "settle_required": False},
                }
                for index in range(self.next_index, self.task["count"])
            ],
        }
        if self.task.get("execution_mode") == "auto":
            self.plan["schema_version"] = "capture.plan/1.0"
            self.plan.pop("execution_mode")
            for waypoint, target in zip(
                self.plan["waypoints"], self.task["auto_plan"]["waypoints"][self.next_index :], strict=True
            ):
                waypoint["position_xy"] = target["position_xy"]
                waypoint["camera_intent"] = {
                    "yaw_degrees": target["yaw_degrees"],
                    "pitch_degrees": None,
                    "settle_required": True,
                }
        check_record(self.validators, "plan", self.plan, "capture plan")
        write_json(self.session_root / "plan.json", self.plan)

    def save(self, state=None, error=None):
        if state is not None:
            self.checkpoint.update(status=state, error=error)
        self.checkpoint.update(next_index=self.next_index, updated_utc=utc_now())
        write_json(self.root / "checkpoint.json", self.checkpoint, replace=True)

    def event(self, kind, **details):
        write_json(
            self.root / "events" / f"{time.time_ns()}-{uuid.uuid4().hex}.json",
            {
                "utc": utc_now(),
                "kind": kind,
                "session_id": self.session_id,
                "generation": self.checkpoint["generation"],
                "next_index": self.next_index,
                **details,
            },
        )

    def pending(self):
        result = []
        for path in sorted((self.root / "commands").glob("*.json")):
            if (self.root / "acks" / path.name).exists():
                continue
            command = read_json(path)
            require(
                command.get("command_id") == path.stem and command.get("action") in CONTROL_ACTIONS,
                "invalid control command",
            )
            if command.get("generation") != self.checkpoint["generation"]:
                acknowledge(self.root, command, "rejected", self.checkpoint, "stale generation")
            else:
                result.append(command)
        return sorted(result, key=lambda item: {"stop": 0, "pause": 1, "resume": 2}[item["action"]])

    def should_cancel(self):
        return any(command["action"] in ("pause", "stop") for command in self.pending())

    def close_worker(self):
        if self.worker is not None:
            try:
                self.worker.close()
            finally:
                self.worker = None

    def controls(self):
        commands = self.pending()
        if not commands:
            return
        command = commands[0]
        action = command["action"]
        if action in ("pause", "stop"):
            self.close_worker()
            self.save("paused" if action == "pause" else "stopped")
        elif self.checkpoint["status"] == "paused":
            self.save("preflight")
            self.deadline = time.monotonic() + 5
        else:
            self.save()
        self.event("command", command=command, status=self.checkpoint["status"])
        acknowledge(self.root, command, "applied", self.checkpoint)
        for other in commands[1:]:
            acknowledge(self.root, other, "rejected", self.checkpoint, "superseded by a higher-priority command")

    def checked_image(self, record):
        image = record.get("image", {})
        require(image.get("status") == "captured", record.get("error") or image.get("reason") or "screenshot failed")
        expected = f"images/frame-{self.worker_index:06d}.png"
        require(image.get("uri") == expected, "unexpected worker image path")
        path = check_path(safe_path(self.worker_root, expected))
        read_verified(path, image["sha256"], False)
        from PIL import Image

        with Image.open(path) as png:
            png.load()
            require(
                png.format == "PNG" and png.mode == "RGB" and png.size == (image["width"], image["height"]),
                "invalid worker PNG",
            )
        timing = record.get("timing", {})
        for key in ("request_monotonic_ns", "complete_monotonic_ns"):
            require(
                isinstance(timing.get(key), str) and re.fullmatch(r"0|[1-9][0-9]*", timing[key]),
                "missing capture API timing",
            )
        require(
            int(self.clock["monotonic_anchor_ns"])
            <= int(timing["request_monotonic_ns"])
            <= int(timing["complete_monotonic_ns"]),
            "invalid capture API timing",
        )
        return image, path

    def preflight(self):
        require(shutil.disk_usage(self.root).free >= 64 * 1024 * 1024, "less than 64 MiB free in task workspace")
        self.worker_root = self.root / "runtime" / ("worker-" + uuid.uuid4().hex)
        self.worker_root.mkdir()
        for name in ("images", "logs", "sdk", ".worker"):
            (self.worker_root / name).mkdir()
        self.worker_index = 0
        self.worker = self.worker_type(self.worker_root)
        connection = self.worker.request("initialize", timeout=25, should_cancel=self.should_cancel)
        self.event("connection", result=connection, runtime_uri=self.worker_root.relative_to(self.root).as_posix())
        require(connection.get("connected"), connection.get("error", "controller connection failed"))
        record = self.worker.request("capture", timeout=12, index=0, should_cancel=self.should_cancel)
        self.event("preflight_capture", result=record)
        image, _ = self.checked_image(record)
        if self.session is None:
            self.session = {
                "schema_version": "capture.session/1.0",
                "session_id": self.session_id,
                "clock": self.clock,
                "source": self.task["source"],
                "controller_kind": "win32-screen-dc-null-input",
                "calibration_groups": [
                    {
                        "calibration_group_id": "calibration-" + uuid.uuid4().hex,
                        "image_width": image["width"],
                        "image_height": image["height"],
                        "camera_mode": "user_controlled_unknown",
                        "fov_degrees": None,
                        "intrinsics_prior": None,
                        "image_coordinate_convention": "top_left_x_right_y_down_pixel_centers",
                    }
                ],
            }
            check_record(self.validators, "session", self.session, "manual session")
            write_json(self.session_root / "session.json", self.session)
        group = self.session["calibration_groups"][0]
        require(
            (image["width"], image["height"]) == (group["image_width"], group["image_height"]),
            "raw dimensions changed; stop and resume in a new process/session",
        )
        self.save("running")

    def commit(self, record):
        image, path = self.checked_image(record)
        frame_id = "frame-" + uuid.uuid4().hex
        image_uri = f"images/raw/{frame_id}.png"
        waypoint = next(
            item for item in self.plan["waypoints"] if item["waypoint_id"] == f"point-{self.next_index + 1:05d}"
        )
        group = self.session["calibration_groups"][0]
        navigation = {
            "region_id": self.task["region_id"],
            "coordinate_frame_id": self.task["map_coordinates"]["coordinate_frame_id"],
            "position_unit": "map_pixel",
            "floor_height_unit": "navigation_unit",
            "heading_unit": "degree",
            "heading_convention": "clockwise_from_map_up",
            **{
                key: {"status": "not_run", "value": None}
                for key in ("position_xy", "floor_height", "character_heading", "camera_yaw")
            },
        }
        frame = {
            "schema_version": "capture.frame/1.0",
            "session_id": self.session_id,
            "segment_id": waypoint["segment_id"],
            "frame_id": frame_id,
            "waypoint_id": waypoint["waypoint_id"],
            "calibration_group_id": group["calibration_group_id"],
            "timing": {
                "clock_id": self.clock["clock_id"],
                "request_monotonic_ns": record["timing"]["request_monotonic_ns"],
                "complete_monotonic_ns": record["timing"]["complete_monotonic_ns"],
                "locate_complete_monotonic_ns": None,
                "native_render_timestamp": None,
                "timestamp_source": "host_capture_api",
            },
            "image": {key: image[key] for key in ("status", "sha256", "width", "height", "pixel_format")},
            "navigation_observation": navigation,
            "camera_estimate_ref": None,
            "control": {"owner": "none", "state": "manual_user_controlled"},
            "quality": {"selection_status": "pending", "rejection_reasons": []},
        }
        frame["image"].update(uri=image_uri, backend="MaaFramework/ScreenDC")
        self.persist_frame(frame, path)

    def persist_frame(self, frame, path):
        frame_id = frame["frame_id"]
        image = frame["image"]
        image_uri = f"images/raw/{frame_id}.png"
        require(image["uri"] == image_uri, "unexpected committed image path")
        target = self.session_root / image_uri
        temporary = target.with_suffix(".png.partial")
        with path.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        read_verified(temporary, image["sha256"], False)
        require(not target.exists(), "refusing to overwrite a raw image")
        temporary.rename(target)
        check_record(self.validators, "frame", frame, "capture frame")
        shard_uri = f"records/{frame_id}.frames.jsonl"
        atomic_bytes(
            self.session_root / shard_uri,
            json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n",
        )
        files = [
            {"uri": uri, "sha256": hashlib.sha256((self.session_root / uri).read_bytes()).hexdigest()}
            for uri in ("plan.json", shard_uri, image_uri)
        ]
        check_frames({frame_id: frame}, self.session, self.plan, file_index(files, "new batch"))
        batch = {
            "schema_version": "capture.batch/1.0",
            "batch_id": frame_id,
            "session_id": self.session_id,
            "region_id": self.task["region_id"],
            "input_revision": 1,
            "status": "committed",
            "frame_ids": [frame_id],
            "files": files,
            "frames_uri": shard_uri,
            "session_identity": {
                key: self.session[key] for key in ("session_id", "clock", "source", "controller_kind")
            },
            "calibration_groups": self.session["calibration_groups"],
        }
        check_record(self.validators, "batch", batch, "new batch")
        write_json(self.session_root / "batches" / f"{frame_id}.json", batch)
        self.next_index += 1
        self.save()
        self.event("committed", frame_id=frame_id)
        print(f"{self.next_index}/{self.task['count']} committed; geometric quality pending", flush=True)

    def capture_next(self):
        self.worker_index += 1
        return self.worker.request("capture", timeout=12, index=self.worker_index, should_cancel=self.should_cancel)

    def run(self, kickoff=None):
        print(
            f"Task: {self.root.name}  Session: {self.session_id}  Generation: {self.checkpoint['generation']}",
            flush=True,
        )
        description = (
            "Automatic navigation and photography"
            if self.task.get("execution_mode") == "auto"
            else "Move and aim manually"
        )
        print(f"{description}. Starting in {max(0, self.deadline - time.monotonic()):g}s. Ctrl+C stops.", flush=True)
        if kickoff:
            acknowledge(self.root, kickoff, "applied", self.checkpoint)
        code = 0
        try:
            while self.checkpoint["status"] not in ("stopped", "completed"):
                self.controls()
                if self.checkpoint["status"] == "stopped":
                    break
                if self.checkpoint["status"] == "paused":
                    time.sleep(0.1)
                    continue
                if self.next_index == self.task["count"]:
                    self.close_worker()
                    self.save("completed")
                    break
                if time.monotonic() < self.deadline:
                    time.sleep(min(0.1, self.deadline - time.monotonic()))
                    continue
                try:
                    if self.worker is None:
                        self.preflight()
                        continue
                    record = self.capture_next()
                    self.event("attempt", result=record)
                    self.commit(record)
                    self.deadline = time.monotonic() + self.task["interval_seconds"]
                except WorkerCancelled as error:
                    self.event("attempt_cancelled", error=str(error))
                    self.close_worker()
                    self.controls()
                except (RuntimeError, ValueError, TimeoutError) as error:
                    self.close_worker()
                    self.event("paused", error=f"{type(error).__name__}: {error}")
                    self.save("paused", str(error))
        except KeyboardInterrupt:
            self.close_worker()
            self.save("stopped", "Interrupted by user")
            self.event("stopped", reason="Ctrl+C")
            code = 130
        except Exception as error:
            with suppress(Exception):
                self.close_worker()
            self.save("failed", f"{type(error).__name__}: {error}")
            self.event("failed", error=self.checkpoint["error"])
            for command in self.pending():
                acknowledge(self.root, command, "rejected", self.checkpoint, self.checkpoint["error"])
            code = 1
        finally:
            self.close_worker()
        return code


def start(workspace, task, count, interval, region_label, start_delay=5, worker_type=Win32Worker):
    root = create_task(workspace, task, count, interval, region_label)
    with exclusive(root / ".task.lock"), exclusive(root.parent / ".controller.lock"):
        return TaskRunner(root, load_task(root), worker_type, start_delay).run()


def control(
    workspace, task, action, command_id=None, generation=None, timeout=30, worker_type=Win32Worker, start_delay=5
):
    root = task_path(workspace, task)
    command = publish_command(root, action, command_id, generation)
    ack_path = root / "acks" / f"{command['command_id']}.json"
    if ack_path.exists():
        return read_json(ack_path)
    try:
        with exclusive(root / ".task.lock"):
            checkpoint = read_json(root / "checkpoint.json")
            if command["generation"] != checkpoint["generation"]:
                return acknowledge(root, command, "rejected", checkpoint, "stale generation")
            if action != "resume":
                return acknowledge(
                    root, command, "rejected", checkpoint, "task process is not active; resume starts a new session"
                )
            try:
                with exclusive(root.parent / ".controller.lock"):
                    if recover(root, load_task(root), make_validators()) == load_task(root)["count"]:
                        return acknowledge(root, command, "rejected", checkpoint, "task is already completed")
                    task_record = load_task(root)
                    if task_record.get("execution_mode") == "auto":
                        from .auto_task import AutoTaskRunner

                        runner = AutoTaskRunner(root, task_record, start_delay)
                    else:
                        runner = TaskRunner(root, task_record, worker_type, start_delay)
                    code = runner.run(kickoff=command)
                    return {**read_json(ack_path), "run_exit_code": code}
            except BlockingIOError:
                return acknowledge(
                    root, command, "rejected", checkpoint, "another task holds this workspace controller"
                )
    except BlockingIOError:
        pass
    deadline = time.monotonic() + timeout
    while not ack_path.exists():
        require(
            time.monotonic() < deadline,
            f"command {command['command_id']} remains pending; inspect status and retry the same command ID",
        )
        time.sleep(0.1)
    return read_json(ack_path)


def export(workspace, task, session_id=None):
    root = task_path(workspace, task)
    task_record = load_task(root)
    session_id = session_id or read_json(root / "checkpoint.json")["session_id"]
    require(isinstance(session_id, str) and re.fullmatch(r"session-[0-9a-f]{32}", session_id), "invalid session ID")
    source = check_path(root / "sessions" / session_id)
    with exclusive(root / ".export.lock"):
        data = session_data(source, task_record, make_validators())
        require(data is not None, "session has no committed frames")
        export_id = "bundle-" + uuid.uuid4().hex
        staging = check_path(root / "exports" / ("." + export_id + ".partial"))
        staging.mkdir()
        uris = {"session.json", "plan.json", *data["files"], *data["batch_uris"]}
        for uri in sorted(uris):
            path = check_path(safe_path(source, uri))
            target = staging / uri
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_bytes(target, path.read_bytes())
        atomic_bytes(
            staging / "frames.jsonl",
            b"".join(
                json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
                for frame in data["frames"].values()
            ),
        )
        files = [
            {"uri": path.relative_to(staging).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(staging.rglob("*"))
            if path.is_file()
        ]
        write_json(
            staging / "bundle.json",
            {
                "schema_version": "capture.bundle/1.0",
                "bundle_id": export_id,
                "session_id": session_id,
                "status": "frozen",
                "source": data["session"]["source"],
                "session_uri": "session.json",
                "plan_uri": "plan.json",
                "frames_uri": "frames.jsonl",
                "batch_uris": data["batch_uris"],
                "files": files,
            },
        )
        validate_bundle(staging)
        output = staging.with_name(export_id)
        require(not output.exists(), "export already exists")
        staging.rename(output)
        return output


def logs(workspace, task):
    root = task_path(workspace, task)
    return [read_json(path) for path in sorted(check_path(root / "events").glob("*.json"))]
