# Reconstruction review loop

This document covers resumable projects, coarse COLMAP reconstruction, external GPT review and constrained rescans. Updated 2026-11-12. Chinese version: [多轮重建、审阅与补采](./reconstruction-loop.md).

## Scope and current verification status

Each round reconstructs the previous frozen capture bundles together with newly appended bundles. It runs only COLMAP features, matching, a joint mapper and sparse quality analysis. Historical inputs, rounds, review results and final jobs remain separate. Features and verified matches may be reused, while the mapper still solves the entire round jointly; local incremental SfM, local MVS, automatic hole filling and transparent-material recovery are not implemented.

Only after both numeric gates and external image review pass does an independent final job copy the largest connected reconstruction from that round and run undistortion, full OpenMVS densification, meshing, refinement and texturing. A smaller model cannot replace a largest model that still fails. The final artifact covers only the qualifying model portion and remains `needs_review`; it does not certify the whole region or game.

At the user's request, this development skipped all tests, simulations and live game verification. Work was limited to source review, formatting, Python 3.12 static compilation and delivery packaging. The commands below are operating steps that have not been accepted on real multi-round game data. Masks, holes, dynamic leakage, scale, collision and device behavior still require review.

## First capture round and transfer

Open the running game on Windows and execute these commands from the capture repository root. The region name must match real local resources. `auto` controls movement and camera input; return to the game after starting. The existing one-click launcher and capture page can also select a region.

```powershell
$Workspace = (Resolve-Path ..).Path
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m tools.capture auto --workspace $Workspace --region "真实区域名称" --task first-round --max-points 24 --views 8
python -m tools.capture status --workspace $Workspace --task first-round
python -m tools.capture export --workspace $Workspace --task first-round
```

Record the complete `bundle` directory returned by `export`. Copy that entire frozen directory into the offline workspace at `local/inputs/first-round/`, preserving relative paths for `bundle.json`, images and metadata. Original images, automatic plans and candidates stay local. Only new automatic capture bundles contain `auto-plan.json` and `rescan-candidates.json` for executable rescans; older bundles remain usable for reconstruction and manual review.

Moving only a review directory is insufficient to continue the same project on another machine or drive Windows rescans. Keep the same code and contracts and copy the entire `local/reconstruction-projects/<project_id>/` directory together with its referenced `local/reconstruction/<job>/` and `local/reconstruction-runs/<run_id>/`, preserving relative layout. A raw CaptureBundle can independently seed a new project; an existing review requires the complete round and coarse evidence. Absolute paths in frozen JSON are original-machine locators and must not be rewritten during transfer. Supply actual tool locations through a new `doctor` file.

## Create a project and run a coarse round

Run the following offline commands from the capture repository root with the dependencies in `tools/capture/requirements.lock`. Install tools separately; `doctor` probes installed tools without downloading them. Missing OpenMVS makes `ready=false`, but coarse rounds can proceed when COLMAP is available. Final refinement requires every tool.

```sh
export WORKSPACE="$(cd .. && pwd)"
export PYTHONDONTWRITEBYTECODE=1
python3 -m tools.reconstruction doctor --workspace "$WORKSPACE" \
    --output "$WORKSPACE/local/validation/reconstruction-tools/loop-001"
```

Save this configuration as `$WORKSPACE/local/reconstruction-config.json` in the parent workspace, rather than inside the repository. The placeholder `camera_group_mapping` satisfies the existing configuration format; the project generates an independent mapping for every real session and calibration group. Unknown camera modes or FOV do not justify sharing intrinsics, and equal resolution does not merge calibration groups. `camera_params=null` estimates intrinsics separately. A joint solve establishes a new SfM frame each round, so the project clears previous world transforms and keeps arbitrary scale until explicit authoring alignment.

```json
{
    "schema_version": "reconstruction.config/1.0",
    "camera_model": "PINHOLE",
    "camera_params": null,
    "camera_group_mapping": {"project-generated": "camera-01"},
    "stage_timeout_seconds": 3600,
    "max_image_size": 1600,
    "world_frame_id": null,
    "scale_status": "arbitrary",
    "transform_world_from_sfm": null,
    "parent_revision": null,
    "random_seed": 0
}
```

```sh
python3 -m tools.reconstruction project-create --workspace "$WORKSPACE" --project region-loop \
    --config "$WORKSPACE/local/reconstruction-config.json" \
    --toolchain "$WORKSPACE/local/validation/reconstruction-tools/loop-001/toolchain.json"
python3 -m tools.reconstruction project-add-round --workspace "$WORKSPACE" --project region-loop \
    --bundle "$WORKSPACE/local/inputs/first-round"
python3 -m tools.reconstruction project-run --workspace "$WORKSPACE" --project region-loop
python3 -m tools.reconstruction project-status --workspace "$WORKSPACE" --project region-loop
```

The project lives under `local/reconstruction-projects/region-loop/`. `inputs/<sha256>/` stores complete frozen bundles; `rounds/round-0001/round.json` binds all sources, the prepared job and run ID. Actual logs, stages, cache evidence and time accounting live in `local/reconstruction-runs/<run_id>/`. Later rounds append every historical source, deduplicate `(session_id,frame_id)` and reject conflicting identities.

Each round verifies prepared inputs, binary fingerprints, configuration and checkpoints. Previous feature and matching databases seed the next round only when old image contents, camera mappings, extraction configuration, COLMAP fingerprints and file hashes match. Changed configurations extract again; corrupt caches fail explicitly. Copied databases receive another hash check. Resume reuses a successful prefix whose inventories still match, and moves other old attempts into `attempts/`. `features/cache.json` records actual reuse or invalidation reasons.

## Export and import external review

```sh
python3 -m tools.reconstruction project-review --workspace "$WORKSPACE" --project region-loop \
    --round round-0001 --review review-001
```

The result includes `review_file`, `response_template`, frozen source directories and the review directory. `review.json` binds the project, round, real reconstruction revision, source bundles, automatic plans, candidate catalogs and resource hashes. `frame-index.json` lists every frame, session, waypoint, map position, available floor height, heading observation, calibration and original image hash. `sources/` preserves source metadata; `cameras.json` and `sparse-model/` preserve the actual coarse solve. Failed rounds export real errors and bounded log tails without fabricated geometry.

The package copies at most 16 issue or reference originals and generates no crops. Every original remains in its frozen source. A full index and a few attachments do not constitute full image review: images and regions start `unreviewed`. If external review needs more images, provide them from their indexed local sources. Unseen images must remain unreviewed. A directory may be zipped manually, with the archive outside the frozen review directory.

```sh
mkdir -p "$WORKSPACE/local/review-exports"
python3 -m zipfile -c "$WORKSPACE/local/review-exports/review-001.zip" \
    "$WORKSPACE/local/reconstruction-projects/region-loop/reviews/review-001"
```

Give the package to the external GPT of your choice and return the completed `response-template.json` following `REVIEW.txt`. The system binds no API, reads no model credentials and uploads no images automatically. Responses require an identified reviewer, complete disjoint `reviewed/unreviewed/failed` frame lists, region coverage, issue evidence and `accepted_models`. Issue classes are `source_image_quality`, `insufficient_observation`, `difficult_material`, `localization_or_tool_failure` and `insufficient_evidence`. Registration ratios or reprojection errors alone cannot establish a material, blur or missing-view diagnosis. A rescan action does not automatically resolve difficult materials or insufficient evidence.

Save the returned file to a new local path and import it. Import verifies the frozen review inventory, current round revision, complete coverage, model digest, image evidence and rescan sources. An old-round response cannot drive the current rescan. Responses are archived by content hash with append-only ordering receipts; importing identical content again does not change precedence. `project-status` exposes the latest imported coverage and unresolved issues.

```sh
python3 -m tools.reconstruction project-import --workspace "$WORKSPACE" --project region-loop \
    --response "$WORKSPACE/local/review-results/review-001.json"
```

## Constrained rescans and the next round

Refresh the project list on the Windows capture page, choose the project and round, export review, import results and preview `rescan`. The page can also be started with `python -m tools.capture serve --workspace <workspace>`. Preview shows each issue, source image, action, candidate target, heading, budget and rejection reason. Preview only reads files. Starting rechecks source hashes, round identity, duplicates, budgets and current native navigation reachability before using the existing task pause, resume, stop and progress controls.

`reconstruction.rescan/2.0` can reference only reachable candidate IDs from its frozen source, with a heading and issue reason. One request binds one source bundle and its original automatic plan; issue evidence may reference every source in the review package. Actions for different sources require separate requests. The model cannot supply arbitrary map coordinates, floors or executable paths. New 3D-to-navigation mapping and new-point generation are not implemented. If candidates cannot represent a deficient area, use `classification=insufficient_evidence` and `status=unresolved`, describe `needs_mapping` and retain `rescan=null`; it cannot execute. Existing `reconstruction.rescan/1.0` remains the manual `automatic_navigation=false` protocol and cannot execute automatically.

When the rescan task ends, use the existing `export` command to freeze a new bundle, transfer it offline and append a round. Rescan frames retain their action and issue associations without overwriting old images. Task startup, saved screenshots and improved reconstruction metrics are separate states; none alone resolves an issue.

```sh
python3 -m tools.reconstruction project-add-round --workspace "$WORKSPACE" --project region-loop \
    --bundle "$WORKSPACE/local/inputs/rescan-round-002"
python3 -m tools.reconstruction project-run --workspace "$WORKSPACE" --project region-loop
python3 -m tools.reconstruction project-review --workspace "$WORKSPACE" --project region-loop \
    --round round-0002 --review review-002
```

## Budgets, stopping and recovery

The default policy below is an engineering gate without real-game calibration. Supply a complete local JSON policy with `--policy` when creating a project; field definitions are in `contracts/reconstruction/project.schema.json`. Frozen project configuration and policy are not overwritten.

| Field | Default |
| --- | --- |
| `max_rounds` | 8 rounds |
| `max_images` | 4000 cumulative deduplicated captured images, including rejected images |
| `max_compute_seconds` | 28800 seconds including failures, retries and final refinement |
| `max_stagnant_rounds` | 2 consecutive comparisons without sufficient improvement |
| `min_relative_improvement` | 0.02 |
| `max_rescan_actions` | 128 cumulative project actions |
| `thresholds.min_registered_images` | 6 |
| `thresholds.min_registration_ratio` | 0.6 |
| `thresholds.min_sparse_points` | 500 |
| `thresholds.min_mean_track_length` | 2.5 |
| `thresholds.max_reprojection_error_p95_px` | 3 pixels |
| `thresholds.max_connected_components` | 1 |

Stagnation compares real coarse solves using `registered_images × registration_ratio / (1 + p95_error)` and accumulates comparisons whose relative gain does not exceed policy. Tool failures do not count as quality stagnation. Round, image and compute caps expose explicit stop reasons, while the last admitted round can still run. Rescan preview also reserves images from existing tasks not yet appended to reconstruction rounds. Time accounting is persisted before execution. Uncertain active time after a crash counts conservatively and can include downtime.

Repeat `project-run` to resume the latest failed round. Cancel using its real `run_id`: `python3 -m tools.reconstruction cancel --workspace "$WORKSPACE" --run <run_id>`. Missing tools report `tools_missing`; failures without a parseable model report `awaiting_review` with native errors and logs. After fixing binaries, run `doctor` into a new directory and create a new round from the existing frozen inputs without overwriting the old plan.

```sh
python3 -m tools.reconstruction project-add-round --workspace "$WORKSPACE" --project region-loop \
    --toolchain "$WORKSPACE/local/validation/reconstruction-tools/loop-002/toolchain.json"
python3 -m tools.reconstruction project-run --workspace "$WORKSPACE" --project region-loop
```

Interrupted source copies and preparation attempts remain local, and subsequent attempts use new directories. Before manually removing a `.lock` directory left by a crash, confirm that the old process and descendants have stopped. Code or formal schema changes invalidate old plans. Resume with the same code version, or create a new project from the frozen inputs; do not edit old plan hashes to pretend compatibility.

## Final refinement, Blender and WorldRelease

Final refinement requires the largest model to pass every numeric gate and the latest imported real external response to accept its `model_digest`. Every registered image in that model must be reviewed, with no associated unresolved issues. Other unregistered images may remain `unreviewed`. Both gates are required; a favorable GPT opinion cannot replace numeric evidence.

First repeat actual external review for `review-002`, provide every original image needed for that model using the index, and save the qualifying response as `local/review-results/review-002.json`. Populate `accepted_models` only after actual image review. Import that second-round response and probe a new toolchain after installing the final tools.

```sh
python3 -m tools.reconstruction project-import --workspace "$WORKSPACE" --project region-loop \
    --response "$WORKSPACE/local/review-results/review-002.json"
python3 -m tools.reconstruction doctor --workspace "$WORKSPACE" \
    --output "$WORKSPACE/local/validation/reconstruction-tools/final-001"
python3 -m tools.reconstruction project-final --workspace "$WORKSPACE" --project region-loop \
    --round round-0002 --final final-001 \
    --toolchain "$WORKSPACE/local/validation/reconstruction-tools/final-001/toolchain.json"
python3 -m tools.reconstruction project-status --workspace "$WORKSPACE" --project region-loop
```

A new toolchain file can add OpenMVS, but the COLMAP binary and capability fingerprints must match the reviewed coarse solve. Missing final tools do not create a stuck final job, so the same `final` ID can be used after installation. Changing tools for an existing job requires a new ID. The final job starts from frozen coarse stages whose copies are verified again, without rerunning the mapper. Retries reuse its checkpoints and time accounting.

Read the actual final ID from `finals[].run_id` and replace `FINAL_RUN` below. The resulting `stages/bundle/reconstruction.json` feeds the existing Blender workflow. Arbitrary-scale reconstruction can enter an independent master, but WorldRelease export requires actual scale and world-alignment evidence, authored collision and a spawn point. Do not infer the SfM transform from a navigation map.

The Blender and WorldRelease commands require the full source workspace, an installed Blender and the web repository. The Windows delivery package contains capture and reconstruction tools, rather than the complete authoring and publishing chain. Transfer the real final reconstruction bundle to the full workspace before proceeding. Commands below use Blender's standard macOS path; on Linux or Windows, add `--blender <local executable>` to both `tools.blender import` and `tools.world_export build`.

```sh
export FINAL_RUN="REPLACE_WITH_ACTUAL_FINAL_RUN_ID_FROM_STATUS"
python3 -m tools.reconstruction verify-bundle \
    --bundle "$WORKSPACE/local/reconstruction-runs/$FINAL_RUN/stages/bundle"
python3 -m tools.blender import \
    --reconstruction "$WORKSPACE/local/reconstruction-runs/$FINAL_RUN/stages/bundle/reconstruction.json" \
    --output "$WORKSPACE/local/masters/region-r1.blend"
python3 -m tools.blender import \
    --reconstruction "$WORKSPACE/local/reconstruction-runs/$FINAL_RUN/stages/bundle/reconstruction.json" \
    --transform "$WORKSPACE/local/alignment.json" --world-frame region-world-v1 --scale-status estimated \
    --output "$WORKSPACE/local/masters/region-aligned.blend"
python3 -m tools.world_export build --master "$WORKSPACE/local/masters/region-r2.blend" \
    --config "$WORKSPACE/local/export/region.json" --releases "$WORKSPACE/local/releases"
python3 -m tools.world_export validate "$WORKSPACE/local/releases/region-r1"
python3 "$WORKSPACE/endfield-world-web/tools/asset-validation/validate_world_release.py" \
    "$WORKSPACE/local/releases/region-r1"
```

The `s/R/t/coordinate_revision/scale_evidence` in `alignment.json` must come from actual scale and alignment evidence. The second import creates an independent aligned master without passing the arbitrary-coordinate master's `--master`. Scaling and saving in Blender alone does not update source `scale_status`. Author patches, collision and anchors in `region-aligned.blend`, then save `region-r2.blend`. `accepted_reconstruction_revision` in `region.json` must name the actual reconstruction revision accepted by its reviewer. See [Blender masters and export](./blender-export.en.md) and [WorldRelease publishing](./release-publishing.en.md). The web app continues to consume only WorldRelease, without reading capture databases, original images or coarse projects.
