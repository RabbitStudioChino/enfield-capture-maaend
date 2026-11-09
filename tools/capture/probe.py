"""Finite capture diagnostics. SDK and OS calls live in win32_adapter."""

import hashlib
import json
import os
import platform
import sys
import time
import uuid
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .win32_adapter import Win32Worker


def utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path, record):
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=4, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def create_run(workspace):
    workspace = Path(workspace).absolute()
    if workspace.is_symlink() or workspace.is_junction() or not workspace.is_dir():
        raise ValueError("workspace must be an existing real directory")
    boundary = workspace.resolve(strict=True)
    current = workspace
    for part in ("local", "validation", "windows-capture"):
        current /= part
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"linked output directory is forbidden: {current}")
        current.mkdir(exist_ok=True)
        if not current.resolve(strict=True).is_relative_to(boundary):
            raise ValueError(f"output directory escapes workspace: {current}")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    run = current / run_id
    run.mkdir(exist_ok=False)
    for name in ("images", "logs", "sdk", ".worker"):
        (run / name).mkdir()
    return run


def package_versions():
    result = {}
    for name in ("maafw", "numpy", "pillow", "strenum", "maaagentbinary"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def run_probe(workspace, command, count, interval, start_delay, worker_type=Win32Worker):
    run = create_run(workspace)
    report_path = run / "report.json"
    frames_path = run / "frames.jsonl"
    frames_path.touch(exist_ok=False)
    report = {
        "schema_version": "capture.probe/1.0",
        "run_id": run.name,
        "command": command,
        "status": "running",
        "started_utc": utc_now(),
        "finished_utc": None,
        "environment": {
            "python": sys.version,
            "os": platform.platform(),
            "machine": platform.machine(),
            "packages": package_versions(),
        },
        "parameters": {"count": count, "interval_seconds": interval, "start_delay_seconds": start_delay},
        "source": {
            "kind": "game_capture_probe",
            "sdk_expected": "5.14.3",
            "screencap": "ScreenDC",
            "window_side_effects": "SDK may restore foreground or move the window during capture",
            "window_check_scope": "checks at API boundaries and polling points; focus/minimize races remain possible",
            "frame_freshness": "unknown; no native render timestamp is exposed",
            "input_methods": "Null",
            "raw_size_requested": True,
            "configuration_basis": "assets/interface.json Win32-Front at 8cccaaec8f59f4b4689b59db84a39d25066ff5ec",
        },
        "clock": {
            "clock_id": run.name,
            "utc_anchor": utc_now(),
            "monotonic_anchor_ns": str(time.monotonic_ns()),
            "native_render_timestamp": None,
        },
        "connection": None,
        "worker": None,
        "attempted": 0,
        "captured": 0,
        "failed": 0,
        "duplicate_images": 0,
        "warnings": [],
        "errors": [],
        "files": [],
    }
    write_json(report_path, report)
    print(f"Results: {run}", flush=True)
    print(f"Switch to the running Endfield window within {start_delay:g} seconds. Ctrl+C stops capture.", flush=True)
    worker = None
    exit_code = 1
    try:
        remaining = start_delay
        while remaining > 0:
            print(f"Starting in {remaining:g}s", flush=True)
            pause = min(1, remaining)
            time.sleep(pause)
            remaining -= pause
        worker = worker_type(run)
        report["worker"] = {"pid": worker.pid, "exit_reason": None}
        report["connection"] = worker.request("initialize", timeout=25)
        write_json(report_path, report)
        if not report["connection"].get("connected"):
            raise RuntimeError(report["connection"].get("error", "connection failed"))
        seen_images = {}
        with frames_path.open("a", encoding="utf-8") as stream:
            for index in range(1, count + 1):
                report["attempted"] += 1
                attempt_start = str(time.monotonic_ns())
                interrupted = False
                try:
                    record = worker.request("capture", timeout=12, index=index)
                except (Exception, KeyboardInterrupt) as error:
                    interrupted = isinstance(error, KeyboardInterrupt)
                    message = f"{type(error).__name__}: {error}"
                    record = {
                        "image": {"status": "failed", "reason": message},
                        "fatal": True,
                        "error": message,
                        "timing": {
                            "request_monotonic_ns": None,
                            "complete_monotonic_ns": None,
                            "native_render_timestamp": None,
                        },
                    }
                record.update(
                    schema_version="capture.probe.frame/1.0",
                    run_id=run.name,
                    frame_id=f"frame-{index:06d}",
                    navigation=None,
                    camera_pose=None,
                    frame_freshness="unknown",
                    attempt_started_monotonic_ns=attempt_start,
                    attempt_finished_monotonic_ns=str(time.monotonic_ns()),
                )
                if record.get("image", {}).get("status") == "captured":
                    image = record["image"]
                    expected_uri = f"images/frame-{index:06d}.png"
                    if image.get("uri") != expected_uri:
                        raise RuntimeError("worker returned an unexpected image path")
                    image_path = run / expected_uri
                    if (
                        image_path.is_symlink()
                        or hashlib.sha256(image_path.read_bytes()).hexdigest() != image["sha256"]
                    ):
                        raise RuntimeError("worker image hash verification failed")
                    report["captured"] += 1
                    if image["sha256"] in seen_images:
                        image["duplicate_of_frame_id"] = seen_images[image["sha256"]]
                        report["duplicate_images"] += 1
                        if not report["warnings"]:
                            report["warnings"].append(
                                "Repeated image content; inspect manual viewpoint changes and backend freshness"
                            )
                    else:
                        seen_images[image["sha256"]] = record["frame_id"]
                else:
                    report["failed"] += 1
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                write_json(report_path, report)
                print(f"{index}/{count}: {record.get('image', {}).get('status', 'failed')}", flush=True)
                if interrupted:
                    raise KeyboardInterrupt
                if record.get("fatal"):
                    raise RuntimeError(record.get("error", "worker capture failed"))
                if index < count:
                    time.sleep(interval)
        report["status"] = "complete" if report["captured"] == count else "partial" if report["captured"] else "failed"
        exit_code = 0 if report["status"] == "complete" else 1
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["errors"].append("Interrupted by user")
        exit_code = 130
    except Exception as error:
        report["status"] = "failed"
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        if worker is not None:
            try:
                worker.close()
            except Exception as error:
                report["errors"].append(f"cleanup_error: {type(error).__name__}: {error}")
                report["status"] = "failed"
                exit_code = 1
            report["worker"].update(exit_reason=report["status"], returncode=worker.returncode)
        report["finished_utc"] = utc_now()
        try:
            for path in sorted(run.rglob("*")):
                if path.is_file() and path != report_path and not path.name.endswith(".partial"):
                    report["files"].append(
                        {
                            "uri": path.relative_to(run).as_posix(),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    )
        except Exception as error:
            report["errors"].append(f"final_hash_error: {type(error).__name__}: {error}")
            report["status"] = "failed"
            exit_code = 1
        try:
            write_json(report_path, report)
        except Exception as error:
            print(f"{report['status']}: cannot finalize report: {error}; errors={report['errors']}", file=sys.stderr)
            return 1
        print(
            f"{report['status']}: {report['captured']}/{report['attempted']} captured. Report: {report_path}",
            flush=True,
        )
    return exit_code
