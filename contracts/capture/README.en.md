# CaptureBundle 1.0

This directory is the authoritative source for the capture input contract. The repository provides JSON Schema 2020-12 and a read-only offline validator. Tests and self-authored synthetic fixtures stay in the parent workspace's `local/` directory and are never committed or uploaded to GitHub. Future collectors and reconstruction consumers can integrate with this contract. Live capture and reconstruction still require implementation and acceptance testing.

## Files and Versions

| File | `schema_version` | Contents |
| --- | --- | --- |
| `bundle.schema.json` | `capture.bundle/1.0` | Frozen bundle entry point, provenance, and complete file hash index |
| `session.schema.json` | `capture.session/1.0` | Session, clock anchor, provenance, controller kind, and calibration groups |
| `plan.schema.json` | `capture.plan/1.0` | Single region, single plan revision, segments, waypoints, and camera intent |
| `frame.schema.json` | `capture.frame/1.0` | Same-frame identity, timing, image, navigation, headings, and quality |
| `batch.schema.json` | `capture.batch/1.0` | Committed batch, immutable frame shard, and input provenance snapshots |
| `common.schema.json` | No standalone record version | Shared definitions for these schemas |

The reader accepts only `1.0`. Unknown major versions report `unknown major`, and other versions report `unsupported schema_version`. Noncritical additional fields within the same version may be ignored. Changes to required fields or the meaning of timing, coordinates, or provenance require an explicit protocol upgrade and updated accepted versions. Schema `$id` values identify locally registered resources. The validator resolves references offline using files in this directory.

## Frozen Bundle Layout

```text
bundle.json
session.json
plan.json
frames.jsonl
batches/<batch_id>.json
records/<batch_id>.frames.jsonl
images/raw/<frame_id>.png
```

The `status` in `bundle.json` must be `frozen`. Its `files` list contains every file and SHA-256 in the bundle except `bundle.json` itself, avoiding circular hashing. Paths use safe POSIX relative paths. Absolute paths, backslashes, colons, `%`, `?`, `#`, empty path components, `.`, `..`, control characters, and symlinks are forbidden. Additional files must appear in the index. Incomplete write artifacts must be excluded before export.

A bundle contains one session, one `clock_id`, one region, and one plan revision. Pausing and resuming within the same process preserves the clock. Resuming after a process restart may preserve the logical task and checkpoint, but requires new `session_id`, `clock_id`, and `segment_id` values, a copied plan with updated session identity, and a separate exported bundle. Previous bundles remain independent. Future reconstruction inputs will associate multiple sessions through `input_batch_ids`. New `segment_id` values represent transitions or position discontinuities. New calibration groups represent image configuration changes. Waypoints in `plan.json` store map-pixel coordinates and camera intent. Intent values cannot serve as actual observations.

## Immutable Batches and Export

- Each `batches/*.json` must have `status` equal to `committed`. Its `frame_ids` and `frames_uri` reference a completed independent frame shard under `records/`. Shard records must match the frozen bundle's aggregate `frames.jsonl` records frame by frame.
- `batch.files` lists exactly `plan.json`, the batch's frame shard, and successfully captured original images with SHA-256 hashes. `input_revision` identifies the plan revision. Batches reference completed versioned plans. The bundled plan content must match that hash.
- `batch.session_identity` stores an immutable snapshot of `session_id`, `clock`, `source`, and `controller_kind`. `batch.calibration_groups` stores complete snapshots of the calibration groups actually used by the batch. Both must match their corresponding bundled session contents.
- Growing session files and global frame logs are excluded from batch hashes. Collection can continue with later batches. An exporter creates a new frozen aggregate index and bundle manifest from committed batches while preserving earlier batch files. Different plan revisions are exported separately.
- Multiple batches may reference the same frame. Each `frame_id` is unique in the aggregate index, and every aggregate frame must be referenced by a committed batch. Passing an active session directory directly fails the frozen entry point or integrity requirements.
- Every nonempty JSONL file must end with a newline. Each line must contain complete strict JSON. Validation rejects duplicate JSON keys, numbers outside the finite IEEE-754 range, blank records, partial records, missing files, and hash mismatches. Nanosecond strings retain integer precision.

This stage provides validation. Checkpoint creation, atomic commit, frozen copying, recovery, and export execution remain for the capture implementation stage.

## Frames, Timing, and Coordinates

`frame_id` associates one image with recognition from that same image. `session_id`, `segment_id`, `waypoint_id`, and `calibration_group_id` must be traceable. Request times are nondecreasing in log order and cannot precede the session's monotonic anchor. Nanoseconds use decimal strings and satisfy `request <= complete <= locate_complete`. When recognition has not run, `locate_complete` is `null` and every observation has `not_run` status. The UTC anchor ends in `Z`. Unknown mapping uncertainty remains `null`.

`native_render_timestamp` is fixed to `null` in `1.0`. Host request and completion times describe capture API operations and cannot establish game render-frame time. `source.kind` explicitly distinguishes `synthetic` from `game_capture`. Synthetic provenance must use the `synthetic` controller.

Successful `image` records contain a `uri`, `sha256`, dimensions, `RGB8` pixel format, and source backend. Originals reside under `images/raw/`. A failed capture records failure status and reason. Image paths, hashes, dimensions, and backend fields are forbidden, and quality must be `rejected`. The validator checks byte hashes and consistency between image dimensions and calibration metadata. Image decoding and pixel-quality checks belong to downstream consumers.

Planar position, floor height, character heading, and camera horizontal heading each store separate `status` and `value` fields. `available` requires a value. `unavailable`, `failed`, and `not_run` require `null`. Valid images may lack navigation results, and headings and positions may fail independently. Map-plane positions use pixels, with X pointing right and Y pointing down. Headings use degrees clockwise from map up. Floor height retains navigation units, and scale is `arbitrary`.

Navigation observations serve control and coarse priors. Camera ground truth, FOV, and intrinsics remain unknown until acquired. `camera_estimate_ref` and `intrinsics_prior` are fixed to `null` in this version. Future reconstruction cameras associate separately through `frame_id`. Minimap position, input duration, and horizontal heading must never populate a full camera pose.

## Usage and Checks

The isolated validator requires Python 3.9+. Dependencies are locked in [`tools/capture/requirements.lock`](../../tools/capture/requirements.lock). MaaEnd's upstream Python 3.14 toolchain remains independent. Run these commands from the repository root using a POSIX shell. The virtual environment, installation cache, and temporary directory stay inside the repository.

```bash
mkdir -p .cache/tmp
python3 -m venv .cache/capture-venv
TMPDIR="$PWD/.cache/tmp" .cache/capture-venv/bin/python -m pip install --cache-dir .cache/pip -r tools/capture/requirements.lock
PYTHONDONTWRITEBYTECODE=1 .cache/capture-venv/bin/python -m tools.capture.validate path/to/frozen-bundle
```

Replace `path/to/frozen-bundle` with your frozen bundle directory. A standalone clone does not include project tests or samples. In the existing two-repository workspace, run the following commands from this repository root using the parent workspace's isolated environment and local validation files.

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m tools.capture.validate ../local/samples/capture/synthetic-minimal
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m unittest discover -s ../local/qa/capture -p test_validate.py -v
```

The command returns `0` on success, `1` for data or filesystem errors, and `2` for argument errors. The Python entry point is `tools.capture.validate.validate_bundle(directory)`. It returns the manifest and frame, batch, and file counts, or raises `BundleValidationError` or a filesystem `OSError`. Temporary test copies stay under the parent workspace's `local/.cache/capture-tests/` and are cleaned up automatically.

The parent workspace's `local/samples/capture/synthetic-minimal/` contains one 16×12 RGB PNG and two synthetic frame records. The PNG was generated with Python's standard-library `struct` and `zlib`, using PNG filter `0` for each row and pixels `(x * 16, y * 20, (x + y) * 9)`, where `x=0..15` and `y=0..11`. A local unit test verifies PNG chunk CRCs, dimensions, and decompressed pixels. The first frame models a valid image with unavailable navigation. The second models capture failure. All identities, map coordinates, and times are synthetic. Game control, live navigation, and visual reconstruction were never executed.

Later stages will define `ReconstructionBundle`, `PreviewSnapshot`, `RescanRequest`, camera solving, and executable rescan contracts, then integrate real same-frame capture, event logs, calibration, and export acceptance testing.
