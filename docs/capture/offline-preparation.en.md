# Offline Image Input Preparation

This guide describes the input preparation subset of R01: arranging frozen `CaptureBundle` data into portable raw image inputs. It runs no COLMAP, OpenMVS, or Blender, is not a formal `ReconstructionBundle`, and establishes no acceptance of image quality, cameras, scale, or reconstruction. Prerequisite acceptance for R00, C04, and the complete reconstruction DAG remains pending. The `capture.probe/1.0` directories from [Windows screenshot diagnostics](windows-validation.en.md) cannot be used directly as frozen bundles.

## Requirements

Use Python 3.9 or later with the existing CaptureBundle validator dependencies, `jsonschema` and `referencing`. Run from the capture repository root. Keep dependency environments, download caches, and bytecode caches in workspace `local/`; set `PYTHONDONTWRITEBYTECODE=1` to avoid bytecode beside source files. No game SDK or reconstruction tools are required.

`--workspace` is an absolute path to the real workspace directory. Each `--bundle` also uses an absolute path to a real directory inside that workspace. Output always goes to workspace `local/reconstruction/<job>/`. `--job` contains 1–64 ASCII letters, digits, hyphens, or underscores, starts with a letter or digit, and excludes Windows reserved names.

```bash
python -m tools.reconstruction --help
python -m tools.reconstruction prepare --help
python -m tools.reconstruction verify --help
python -m tools.reconstruction prepare --workspace /absolute/workspace --bundle /absolute/workspace/local/capture/bundle-a --job region-a-001
python -m tools.reconstruction verify --workspace /absolute/workspace --job region-a-001
```

Repeat `--bundle` to combine bundles, listing each bundle once. Frozen exports for manually positioned tasks are described in the [task CLI](manual-cli.en.md). The actual Windows task workflow still awaits acceptance. Local, self-authored synthetic bundles only exercise tool logic and must retain their `source.kind=synthetic` provenance.

```bash
python -m tools.reconstruction prepare --workspace /absolute/workspace --bundle /absolute/workspace/local/capture/bundle-a --bundle /absolute/workspace/local/capture/bundle-b --job region-a-002
```

## Input Selection and Compatibility

- Each bundle first passes `tools.capture.validate.validate_bundle` for its formal schemas, frozen status, complete file index, SHA-256 hashes, frame shards, and committed batches. Active sessions, probe directories, partial JSONL tails, missing files, and hash mismatches are rejected. Source bundles are validated again after copying.
- One job requires identical `region_id`, complete `map_coordinates`, `controller_kind`, and all `source` fields except `description`. Provenance checks include `kind`, `game_version`, collector version, upstream commit, and submodule commits. The contract has no separate map version field; map identity is checked through `coordinate_frame_id` and the remaining map coordinate fields. An unknown `game_version=null` remains unknown and cannot prove equal game versions.
- Only raw images with `image.status=captured` and a `quality.selection_status` other than `rejected` are copied. `pending` remains pending quality review and is never promoted to accepted. Failed and rejected frames retain their mappings and reasons but receive no image in `images/`. A job fails when no eligible raw images remain.
- Supported image extensions are `.png`, `.jpg`, `.jpeg`, `.bmp`, `.tif`, and `.tiff`. Original bytes and extension types are preserved. Checks establish byte provenance and hashes without decoding images or confirming actual dimensions, visual content, or feature matchability.
- Output names use the SHA-256 of a canonical serialization of `(session_id, frame_id)` to prevent equal frame names in different sessions from overwriting each other. Repeated frames are deduplicated only when their entire original records agree, retaining every origin. Conflicting calibration snapshots for one session are rejected.
- Calibration groups remain separate by `(session_id, calibration_group_id)`, even when dimensions or original group names agree. Subsequent COLMAP work must explicitly map each group to solved cameras; never assume that every image shares one camera.

## Output and Verification

```text
local/reconstruction/<job>/
  preparation.json
  frames.jsonl
  calibration-groups.json
  images/<identity-sha256>.png
  sources/<bundle-sha256>/bundle.json
  sources/<bundle-sha256>/session.json
  sources/<bundle-sha256>/plan.json
  sources/<bundle-sha256>/frames.jsonl
  sources/<bundle-sha256>/batches/...
  sources/<bundle-sha256>/records/...
```

`preparation.json` uses the internal format `reconstruction.preparation/1.0`. It records the tool version, job ID, compatibility, source bundle hashes, and the SHA-256 and byte count of every output file, excluding itself to avoid circular hashes. Source directories retain the original frozen bundle JSON / JSONL metadata and its original hash relationships without copying excluded raw images. `frames.jsonl` preserves each complete original frame record, source bundle references, selected image path, and calibration group mapping. Original navigation observations and unknown camera fields remain unchanged.

`images/` contains actual copies rather than hard links or symbolic links and can serve as candidate image input for later COLMAP work. Source images remain unchanged. Prepared files become read-only before commit, and the tool rejects existing jobs. Read-only attributes are not tamper protection. Move the complete job directory to the same `local/reconstruction/<job>/` location in another workspace and run `verify` there. Verification needs neither the original bundles nor absolute paths from the original workstation.

`verify` recomputes every output hash and byte count, rejects missing and extra files, and checks closure of provenance metadata, frame mappings, calibration groups, and compatibility. It confirms input consistency and byte integrity, without authenticating a publisher or accepting reconstruction quality. Editing manifests and recalculating hashes cannot establish trusted provenance.

Success prints a message such as `PREPARED INPUTS 12 images; reconstruction=not_run; quality=not_assessed` and exits `0`. Validation or file operation failures exit `1`; command line argument errors exit `2`, and Ctrl+C exits `130`. All states explicitly indicate that image processing, HUD removal, camera solving, and reconstruction have not run. Scale is unknown, and calibration groups require explicit mapping. Valid images or successful preparation do not establish reconstruction feasibility.

## Writing and Failure Recovery

Jobs use an exclusive `.<job>.lock` directory beside their output. They first write `.<job>.<random>.partial/`, verify it completely, and rename it to the final directory on the same volume. Existing jobs, concurrent jobs with the same name, path escapes, symbolic links, and Windows reparse points / junctions are rejected. Input bundles cannot contain output staging.

Failures after entering staging retain `failure.json` (Ctrl+C records `cancelled` and releases the lock) and any diagnostic contents already written. The console identifies that uncommitted directory, and no partial final job is published. Unwritable directories, existing locks, or failures before staging can only produce console errors. Unwritable storage may also prevent failure reports, so preserve terminal output. After confirming that the process has exited, inspect or remove failed staging directories manually. Remove a lock left by abnormal termination only after confirming that no process uses it. The tool never takes over locks or resumes failed jobs automatically.

Only offline input preparation independent of Windows hardware has been implemented. HUD / character masks, image preprocessing, manual camera group mapping, COLMAP features and registration, OpenMVS, scale evidence, quality thresholds, and Blender work have not run. Return Windows hardware results as specified in the [validation guide](windows-validation.en.md).

## Local Code Checks

Workspace tests and synthetic samples are not committed to GitHub. When the local test workspace is available, run the following from the capture repository root. `/absolute/workspace/local/envs/offline/bin/python` represents a local interpreter with the validator dependencies installed.

```bash
PYTHONDONTWRITEBYTECODE=1 /absolute/workspace/local/envs/offline/bin/python /absolute/workspace/local/qa/capture/test_reconstruction.py -v
```

Tests cover raw image copies, portable session mappings, duplicate and conflicting frames, source and map conflicts, empty inputs, corrupt and active bundles, source changes during copying, locks and overwrite protection, path links, and rejection of tampered prepared data. Windows junction checks only simulate their flags and do not replace actual NTFS or Windows game acceptance.
