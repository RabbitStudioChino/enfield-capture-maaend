import copy
import hashlib
import io
import json
import shutil
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools.capture.validate import BundleValidationError, check_record, main, make_validators, validate_bundle

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "contracts/capture/fixtures/synthetic-minimal"
TEST_TEMP_ROOT = REPO_ROOT / ".cache/capture-tests"
BATCH_URI = "batches/synthetic-batch-001.json"
SHARD_URI = "records/synthetic-batch-001.frames.jsonl"


def read_json(root, uri):
    return json.loads((root / uri).read_bytes())


def write_json(root, uri, record):
    (root / uri).write_text(json.dumps(record, indent=4) + "\n", encoding="utf-8")


def read_frames(root):
    return [json.loads(line) for line in (root / "frames.jsonl").read_bytes().splitlines()]


def write_frames(root, frames):
    data = "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames)
    for uri in ("frames.jsonl", SHARD_URI):
        (root / uri).write_text(data, encoding="utf-8")


def reseal(root):
    """Update existing hash entries after a test edits metadata."""
    for uri in (BATCH_URI, "bundle.json"):
        record = read_json(root, uri)
        for entry in record["files"]:
            entry["sha256"] = hashlib.sha256((root / entry["uri"]).read_bytes()).hexdigest()
        write_json(root, uri, record)


class ValidateBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    def bundle(self):
        temporary = tempfile.TemporaryDirectory(prefix="bundle-", dir=TEST_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        return Path(shutil.copytree(FIXTURE, Path(temporary.name) / "bundle"))

    def test_minimal_fixture_and_cli(self):
        result = validate_bundle(FIXTURE)
        self.assertEqual((result["frame_count"], result["batch_count"], result["file_count"]), (2, 1, 6))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([str(FIXTURE)]), 0)
        self.assertIn("VALID 2 frames", output.getvalue())

    def test_fixture_contains_actual_self_authored_rgb_png(self):
        data = (FIXTURE / "images/raw/frame-000001.png").read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        offset = 8
        compressed = b""
        while offset < len(data):
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            kind = data[offset + 4 : offset + 8]
            payload = data[offset + 8 : offset + 8 + length]
            crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
            self.assertEqual(crc, zlib.crc32(kind + payload) & 0xFFFFFFFF)
            if kind == b"IHDR":
                self.assertEqual(struct.unpack(">IIBBBBB", payload), (16, 12, 8, 2, 0, 0, 0))
            if kind == b"IDAT":
                compressed += payload
            offset += length + 12
        expected = b"".join(
            b"\x00" + b"".join(bytes((x * 16, y * 20, (x + y) * 9)) for x in range(16)) for y in range(12)
        )
        self.assertEqual(zlib.decompress(compressed), expected)

    def test_navigation_and_headings_are_independent_from_camera_truth(self):
        root = self.bundle()
        frames = read_frames(root)
        frames[0]["navigation_observation"]["camera_yaw"] = {"status": "available", "value": 42}
        write_frames(root, frames)
        reseal(root)
        self.assertEqual(validate_bundle(root)["frame_count"], 2)
        self.assertIsNone(frames[0]["camera_estimate_ref"])

    def test_schema_versions_and_extensions(self):
        validators = make_validators()
        records = {
            "bundle": read_json(FIXTURE, "bundle.json"),
            "session": read_json(FIXTURE, "session.json"),
            "plan": read_json(FIXTURE, "plan.json"),
            "batch": read_json(FIXTURE, BATCH_URI),
            "frame": read_frames(FIXTURE)[0],
        }
        for kind, record in records.items():
            with self.subTest(kind=kind):
                record["producer_note"] = "compatible noncritical extension"
                check_record(validators, kind, record, kind)
                for version, message in (("2.0", "unknown major"), ("1.1", "unsupported schema_version")):
                    record["schema_version"] = f"capture.{kind}/{version}"
                    with self.assertRaisesRegex(BundleValidationError, message):
                        check_record(validators, kind, record, kind)

    def test_corrupt_or_missing_image(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                root = self.bundle()
                image = root / "images/raw/frame-000001.png"
                if missing:
                    image.unlink()
                else:
                    image.write_bytes(b"corrupt")
                with self.assertRaisesRegex(BundleValidationError, "missing regular file|SHA-256 mismatch"):
                    validate_bundle(root)

    def test_unsafe_paths(self):
        for uri in (
            "../outside.png", "/outside.png", "images//x", "./session.json", "C:/x", "images\\x",
            "images/x%20.png", "images/x?query.png", "images/x#fragment.png",
        ):
            with self.subTest(uri=uri):
                root = self.bundle()
                manifest = read_json(root, "bundle.json")
                manifest["files"][0]["uri"] = uri
                write_json(root, "bundle.json", manifest)
                with self.assertRaisesRegex(BundleValidationError, "unsafe relative path"):
                    validate_bundle(root)

    def test_symlink_file_and_directory(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                root = self.bundle()
                if directory:
                    target = root / "images/raw"
                    target.rename(root / "saved-images")
                    target.symlink_to(root / "saved-images", target_is_directory=True)
                else:
                    target = root / "images/raw/frame-000001.png"
                    target.rename(root / "saved-image.png")
                    target.symlink_to(root / "saved-image.png")
                with self.assertRaisesRegex(BundleValidationError, "symlink"):
                    validate_bundle(root)

    def test_unindexed_file_and_active_session_rejected(self):
        root = self.bundle()
        (root / "pending.png").write_bytes(b"in-progress")
        with self.assertRaisesRegex(BundleValidationError, "index does not match"):
            validate_bundle(root)
        (root / "bundle.json").unlink()
        with redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main([str(root)]), 1)
        self.assertIn("bundle.json", output.getvalue())

    def test_partial_jsonl_and_invalid_json(self):
        original = (FIXTURE / "frames.jsonl").read_bytes()
        for data in (original.rstrip(b"\n"), original + b'{"frame_id":\n', original + b"\n"):
            with self.subTest(data=data[-30:]):
                root = self.bundle()
                (root / "frames.jsonl").write_bytes(data)
                reseal(root)
                with self.assertRaises(BundleValidationError):
                    validate_bundle(root)

    def test_duplicate_json_keys_and_nonfinite_numbers(self):
        for raw in (b'{"schema_version":"capture.bundle/1.0","schema_version":"capture.bundle/1.0"}', b'{"x":NaN}'):
            with self.subTest(raw=raw):
                root = self.bundle()
                (root / "bundle.json").write_bytes(raw)
                with self.assertRaisesRegex(BundleValidationError, "duplicate JSON key|non-finite"):
                    validate_bundle(root)

    def test_overflow_number_in_navigation_rejected_end_to_end(self):
        for literal in ("1e400", "1" + "0" * 400):
            with self.subTest(literal=literal[:10]):
                root = self.bundle()
                frames = read_frames(root)
                frames[0]["navigation_observation"]["position_xy"] = {"status": "available", "value": [12345.0, 2]}
                data = "".join(json.dumps(frame) + "\n" for frame in frames).replace("12345.0", literal)
                for uri in ("frames.jsonl", SHARD_URI):
                    (root / uri).write_text(data, encoding="utf-8")
                reseal(root)
                with self.assertRaisesRegex(BundleValidationError, "non-finite JSON number|finite IEEE-754 range"):
                    validate_bundle(root)

    def test_frame_semantic_errors(self):
        cases = (
            ("session_id", "another-session", "session_id mismatch"),
            ("segment_id", "another-segment", "segment_id"),
            ("waypoint_id", "missing-waypoint", "waypoint_id"),
            ("calibration_group_id", "missing-camera", "calibration_group_id"),
            ("camera_estimate_ref", "navigation_observation", "is not of type 'null'"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                root = self.bundle()
                frames = read_frames(root)
                frames[0][field] = value
                write_frames(root, frames)
                reseal(root)
                with self.assertRaisesRegex(BundleValidationError, message):
                    validate_bundle(root)

    def test_invalid_timing(self):
        cases = (
            ("clock_id", "another-clock"),
            ("request_monotonic_ns", "10000000000000021"),
            ("request_monotonic_ns", "1"),
            ("request_monotonic_ns", 10000000000000010),
            ("locate_complete_monotonic_ns", "10000000000000019"),
            ("locate_complete_monotonic_ns", None),
            ("native_render_timestamp", "10000000000000020"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                root = self.bundle()
                frames = read_frames(root)
                frames[0]["timing"][field] = value
                write_frames(root, frames)
                reseal(root)
                with self.assertRaises(BundleValidationError):
                    validate_bundle(root)

    def test_navigation_status_and_image_metadata(self):
        for mode in ("available_without_value", "value_with_failed_status", "wrong_dimensions", "image_on_failure"):
            with self.subTest(mode=mode):
                root = self.bundle()
                frames = read_frames(root)
                if mode == "available_without_value":
                    frames[0]["navigation_observation"]["position_xy"]["status"] = "available"
                elif mode == "value_with_failed_status":
                    frames[0]["navigation_observation"]["camera_yaw"] = {"status": "failed", "value": 0}
                elif mode == "wrong_dimensions":
                    frames[0]["image"]["width"] += 1
                else:
                    frames[1]["image"]["uri"] = frames[0]["image"]["uri"]
                write_frames(root, frames)
                reseal(root)
                with self.assertRaises(BundleValidationError):
                    validate_bundle(root)

    def test_duplicate_ids_and_unknown_frame_reference(self):
        for mode in ("frame", "waypoint", "calibration", "batch", "file", "unknown_frame", "uncommitted"):
            with self.subTest(mode=mode):
                root = self.bundle()
                if mode == "frame":
                    frames = read_frames(root)
                    frames.append(copy.deepcopy(frames[0]))
                    write_frames(root, frames)
                elif mode in ("waypoint", "calibration"):
                    uri, field = ("plan.json", "waypoints") if mode == "waypoint" else ("session.json", "calibration_groups")
                    record = read_json(root, uri)
                    record[field].append(copy.deepcopy(record[field][0]))
                    write_json(root, uri, record)
                elif mode in ("unknown_frame", "uncommitted"):
                    batch = read_json(root, BATCH_URI)
                    if mode == "unknown_frame":
                        batch["frame_ids"].append("missing-frame")
                    else:
                        batch["status"] = "writing"
                    write_json(root, BATCH_URI, batch)
                else:
                    manifest = read_json(root, "bundle.json")
                    if mode == "file":
                        manifest["files"].append(copy.deepcopy(manifest["files"][0]))
                    else:
                        duplicate = "batches/duplicate.json"
                        shutil.copyfile(root / BATCH_URI, root / duplicate)
                        manifest["files"].append({"uri": duplicate, "sha256": "0" * 64})
                        manifest["batch_uris"].append(duplicate)
                    write_json(root, "bundle.json", manifest)
                reseal(root)
                with self.assertRaises(BundleValidationError):
                    validate_bundle(root)

    def test_batch_requires_complete_hashed_inputs(self):
        for mode in ("missing_image", "hash_mismatch", "unreferenced_frame", "calibration_drift", "identity_drift"):
            with self.subTest(mode=mode):
                root = self.bundle()
                batch = read_json(root, BATCH_URI)
                if mode == "missing_image":
                    batch["files"] = [entry for entry in batch["files"] if not entry["uri"].startswith("images/")]
                elif mode == "unreferenced_frame":
                    batch["frame_ids"].pop()
                elif mode == "calibration_drift":
                    batch["calibration_groups"][0]["camera_mode"] = "changed-camera"
                elif mode == "identity_drift":
                    batch["session_identity"]["clock"]["clock_id"] = "changed-clock"
                else:
                    batch["files"][0]["sha256"] = "0" * 64
                write_json(root, BATCH_URI, batch)
                manifest = read_json(root, "bundle.json")
                for entry in manifest["files"]:
                    entry["sha256"] = hashlib.sha256((root / entry["uri"]).read_bytes()).hexdigest()
                write_json(root, "bundle.json", manifest)
                with self.assertRaises(BundleValidationError):
                    validate_bundle(root)

    def test_aggregate_must_match_immutable_batch_records(self):
        root = self.bundle()
        frames = read_frames(root)
        frames[0]["control"]["state"] = "altered-only-in-aggregate"
        (root / "frames.jsonl").write_text("".join(json.dumps(frame) + "\n" for frame in frames), encoding="utf-8")
        reseal(root)
        with self.assertRaisesRegex(BundleValidationError, "aggregate and immutable frame differ"):
            validate_bundle(root)

    def test_overlapping_later_batch_preserves_previous_batch_and_calibration(self):
        root = self.bundle()
        original_batch = (root / BATCH_URI).read_bytes()
        original_records = (root / SHARD_URI).read_bytes()
        second_records_uri = "records/synthetic-batch-002.frames.jsonl"
        second_batch_uri = "batches/synthetic-batch-002.json"
        (root / second_records_uri).write_bytes(original_records.splitlines(keepends=True)[0])
        second_batch = read_json(root, BATCH_URI)
        second_batch.update(
            batch_id="synthetic-batch-002", frames_uri=second_records_uri, frame_ids=["frame-000001"]
        )
        for entry in second_batch["files"]:
            if entry["uri"] == SHARD_URI:
                entry["uri"] = second_records_uri
                entry["sha256"] = hashlib.sha256((root / second_records_uri).read_bytes()).hexdigest()
        write_json(root, second_batch_uri, second_batch)
        session = read_json(root, "session.json")
        later_group = copy.deepcopy(session["calibration_groups"][0])
        later_group["calibration_group_id"] = "synthetic-camera-002"
        session["calibration_groups"].append(later_group)
        write_json(root, "session.json", session)
        manifest = read_json(root, "bundle.json")
        manifest["batch_uris"].append(second_batch_uri)
        manifest["files"].extend({"uri": uri, "sha256": "0" * 64} for uri in (second_records_uri, second_batch_uri))
        write_json(root, "bundle.json", manifest)
        reseal(root)
        self.assertEqual(validate_bundle(root)["batch_count"], 2)
        self.assertEqual((root / BATCH_URI).read_bytes(), original_batch)
        self.assertEqual((root / SHARD_URI).read_bytes(), original_records)

    def test_invalid_utc_date_rejected_without_optional_format_dependencies(self):
        root = self.bundle()
        session = read_json(root, "session.json")
        session["clock"]["utc_anchor"] = "2026-02-30T00:00:00Z"
        write_json(root, "session.json", session)
        reseal(root)
        with self.assertRaisesRegex(BundleValidationError, "date-time|invalid UTC anchor"):
            validate_bundle(root)


if __name__ == "__main__":
    unittest.main()
