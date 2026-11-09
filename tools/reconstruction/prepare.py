"""Copy selected raw images and provenance without solving cameras or geometry."""

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from tools.capture.validate import (
    BundleValidationError,
    check_frames,
    check_record,
    file_index,
    load_json,
    make_validators,
    read_frames,
    read_jsonl,
    read_verified,
    require,
    safe_path,
    validate_bundle,
)

FORMAT = "reconstruction.preparation/1.0"
GENERATOR = {"name": "tools.reconstruction", "version": "1.0.0"}
STATE = {
    "reconstruction": "not_run",
    "camera_solution": "not_run",
    "scale": "unknown",
    "quality": "not_assessed",
    "hud_removal": "not_run",
    "image_processing": "none",
    "camera_group_mapping": "requires_explicit_mapping",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def check_path(path):
    """Reject symlinks and Windows reparse points in every existing component."""
    require(".." not in path.parts, f"parent traversal is forbidden: {path}")
    path = path.absolute()
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        require(
            not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
            f"symlink or reparse point is forbidden: {component}",
        )
    return path


def within(workspace, path):
    path = check_path(path)
    require(path.is_relative_to(workspace), f"path is outside workspace: {path}")
    return path


def check_tree(root):
    check_path(root)
    require(root.is_dir(), f"directory does not exist: {root}")
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = check_path(Path(directory) / name)
            require(path.is_dir() or path.is_file(), f"not a regular file or directory: {path}")


def output_path(workspace, job, create=False):
    workspace = check_path(Path(workspace))
    require(workspace.is_dir(), f"workspace does not exist: {workspace}")
    require(
        isinstance(job, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", job) is not None,
        "job must contain 1-64 ASCII letters, digits, hyphens or underscores, starting with a letter or digit",
    )
    require(
        job.split(".")[0].upper()
        not in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))},
        "reserved Windows job ID",
    )
    parent = within(workspace, workspace / "local" / "reconstruction")
    if create:
        for directory in (workspace / "local", parent):
            check_path(directory)
            directory.mkdir(exist_ok=True)
            check_path(directory)
    return workspace, parent / job


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=4, allow_nan=False) + "\n").encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identifier(*parts):
    return digest(encoded(parts))


def write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    check_path(path)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def copy_image(source, target, expected_hash):
    check_path(source)
    result = hashlib.sha256()
    with source.open("rb") as reader, target.open("xb") as writer:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            result.update(block)
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
    require(result.hexdigest() == expected_hash, f"source changed during copy: {source}")


def snapshot_bundle(workspace, bundle, staging):
    root = within(workspace, Path(bundle))
    require(not staging.is_relative_to(root), "output staging cannot be inside an input bundle")
    check_tree(root)
    validated = validate_bundle(root)
    manifest = validated["manifest"]
    bundle_data = (root / "bundle.json").read_bytes()
    require(load_json(bundle_data, "bundle.json") == manifest, "bundle changed after validation")
    source_id = digest(bundle_data)
    prefix = f"sources/{source_id}"
    metadata = {"bundle.json": bundle_data}
    for entry in manifest["files"]:
        if entry["uri"].endswith((".json", ".jsonl")):
            metadata[entry["uri"]] = read_verified(safe_path(root, entry["uri"]), entry["sha256"], True)
    require(not (staging / prefix).exists(), "duplicate frozen bundle; supply each bundle only once")
    for uri, data in metadata.items():
        write_bytes(staging / prefix / uri, data)
    return (
        root,
        {
            "source_id": source_id,
            "bundle_id": manifest["bundle_id"],
            "session_id": manifest["session_id"],
            "bundle_uri": f"{prefix}/bundle.json",
            "bundle_sha256": source_id,
            "source": manifest["source"],
        },
        metadata,
    )


def collect_inputs(workspace, bundles, staging):
    sources, frames, groups = [], {}, {}
    sessions, bundle_ids = {}, {}
    compatibility = None
    roots = []
    (staging / "images").mkdir()
    for bundle in bundles:
        root, source, metadata = snapshot_bundle(workspace, bundle, staging)
        source_id = source["source_id"]
        require(source["bundle_id"] not in bundle_ids, "duplicate bundle_id; supply each frozen bundle only once")
        bundle_ids[source["bundle_id"]] = source_id
        session = load_json(metadata["session.json"], "session.json")
        plan = load_json(metadata["plan.json"], "plan.json")
        current = compatibility_for(source, session, plan)
        require(compatibility is None or current == compatibility, "incompatible region, map coordinates or source")
        compatibility = current
        session_id = session["session_id"]
        require(session_id not in sessions or sessions[session_id] == session, "conflicting immutable session metadata")
        sessions[session_id] = session
        calibrations = {group["calibration_group_id"]: group for group in session["calibration_groups"]}
        for frame in read_jsonl(metadata["frames.jsonl"], "frames.jsonl"):
            key = (session_id, frame["frame_id"])
            origin = {"source_id": source_id, "frame_id": frame["frame_id"]}
            if key in frames:
                require(frames[key]["frame"] == frame, f"conflicting frame identity: {key}")
                frames[key]["origins"].append(origin)
                continue
            image = frame["image"]
            selected = image["status"] == "captured" and frame["quality"]["selection_status"] != "rejected"
            record = {"frame": frame, "origins": [origin], "image_uri": None, "camera_group_id": None}
            if selected:
                suffix = Path(image["uri"]).suffix.lower()
                require(suffix in IMAGE_SUFFIXES, f"unsupported raw image suffix: {suffix!r}")
                image_uri = f"images/{identifier(*key)}{suffix}"
                copy_image(safe_path(root, image["uri"]), staging / image_uri, image["sha256"])
                group_id = "cal-" + identifier(session_id, frame["calibration_group_id"])
                if group_id not in groups:
                    groups[group_id] = {
                        "camera_group_id": group_id,
                        "session_id": session_id,
                        "calibration": calibrations[frame["calibration_group_id"]],
                        "image_uris": [],
                    }
                groups[group_id]["image_uris"].append(image_uri)
                record.update(image_uri=image_uri, camera_group_id=group_id)
            frames[key] = record
        roots.append((root, source_id))
        sources.append(source)
    require(any(record["image_uri"] for record in frames.values()), "no eligible captured images in input bundles")
    for root, source_id in roots:
        check_tree(root)
        validate_bundle(root)
        require(digest((root / "bundle.json").read_bytes()) == source_id, "source bundle changed during preparation")
    write_bytes(
        staging / "frames.jsonl",
        b"".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
            for _, record in sorted(frames.items())
        ),
    )
    for group in groups.values():
        group["image_uris"].sort()
    write_bytes(
        staging / "calibration-groups.json",
        encoded(
            {
                "mapping_status": "requires_explicit_mapping",
                "groups": sorted(groups.values(), key=lambda group: group["camera_group_id"]),
            }
        ),
    )
    return sources, compatibility, sum(record["image_uri"] is not None for record in frames.values())


def inventory(root):
    check_tree(root)
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != root / "preparation.json":
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            entries.append(
                {"uri": path.relative_to(root).as_posix(), "sha256": hasher.hexdigest(), "bytes": path.stat().st_size}
            )
    return entries


def check_snapshot(root, source, validators):
    """Check copied capture metadata; excluded image bytes intentionally stay outside this job."""
    source_id = source.get("source_id")
    require(isinstance(source_id, str) and re.fullmatch(r"[0-9a-f]{64}", source_id), "invalid source ID")
    prefix = f"sources/{source_id}"
    require(
        source.get("bundle_uri") == f"{prefix}/bundle.json" and source.get("bundle_sha256") == source_id,
        "invalid source bundle reference",
    )
    data = read_verified(safe_path(root, source["bundle_uri"]), source_id, True)
    original = load_json(data, source["bundle_uri"])
    check_record(validators, "bundle", original, "source bundle")
    require(
        all(original[key] == source.get(key) for key in ("source", "bundle_id", "session_id")),
        "source provenance mismatch",
    )
    files = file_index(original["files"], "source bundle")
    require("bundle.json" not in files, "source bundle cannot hash itself")
    metadata = {}
    for uri, sha256 in files.items():
        # Validate even excluded image URIs without requiring their bytes in this prepared input.
        parts = uri.split("/")
        require(
            all(part not in ("", ".", "..") for part in parts)
            and not any(character in uri for character in ("\\", ":", "%", "?", "#"))
            and not any(ord(character) < 32 for character in uri),
            "unsafe source file URI",
        )
        if uri.endswith((".json", ".jsonl")):
            metadata[uri] = read_verified(safe_path(root, f"{prefix}/{uri}"), sha256, True)
            if uri.endswith(".jsonl"):
                read_jsonl(metadata[uri], uri)
    session = load_json(metadata["session.json"], "source session")
    plan = load_json(metadata["plan.json"], "source plan")
    check_record(validators, "session", session, "source session")
    check_record(validators, "plan", plan, "source plan")
    try:
        datetime.fromisoformat(session["clock"]["utc_anchor"].replace("Z", "+00:00"))
    except ValueError as error:
        raise BundleValidationError(f"invalid source UTC anchor: {error}") from error
    require(original["session_id"] == session["session_id"] == plan["session_id"], "source session mismatch")
    require(original["source"] == session["source"], "source session provenance mismatch")
    require(
        (session["source"]["kind"] == "synthetic") == (session["controller_kind"] == "synthetic"),
        "synthetic source/controller mismatch",
    )
    frames = read_frames(metadata["frames.jsonl"], validators, "source frames")
    check_frames(frames, session, plan, files)
    batches, referenced = set(), set()
    for uri in original["batch_uris"]:
        require(uri.startswith("batches/") and uri.endswith(".json"), "invalid batch URI")
        batch = load_json(metadata[uri], uri)
        check_record(validators, "batch", batch, uri)
        require(batch["batch_id"] not in batches, "duplicate source batch ID")
        batches.add(batch["batch_id"])
        require(
            batch["session_id"] == session["session_id"]
            and batch["region_id"] == plan["region_id"]
            and batch["input_revision"] == plan["revision"],
            "source batch identity mismatch",
        )
        require(
            batch["session_identity"]
            == {key: session[key] for key in ("session_id", "clock", "source", "controller_kind")},
            "immutable session identity mismatch",
        )
        shard_uri = batch["frames_uri"]
        require(shard_uri.startswith("records/") and shard_uri.endswith(".jsonl"), "invalid source frame shard")
        shard = read_frames(metadata[shard_uri], validators, shard_uri)
        require(
            set(shard) == set(batch["frame_ids"]) and all(frames.get(key) == frame for key, frame in shard.items()),
            "immutable source frames mismatch",
        )
        referenced.update(shard)
        expected = {"plan.json", shard_uri} | {
            frame["image"]["uri"] for frame in shard.values() if frame["image"]["status"] == "captured"
        }
        batch_files = file_index(batch["files"], uri)
        require(
            set(batch_files) == expected and all(files.get(uri) == sha for uri, sha in batch_files.items()),
            "source batch file hashes mismatch",
        )
        group_ids = {frame["calibration_group_id"] for frame in shard.values()}
        expected_groups = {
            group["calibration_group_id"]: group
            for group in session["calibration_groups"]
            if group["calibration_group_id"] in group_ids
        }
        actual_groups = {group["calibration_group_id"]: group for group in batch["calibration_groups"]}
        require(
            len(actual_groups) == len(batch["calibration_groups"]) and actual_groups == expected_groups,
            "immutable source calibration mismatch",
        )
    require(referenced == set(frames), "source frames outside committed batches")
    require(
        {uri for uri in files if uri.startswith("batches/")} == set(original["batch_uris"]), "unlisted source batch"
    )
    return (
        session,
        plan,
        {(frame["session_id"], key): frame for key, frame in frames.items()},
        {f"{prefix}/{uri}" for uri in metadata} | {f"{prefix}/bundle.json"},
    )


def compatibility_for(source, session, plan):
    return {
        "region_id": plan["region_id"],
        "map_coordinates": plan["map_coordinates"],
        "source": {key: value for key, value in source["source"].items() if key != "description"},
        "controller_kind": session["controller_kind"],
    }


def verify_directory(root):
    try:
        return _verify_directory(root)
    except (KeyError, TypeError, AttributeError) as error:
        raise BundleValidationError(f"malformed prepared input: {error}") from error


def _verify_directory(root):
    """Verify a prepared input's byte inventory and source/frame/group references."""
    check_tree(root)
    manifest = load_json(safe_path(root, "preparation.json").read_bytes(), "preparation.json")
    require(isinstance(manifest, dict) and manifest.get("format") == FORMAT, "unsupported preparation format")
    require(manifest.get("generator") == GENERATOR, "unsupported preparation generator")
    require(manifest.get("state") == STATE, "preparation must remain explicitly unreconstructed and unassessed")
    require(
        isinstance(manifest.get("files"), list) and manifest["files"] == inventory(root),
        "prepared files inventory/hash mismatch",
    )
    require(all(entry["uri"] != "preparation.json" for entry in manifest["files"]), "manifest cannot hash itself")
    records = read_jsonl(safe_path(root, "frames.jsonl").read_bytes(), "frames.jsonl")
    calibration = load_json(safe_path(root, "calibration-groups.json").read_bytes(), "calibration-groups.json")
    require(
        isinstance(calibration, dict) and calibration.get("mapping_status") == "requires_explicit_mapping",
        "explicit camera group mapping is required",
    )
    sources = manifest.get("sources")
    require(isinstance(sources, list) and bool(sources), "missing provenance")
    source_frames, source_sessions, sessions = {}, {}, {}
    expected_files = {"frames.jsonl", "calibration-groups.json"}
    compatibility, bundle_ids = None, set()
    validators = make_validators()
    for source in sources:
        require(isinstance(source, dict), "invalid provenance entry")
        session, plan, raw_frames, metadata_files = check_snapshot(root, source, validators)
        source_id = source["source_id"]
        require(source_id not in source_frames and source["bundle_id"] not in bundle_ids, "duplicate source identity")
        bundle_ids.add(source["bundle_id"])
        current = compatibility_for(source, session, plan)
        require(compatibility is None or compatibility == current, "incompatible region, map coordinates or source")
        compatibility = current
        session_id = session["session_id"]
        require(session_id not in sessions or sessions[session_id] == session, "conflicting immutable session metadata")
        sessions[session_id] = session
        source_sessions[source_id] = session
        source_frames[source_id] = raw_frames
        expected_files.update(metadata_files)
    require(manifest.get("compatibility") == compatibility, "compatibility manifest mismatch")
    groups = {}
    require(isinstance(calibration.get("groups"), list), "invalid calibration groups")
    for group in calibration["groups"]:
        require(isinstance(group, dict) and isinstance(group.get("camera_group_id"), str), "invalid calibration group")
        require(group["camera_group_id"] not in groups, "duplicate calibration group")
        groups[group["camera_group_id"]] = group
    seen, images, used_groups, covered = set(), set(), {}, set()
    for record in records:
        require(isinstance(record, dict) and isinstance(record.get("frame"), dict), "invalid frame mapping")
        frame = record["frame"]
        key = (frame.get("session_id"), frame.get("frame_id"))
        require(all(isinstance(part, str) for part in key) and key not in seen, "duplicate or invalid frame identity")
        seen.add(key)
        require(isinstance(record.get("origins"), list) and bool(record["origins"]), "missing frame origin")
        for origin in record["origins"]:
            require(isinstance(origin, dict) and origin.get("source_id") in source_frames, "unknown frame source")
            require(
                origin.get("frame_id") == key[1] and source_frames[origin["source_id"]].get(key) == frame,
                "frame mapping differs from source snapshot",
            )
            covered.add((origin["source_id"], key))
        image = frame["image"]
        selected = image["status"] == "captured" and frame["quality"]["selection_status"] != "rejected"
        if selected:
            suffix = Path(image["uri"]).suffix.lower()
            require(suffix in IMAGE_SUFFIXES, "unsupported image suffix")
            image_uri = f"images/{identifier(*key)}{suffix}"
            require(
                record.get("image_uri") == image_uri and image_uri not in images, "invalid prepared image reference"
            )
            read_verified(safe_path(root, image_uri), image["sha256"], False)
            images.add(image_uri)
            group_id = "cal-" + identifier(key[0], frame["calibration_group_id"])
            require(record.get("camera_group_id") == group_id and group_id in groups, "invalid camera group mapping")
            group = groups[group_id]
            source_id = record["origins"][0]["source_id"]
            session = source_sessions[source_id]
            expected = next(
                (
                    item
                    for item in session["calibration_groups"]
                    if item["calibration_group_id"] == frame["calibration_group_id"]
                ),
                None,
            )
            require(
                group.get("session_id") == key[0] and group.get("calibration") == expected,
                "calibration group differs from source",
            )
            used_groups.setdefault(group_id, []).append(image_uri)
        else:
            require(
                record.get("image_uri") is None and record.get("camera_group_id") is None, "excluded frame has image"
            )
    require(
        covered == {(source_id, key) for source_id, frames in source_frames.items() for key in frames},
        "source frame missing from mapping",
    )
    require(
        set(groups) == set(used_groups)
        and all(group["image_uris"] == sorted(used_groups[group_id]) for group_id, group in groups.items()),
        "group image membership mismatch",
    )
    actual_images = {entry["uri"] for entry in manifest["files"] if entry["uri"].startswith("images/")}
    require(
        images == actual_images and bool(images) and manifest.get("image_count") == len(images), "image count mismatch"
    )
    require({entry["uri"] for entry in manifest["files"]} == expected_files | images, "unexpected prepared files")
    return manifest


def verify(workspace, job):
    _, output = output_path(workspace, job)
    manifest = verify_directory(output)
    require(manifest.get("job_id") == job, "job ID mismatch")
    return manifest


def prepare(workspace, bundles, job):
    workspace, output = output_path(workspace, job, create=True)
    require(bool(bundles), "at least one frozen bundle is required")
    lock = check_path(output.with_name(f".{job}.lock"))
    lock.mkdir()  # Exclusive directory creation also works on Windows.
    staging = output.with_name(f".{job}.{uuid.uuid4().hex}.partial")
    try:
        staging.mkdir()
        require(not output.exists(), f"job already exists: {output}")
        sources, compatibility, image_count = collect_inputs(workspace, bundles, staging)
        manifest = {
            "format": FORMAT,
            "generator": GENERATOR,
            "job_id": job,
            "state": STATE,
            "compatibility": compatibility,
            "sources": sources,
            "image_count": image_count,
            "files": inventory(staging),
        }
        write_bytes(staging / "preparation.json", encoded(manifest))
        verify_directory(staging)
        for path in staging.rglob("*"):
            if path.is_file():
                path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        check_path(output)
        require(not output.exists(), f"job already exists: {output}")
        staging.rename(output)
        return manifest
    except (Exception, KeyboardInterrupt) as error:
        if staging.is_dir():
            with suppress(OSError):
                write_bytes(
                    staging / "failure.json",
                    encoded(
                        {
                            "status": "cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                            "error": str(error) or type(error).__name__,
                            "state": STATE,
                        }
                    ),
                )
        error.preparation_diagnostics = str(staging)
        raise
    finally:
        lock.rmdir()
