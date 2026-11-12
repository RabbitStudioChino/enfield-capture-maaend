"""Validate a frozen CaptureBundle without reading an active capture session."""

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "capture"
SCHEMA_BASE = "https://schemas.endfield.local/capture/1.0/"


class BundleValidationError(ValueError):
    """The snapshot cannot be consumed as reconstruction input."""


def require(condition, message):
    if not condition:
        raise BundleValidationError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_json(data, label):
    try:
        return json.loads(
            data,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
            parse_int=finite_integer,
        )
    except (ValueError, UnicodeError) as error:
        raise BundleValidationError(f"{label}: {error}") from error


def reject_constant(value):
    raise ValueError(f"non-finite JSON number {value}")


def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        reject_constant(value)
    return result


def finite_integer(value):
    result = int(value)
    if abs(result) > sys.float_info.max:
        raise ValueError(f"JSON integer outside finite IEEE-754 range {value}")
    return result


def make_validators():
    schemas = {}
    for path in sorted(CONTRACT_DIR.glob("*.schema.json")):
        schema = load_json(path.read_bytes(), path.name)
        Draft202012Validator.check_schema(schema)
        schemas[path.stem.removesuffix(".schema")] = schema
    registry = Registry().with_resources((schema["$id"], Resource.from_contents(schema)) for schema in schemas.values())
    return {
        name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
        for name, schema in schemas.items()
    }


def check_record(validators, kind, record, label):
    version = record.get("schema_version") if isinstance(record, dict) else None
    expected = f"capture.{kind}/1.0"
    supported = {expected: kind}
    if kind == "plan":
        supported["capture.plan/1.1"] = "plan-manual"
    if not isinstance(version, str) or version not in supported:
        match = re.fullmatch(rf"capture\.{kind}/([0-9]+)\.([0-9]+)", str(version))
        reason = "unknown major" if match and match[1] != "1" else "unsupported schema_version"
        raise BundleValidationError(f"{label}: {reason} {version!r}, expected {', '.join(supported)}")
    error = next(validators[supported[version]].iter_errors(record), None)
    if error:
        location = "/".join(str(part) for part in error.absolute_path)
        raise BundleValidationError(f"{label}/{location}: {error.message}")


def safe_path(root, uri):
    require(isinstance(uri, str) and bool(uri), "file URI must be a nonempty relative path")
    parts = uri.split("/")
    require(
        all(part not in ("", ".", "..") for part in parts)
        and not any(character in uri for character in ("\\", ":", "%", "?", "#"))
        and not any(ord(character) < 32 for character in uri)
        and not PurePosixPath(uri).is_absolute(),
        f"unsafe relative path {uri!r}",
    )
    current = root
    for part in parts:
        current /= part
        require(not current.is_symlink(), f"symlink is forbidden in bundle path {uri!r}")
    require(current.is_file(), f"missing regular file {uri!r}")
    return current


def file_index(entries, label):
    result = {}
    for entry in entries:
        uri = entry["uri"]
        require(uri not in result, f"{label}: duplicate file URI {uri!r}")
        result[uri] = entry["sha256"]
    return result


def read_jsonl(data, label):
    require(not data or data.endswith(b"\n"), f"{label}: missing final newline, possible partial write")
    records = []
    for line_number, line in enumerate(data.splitlines(), 1):
        line_label = f"{label} line {line_number}"
        require(bool(line.strip()), f"{line_label}: blank record")
        records.append(load_json(line, line_label))
    return records


def read_frames(data, validators, label="frames.jsonl"):
    frames = {}
    for frame in read_jsonl(data, label):
        check_record(validators, "frame", frame, label)
        frame_id = frame["frame_id"]
        require(frame_id not in frames, f"{label}: duplicate frame_id {frame_id}")
        frames[frame_id] = frame
    require(bool(frames), f"{label}: no frame records")
    return frames


def read_verified(path, expected_hash, keep_content):
    digest = hashlib.sha256()
    content = bytearray()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            if keep_content:
                content.extend(block)
    require(digest.hexdigest() == expected_hash, f"{path.name}: SHA-256 mismatch")
    return bytes(content)


def check_frames(frames, session, plan, files):
    groups = {}
    for group in session["calibration_groups"]:
        group_id = group["calibration_group_id"]
        require(group_id not in groups, f"duplicate calibration_group_id {group_id}")
        groups[group_id] = group
    waypoints = {}
    for waypoint in plan["waypoints"]:
        waypoint_id = waypoint["waypoint_id"]
        require(waypoint_id not in waypoints, f"duplicate waypoint_id {waypoint_id}")
        waypoints[waypoint_id] = waypoint
    previous_request = int(session["clock"]["monotonic_anchor_ns"])
    for frame_id, frame in frames.items():
        label = f"frame {frame_id}"
        require(frame["session_id"] == session["session_id"], f"{label}: session_id mismatch")
        timing = frame["timing"]
        require(timing["clock_id"] == session["clock"]["clock_id"], f"{label}: clock_id mismatch")
        request = int(timing["request_monotonic_ns"])
        complete = int(timing["complete_monotonic_ns"])
        locate = timing["locate_complete_monotonic_ns"]
        require(previous_request <= request <= complete, f"{label}: monotonic time order invalid")
        require(locate is None or complete <= int(locate), f"{label}: locate time precedes capture completion")
        previous_request = request
        require(frame["waypoint_id"] in waypoints, f"{label}: unknown waypoint_id")
        require(
            frame["segment_id"] == waypoints[frame["waypoint_id"]]["segment_id"],
            f"{label}: segment_id does not match waypoint",
        )
        require(frame["calibration_group_id"] in groups, f"{label}: unknown calibration_group_id")
        navigation = frame["navigation_observation"]
        require(navigation["region_id"] == plan["region_id"], f"{label}: navigation region mismatch")
        require(
            navigation["coordinate_frame_id"] == plan["map_coordinates"]["coordinate_frame_id"],
            f"{label}: navigation coordinate frame mismatch",
        )
        observations = [navigation[key] for key in ("position_xy", "floor_height", "character_heading", "camera_yaw")]
        if plan["schema_version"] == "capture.plan/1.1":
            require(
                locate is None and all(item["status"] == "not_run" and item["value"] is None for item in observations),
                f"{label}: manual capture cannot claim navigation observations",
            )
        if locate is None:
            require(all(item["status"] == "not_run" for item in observations), f"{label}: recognition has no time")
        image = frame["image"]
        if image["status"] == "captured":
            require(image["uri"].startswith("images/raw/"), f"{label}: original image must be in images/raw/")
            require(files.get(image["uri"]) == image["sha256"], f"{label}: image absent or hash mismatch")
            group = groups[frame["calibration_group_id"]]
            require(
                (image["width"], image["height"]) == (group["image_width"], group["image_height"]),
                f"{label}: image size disagrees with calibration group",
            )
        else:
            require(locate is None, f"{label}: failed capture cannot have recognition time")
            require(
                all(item["status"] == "not_run" for item in observations), f"{label}: failed capture has observations"
            )
            require(frame["quality"]["selection_status"] == "rejected", f"{label}: failed capture must be rejected")
        if frame["quality"]["selection_status"] == "rejected":
            require(bool(frame["quality"]["rejection_reasons"]), f"{label}: rejected frame has no reason")


def check_automatic_plan(automatic, candidates, plan, validators):
    from .rescan import candidate_catalog

    check_record(validators, "auto-plan", automatic, "auto-plan.json")
    check_record(validators, "rescan-candidates", candidates, "rescan-candidates.json")
    require(candidates == candidate_catalog(automatic), "candidate directory differs from frozen automatic plan")
    require(plan.get("auto_plan_id") == automatic["plan_id"], "session automatic plan identity mismatch")
    require(plan["region_id"] == candidates["region_id"], "automatic plan region mismatch")
    require(plan["map_coordinates"] == automatic["map_coordinates"], "automatic plan coordinate frame mismatch")
    originals = {}
    for index, point in enumerate(automatic["waypoints"]):
        require(point["waypoint_id"] == f"point-{index + 1:05d}", "automatic waypoint IDs are not sequential")
        originals[point["waypoint_id"]] = point
    candidate_ids = {
        point: candidate["candidate_id"]
        for candidate in candidates["candidates"]
        for point in candidate["source_waypoint_ids"]
    }
    for waypoint in plan["waypoints"]:
        original_id = waypoint.get("auto_plan_waypoint_id")
        require(original_id == waypoint["waypoint_id"] and original_id in originals, "unknown source waypoint")
        original = originals[original_id]
        for key in ("position_xy", "target_deck_y", "segment_id"):
            require(waypoint.get(key) == original[key], f"automatic waypoint {key} changed")
        require(waypoint.get("candidate_id") == candidate_ids[original_id], "automatic candidate identity changed")
        require(
            original.get("candidate_id", candidate_ids[original_id]) == candidate_ids[original_id],
            "source automatic candidate identity is invalid",
        )
        intent = waypoint["camera_intent"]
        require(
            intent == {"yaw_degrees": original["yaw_degrees"], "pitch_degrees": None, "settle_required": True},
            "automatic camera intent changed",
        )
        for key in ("rescan_action_id", "rescan_issue_id"):
            require(waypoint.get(key) == original.get(key), "rescan waypoint mapping changed")
    request = automatic.get("rescan_request")
    require((request is None) == (automatic.get("rescan_source") is None), "incomplete rescan source binding")
    if request is not None:
        require(len(request["actions"]) == len(automatic["waypoints"]), "rescan action coverage mismatch")
        for point, action in zip(automatic["waypoints"], request["actions"], strict=True):
            require(
                point.get("rescan_action_id") == action["action_id"]
                and point.get("rescan_issue_id") == action["issue_id"]
                and point.get("candidate_id") == action["target_candidate_id"]
                and point["yaw_degrees"] == action["heading_degrees"],
                "rescan action differs from the resolved waypoint",
            )


def validate_bundle(directory):
    """Return manifest and counts, or raise BundleValidationError. Reads only."""
    root = Path(directory).absolute()
    require(not root.is_symlink(), "bundle root must not be a symlink")
    require(root.is_dir(), f"bundle directory does not exist: {root}")
    validators = make_validators()
    manifest = load_json(safe_path(root, "bundle.json").read_bytes(), "bundle.json")
    check_record(validators, "bundle", manifest, "bundle.json")
    files = file_index(manifest["files"], "bundle.json")
    require("bundle.json" not in files, "bundle.json cannot hash itself")
    contents = {}
    for uri, expected_hash in files.items():
        is_metadata = uri.endswith((".json", ".jsonl"))
        content = read_verified(safe_path(root, uri), expected_hash, is_metadata)
        if is_metadata:
            contents[uri] = content
        if uri.endswith(".jsonl"):
            read_jsonl(content, uri)
    actual_files = set()
    for directory_path, directory_names, file_names in os.walk(root, followlinks=False):
        for name in directory_names + file_names:
            path = Path(directory_path) / name
            require(not path.is_symlink(), f"symlink is forbidden: {path.relative_to(root)}")
        actual_files.update((Path(directory_path) / name).relative_to(root).as_posix() for name in file_names)
    require(actual_files == set(files) | {"bundle.json"}, "bundle files index does not match directory contents")
    for required_uri in ("session.json", "plan.json", "frames.jsonl", *manifest["batch_uris"]):
        require(required_uri in contents, f"bundle file index missing {required_uri}")
    session = load_json(contents["session.json"], "session.json")
    plan = load_json(contents["plan.json"], "plan.json")
    check_record(validators, "session", session, "session.json")
    check_record(validators, "plan", plan, "plan.json")
    try:
        datetime.fromisoformat(session["clock"]["utc_anchor"].replace("Z", "+00:00"))
    except ValueError as error:
        raise BundleValidationError(f"session.json: invalid UTC anchor: {error}") from error
    require(manifest["session_id"] == session["session_id"] == plan["session_id"], "session_id mismatch")
    require(manifest["source"] == session["source"], "bundle and session provenance mismatch")
    require(
        (session["source"]["kind"] == "synthetic") == (session["controller_kind"] == "synthetic"),
        "synthetic source/controller mismatch",
    )
    frames = read_frames(contents["frames.jsonl"], validators)
    check_frames(frames, session, plan, files)
    automatic_uris = {"auto-plan.json", "rescan-candidates.json"}
    if automatic_uris.intersection(files) or manifest.get("auto_plan_uri") is not None:
        require(automatic_uris <= files.keys(), "incomplete frozen automatic planning metadata")
        require(
            manifest.get("auto_plan_uri") == "auto-plan.json"
            and manifest.get("rescan_candidates_uri") == "rescan-candidates.json",
            "automatic planning metadata is not declared by the manifest",
        )
        automatic = load_json(contents["auto-plan.json"], "auto-plan.json")
        candidates = load_json(contents["rescan-candidates.json"], "rescan-candidates.json")
        check_automatic_plan(automatic, candidates, plan, validators)
        if automatic.get("rescan_request") is not None:
            require(manifest.get("rescan_links_uri") == "rescan-links.json", "missing rescan frame links")
            links = load_json(contents.get("rescan-links.json", b""), "rescan-links.json")
            check_record(validators, "rescan-links", links, "rescan-links.json")
            require(links["request"] == automatic["rescan_request"], "rescan request changed in frame links")
            actions = {item["action_id"]: item for item in links["request"]["actions"]}
            points = {item["waypoint_id"]: item for item in plan["waypoints"]}
            expected = [
                {
                    "session_id": session["session_id"],
                    "frame_id": frame["frame_id"],
                    "waypoint_id": frame["waypoint_id"],
                    "image_uri": frame["image"]["uri"],
                    "image_sha256": frame["image"]["sha256"],
                    "action": actions[points[frame["waypoint_id"]]["rescan_action_id"]],
                }
                for frame in frames.values()
            ]
            require(links["frames"] == expected, "rescan issue/action/image links changed")
    else:
        require(not manifest.get("rescan_candidates_uri"), "candidate directory requires frozen automatic plan")
        require(not manifest.get("rescan_links_uri"), "rescan links require frozen automatic plan")
    batch_ids = set()
    referenced_frames = set()
    for uri in manifest["batch_uris"]:
        require(uri.startswith("batches/") and uri.endswith(".json"), f"invalid batch URI {uri}")
        batch = load_json(contents[uri], uri)
        check_record(validators, "batch", batch, uri)
        require(batch["batch_id"] not in batch_ids, f"duplicate batch_id {batch['batch_id']}")
        batch_ids.add(batch["batch_id"])
        require(batch["session_id"] == session["session_id"], f"{uri}: session_id mismatch")
        require(batch["region_id"] == plan["region_id"], f"{uri}: region_id mismatch")
        require(batch["input_revision"] == plan["revision"], f"{uri}: plan revision mismatch")
        identity = {key: session[key] for key in ("session_id", "clock", "source", "controller_kind")}
        require(batch["session_identity"] == identity, f"{uri}: immutable session identity mismatch")
        frames_uri = batch["frames_uri"]
        require(frames_uri.startswith("records/") and frames_uri.endswith(".jsonl"), f"{uri}: invalid frames_uri")
        require(frames_uri in contents, f"{uri}: missing immutable frame records")
        batch_frames = read_frames(contents[frames_uri], validators, frames_uri)
        require(set(batch_frames) == set(batch["frame_ids"]), f"{uri}: frame_ids disagree with immutable records")
        expected_uris = {"plan.json", frames_uri}
        calibration_ids = set()
        for frame_id in batch["frame_ids"]:
            require(frame_id in frames, f"{uri}: unknown frame_id {frame_id}")
            require(batch_frames[frame_id] == frames[frame_id], f"{uri}: aggregate and immutable frame differ")
            referenced_frames.add(frame_id)
            calibration_ids.add(frames[frame_id]["calibration_group_id"])
            image = frames[frame_id]["image"]
            if image["status"] == "captured":
                expected_uris.add(image["uri"])
        batch_files = file_index(batch["files"], uri)
        require(set(batch_files) == expected_uris, f"{uri}: batch files do not cover exactly its immutable inputs")
        require(all(files.get(path) == digest for path, digest in batch_files.items()), f"{uri}: batch hash mismatch")
        expected_calibrations = {
            group["calibration_group_id"]: group
            for group in session["calibration_groups"]
            if group["calibration_group_id"] in calibration_ids
        }
        actual_calibrations = {}
        for group in batch["calibration_groups"]:
            group_id = group["calibration_group_id"]
            require(group_id not in actual_calibrations, f"{uri}: duplicate calibration_group_id")
            actual_calibrations[group_id] = group
        require(actual_calibrations == expected_calibrations, f"{uri}: immutable calibration snapshot mismatch")
    require(referenced_frames == set(frames), "frames.jsonl contains frames outside committed batches")
    indexed_batches = {uri for uri in files if uri.startswith("batches/")}
    require(indexed_batches == set(manifest["batch_uris"]), "unlisted batch in bundle")
    return {"manifest": manifest, "frame_count": len(frames), "batch_count": len(batch_ids), "file_count": len(files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="frozen bundle directory containing bundle.json")
    args = parser.parse_args(argv)
    try:
        result = validate_bundle(args.directory)
    except (BundleValidationError, OSError) as error:
        print(f"INVALID: {error}", file=sys.stderr)
        return 1
    print(f"VALID {result['frame_count']} frames, {result['batch_count']} batches, {result['file_count']} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
