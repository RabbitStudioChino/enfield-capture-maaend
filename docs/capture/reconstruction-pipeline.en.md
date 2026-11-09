# Offline reconstruction runner

This document covers single-region serial reconstruction, checkpoint recovery and portable output. Updated 2026-11-09. Chinese version at [离线重建运行器](./reconstruction-pipeline.md).

## Environment and tool probing

Use Python 3.9 or newer with `tools/capture/requirements.lock` installed. Keep inputs, configuration, logs, caches and output under workspace `local/`. Run the commands below from the capture repository root, with `WORKSPACE` pointing to its parent workspace. Install tools locally beforehand. The runner performs installation-independent probing.

```sh
export WORKSPACE="$(cd .. && pwd)"
export PYTHONDONTWRITEBYTECODE=1
python3 -m tools.reconstruction doctor \
    --workspace "$WORKSPACE" \
    --output "$WORKSPACE/local/validation/reconstruction-tools/run-001" \
    --colmap "$WORKSPACE/local/tools/colmap/colmap" \
    --openmvs-dir "$WORKSPACE/local/tools/openmvs"
```

`doctor` executes `--help` for five COLMAP operations and five OpenMVS tools, recording original text, SHA-256, version text and executable hashes. OpenMVS 2.4.0 returns 1 for help, but complete help remains valid capability evidence. Missing tools, unusable executables or unsupported flags reject the affected stage. `ready` describes help availability.

The build fingerprint uses binary SHA-256 and normalized flag capabilities. Help timestamps, machine capacity, log paths and installation paths are excluded from the build key. Dynamic libraries and the operating system remain part of the execution environment, with environment clues preserved in original help. Executable hashes are rechecked before execution.

Official COLMAP 4.2.1 CPU and OpenMVS 2.4.0 binaries were checked and exercised with a self-owned multiview scene on macOS arm64. Windows process-tree cancellation and real game inputs still require their corresponding hardware validation.

## Explicit camera groups and coordinates

First create an immutable job using [offline input preparation](./offline-preparation.en.md). Read `local/reconstruction/<job>/calibration-groups.json` and explicitly map each `camera_group_id` to an output group. Configuration follows `contracts/reconstruction/config.schema.json`.

```json
{
    "schema_version": "reconstruction.config/1.0",
    "camera_model": "PINHOLE",
    "camera_params": null,
    "camera_group_mapping": {
        "cal-REPLACE_WITH_PREPARED_GROUP_HASH": "camera-01"
    },
    "random_seed": 0,
    "stage_timeout_seconds": 3600,
    "max_image_size": 1600,
    "world_frame_id": null,
    "scale_status": "arbitrary",
    "transform_world_from_sfm": null,
    "parent_revision": null
}
```

The mapping must be complete. A shared destination group shares intrinsics, with matching raw dimensions, camera mode and FOV required before merging. Separate image subdirectories drive `single_camera_per_folder`, followed by SQLite verification of actual camera assignments. The initial runner uses one explicit `camera_model` across groups, while estimating independent group intrinsics. Optional common `camera_params` require matching image geometry and the correct parameter count for that model.

Preprocessing performs an identity copy and records `A_processed_from_raw=I`. Raw images are preserved and actual COLMAP feature extraction performs image decoding. HUD removal, character masks and propagation through MVS retain their individual unvalidated states. Contamination remains part of human quality review.

The original mesh retains arbitrary SfM coordinates. Canonical-world cameras require explicit `s`, an orthonormal determinant-1 `R`, `t`, `coordinate_revision`, `scale_evidence`, a `world_frame_id` and `estimated` or `verified` scale. The transform is `Xw=s R Xsfm+t`. Camera centers use `C=-Rcvᵀt`, and web-camera rotation uses `R Rcvᵀ diag(1,-1,-1)`. Scaling affects positions and geometry. Matrices are stored row-major, following the documented COLMAP conventions. [COLMAP format](https://colmap.github.io/format.html)

## Planning and execution

Save configuration under workspace `local/` and choose a new run ID. A plan binds the preparation manifest hash, configuration, formal schemas, runner code, tool fingerprint and random seed. COLMAP receives explicit global and mapper seeds. OpenMVS randomness is controlled by the pinned version itself, so output bytes may vary.

```sh
python3 -m tools.reconstruction plan \
    --workspace "$WORKSPACE" --job area-01 --run area-01-r001 \
    --config "$WORKSPACE/local/reconstruction-config.json" \
    --toolchain "$WORKSPACE/local/validation/reconstruction-tools/run-001/toolchain.json"
python3 -m tools.reconstruction run --workspace "$WORKSPACE" --run area-01-r001
python3 -m tools.reconstruction status --workspace "$WORKSPACE" --run area-01-r001
```

| Stage | Output and checks |
| --- | --- |
| verify | Revalidate frozen prepared input and byte hashes |
| preprocess | Copy images into explicit groups, recording identity processing and source frames |
| features | SQLite integrity, image names, nonempty features, descriptor lengths and camera groups |
| match | Independent database copy and at least one geometrically verified pair |
| map | Actual mapper and text conversion, parsing cameras, poses, points and reciprocal tracks |
| undistort | Separate undistorted images, pinhole models and closed image references |
| import | MVSI camera scene, actual InterfaceCOLMAP readback and image mapping checks |
| densify | MVSI camera scene and companion dense PLY, parsing nonempty points and camera references |
| mesh | Actual triangle geometry, parsing binary or text PLY and index ranges |
| refine | Explicit camera scene plus `mesh-file`, checking refined PLY |
| texture | Explicit refined mesh, checking OBJ/UV/MTL and PNG/JPEG structure |
| quality | Registration ratio, components, track lengths, reprojection error, dense points and mesh counts |
| bundle | Packaging, schema, paths, hashes, coordinates and provenance consistency |

When mapper produces multiple models, the runner selects the largest registered component, using model name as the tie-breaker, and records all component sizes. Excluded input still affects registration ratio and quality remains `needs_review`. Small regions use exhaustive matching. Installed help determines commands and CPU flags. [COLMAP CLI](https://colmap.github.io/cli.html)

OpenMVS 2.4.0 InterfaceCOLMAP reads the MVSI interface format. The runner explicitly uses `archive-type=-1` and passes camera scenes and PLY meshes separately, avoiding incompatible Boost-native scenes. Binaries without `export-texture-lossless` retain their JPEG output, whose dimensions and marker structure are checked. PNG additionally receives CRC, decompression-size and scanline checks. [OpenMVS InterfaceCOLMAP](https://github.com/cdcseacave/openMVS/blob/v2.4.0/apps/InterfaceCOLMAP/InterfaceCOLMAP.cpp), [OpenMVS workflow](https://github.com/cdcseacave/openMVS/blob/develop/docs/wiki/Usage.md)

## Recovery and cancellation

```sh
python3 -m tools.reconstruction cancel --workspace "$WORKSPACE" --run area-01-r001
python3 -m tools.reconstruction resume --workspace "$WORKSPACE" --run area-01-r001
```

Each run uses an exclusive directory lock. A local request file triggers cancellation through polling and process-group termination, with `taskkill /T /F` on Windows. Timeout bounds apply to each external command. Ctrl-C also records cancellation. A queued cancellation takes effect when execution starts, while explicit `resume` clears an earlier request.

Checkpoints are written after successful commands and artifact validation. Recovery reuses a prefix only when checkpoints say `succeeded`, stage keys match and all file hashes match. The first invalid stage and all descendants rerun, retaining prior attempts under the run's `attempts/`. Corrupt checkpoints are recomputed. Configuration, code, schema or tool changes require a new plan.

States are `queued`, `running`, `failed`, `cancelled` or `needs_review`. Failures retain `stage`, the error and `logs/<stage>.log`, leaving descendants unexecuted. A crash-retained lock requires confirming that the owner and its descendants stopped before manual removal. Logs and runtime caches stay under `local/reconstruction-runs/<run>/`, outside the portable bundle.

## Portable output and human feedback

The final directory is `local/reconstruction-runs/<run>/stages/bundle/`, containing `reconstruction.json`, `cameras.json`, `quality.json`, `provenance.json` and `assets/`. Copy the entire directory for Blender authoring. File hashes determine the content revision, while the build key remains separate, so different outputs from the same configuration receive different revisions.

```sh
python3 -m tools.reconstruction verify-bundle \
    --bundle "$WORKSPACE/local/reconstruction-runs/area-01-r001/stages/bundle"
python3 -m tools.reconstruction feedback \
    --workspace "$WORKSPACE" --run area-01-r001 --request review-001
```

`provenance.json` carries configuration, normalized tool fingerprints and the preparation manifest. `source.input_batches` uses combined `source_id`, `batch_id` and `sha256` provenance, with batch IDs interpreted within their source session. Formal schemas live under `contracts/reconstruction/`. File references must close, with the manifest serving as the external transfer trust root.

The feedback directory contains a PreviewSnapshot bound to the source revision and bundle hash, cameras, sparse geometry, quality and a RescanRequest. An explicit transform places both cameras and preview points in canonical-world coordinates. Requests carry session/frame identities and retain `needs_mapping` with `automatic_navigation=false` for human view selection. Feedback reads and writes share the same run lock.

Reprojection statistics use COLMAP's stored global bundle-adjustment error. Numerical thresholds await game M0 calibration, while geometry holes, held-out views, dynamic contamination, walkable surfaces, game reachability and metric scale each remain `not_assessed`. The self-owned synthetic scene establishes actual toolchain and scheduler execution. Stable reconstruction from real game imagery still depends on returned Windows data and human acceptance.
