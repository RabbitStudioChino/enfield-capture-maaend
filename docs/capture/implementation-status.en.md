# Independent Implementation and Hardware Boundaries

Updated 2026-11-09 (Asia/Shanghai). This document separates implementation, tool integration, and actual game acceptance. The [detailed architecture](architecture.en.md) remains the design baseline.

## Implementation Independent of Windows Results

| Module | Implementation and entry point | Acceptance boundary |
| --- | --- | --- |
| Capture handoff | CaptureBundle validation, frozen input preparation, provenance and calibration groups, immutable copies, and transfer verification; [input preparation](offline-preparation.en.md) | Probe directories are not converted into fabricated capture sessions |
| Offline reconstruction | Tool capabilities and fingerprints, plans, staged execution, status, cancellation, checkpoint recovery, and artifact checks; [pipeline](reconstruction-pipeline.en.md) | Runs actual COLMAP / OpenMVS; process success does not establish geometry acceptance |
| Coordinates and feedback | COLMAP model parsing, camera conversion, explicit scale and coordinate revisions, statistics, PreviewSnapshot / RescanRequest | Without a reliable navigation mapping, suggestions remain for manual review and send no game commands |
| Blender master | Separate source revisions, Working / ManualPatches / Collision / Anchors, repeated imports and protection, coordinate round trips; [master tools](blender-export.en.md) | Arbitrary-scale sources can be retained; publication needs explicit scale evidence and an accepted revision |
| Release construction | Schemas pinned by Git commit and SHA, quality profiles and LOD derived from the master, embedded GLB, triangle collision, indexes, provenance, staging validation, and preservation of existing releases; [release tools](release-publishing.en.md) | Repairs, spawn placement, and collision still need checks in the actual scene |
| Independent roaming | Boxes / trimesh, keyboard and touch, free camera, safe return, context recovery, character heading, and available animations | The browser connects to neither MaaEnd nor the game |
| Resources and performance | Hash checks, bounded concurrency, content sharing and disposal, chunk residency, unavailable-boundary protection, LOD hysteresis, quality settings, optional automatic resolution, and runtime reports | Desktop or synthetic scenes cannot establish mobile frame rates or thermal behavior |

The [independent web repository](https://github.com/RabbitStudioChino/endfield-world-web) owns browser implementation and instructions. Triangle collision uses the separate `world.collision/2.0` format while retaining `1.0` boxes. The capture repository pins schemas without copying web runtime code.

## Actual Tool Checks Performed

This workspace uses official COLMAP 4.2.1, OpenMVS 2.4.0, and the installed Blender. Downloads, original help output, logs, self-authored multiview images, and all check scripts remain in `local/`. A self-authored Blender scene has passed through sparse reconstruction, densification, meshing, and texturing. Output remains `needs_review`; geometry is not automatically accepted.

Actual Blender GLB / triangle-collision releases have been checked by the web validator and loaded in Chrome for character movement, quality switching, and runtime reports. Chunking, LOD, cancellation, and recovery have local checks. These do not establish acceptance of the real game world or mobile devices.

## Windows Hardware Evidence Received

Actual game reports and screenshots were received from the user on 2026-11-09. A normal `doctor` completed one frame, and a normal `probe` completed 12/12 frames, all recorded at the original 2560×1440 size. Their workers were reaped. SHA-256 checks matched all 29 files listed by the normal `probe` report.

Two subsequent runs rejected window conditions while awaiting connection, captured zero frames, and reaped their workers. A third run attempted 11 captures. Frames 1, 6, and 7 succeeded at 1920×1200, while eight attempts were rejected by window checks. Its capture report is `interrupted`, with `finished_utc` and a worker return code. SHA-256 checks matched all 29 files listed across these three exceptional capture runs. The old logs used a shared focus-loss and minimization rejection message, so they cannot establish that both conditions were separately observed.

The interrupted run's outer PowerShell report finalized, but the old Python dependency report remained `installed` without `finished_utc`. The installation report now covers only the `dependency_setup` stage in `capture.probe.bootstrap/1.1` and writes its terminal state before starting the probe. The outer PowerShell report continues to summarize the whole launcher, and the probe report records capture results. Rejected-window snapshots and state booleans have local regression checks. Windows behavior with the fix still awaits [one interruption check](windows-validation.en.md#one-remaining-interruption-check).

## Remaining Work Requiring Actual Evidence

| Work | Missing evidence | Next action |
| --- | --- | --- |
| Fixed Windows launcher interruption and window diagnostics | Updated installation terminal state, finalized outer and capture reports, and live snapshots distinguishing focus loss from minimization | Return [one interruption check](windows-validation.en.md#one-remaining-interruption-check) to verify installation and capture report ownership and observed window state |
| Native same-frame capture and full task control | C01 behavior, real localization input, and Pipeline recognition templates | Integrate C02–C04 native completion records, bounded writes, input leases, automatic capture, and recovery without blind operations based on unknown screens |
| Game reconstruction settings and quality | Real multiview data, dynamic occlusion, HUD, usable camera groups, and held-out views | Calibrate filtering, masks, and reconstruction parameters; choose recapture or manual repairs |
| Automatic recapture and cross-region registration | Actual floor mappings, reachable viewpoints, trustworthy anchors, and errors | Add validated automated decisions to explicit transforms and revision constraints; bounding-box placement does not establish registration |
| Real-scene and mobile experience | Actual assets, target iOS / Android devices, and sustained interaction records | Check doorways, slopes, steps, boundaries, sustained performance, background recovery, and thermal behavior before setting final parameters |

macOS game capture, PlayCover, ADB, first person, WebGPU, learned geometry, and splat branches remain explicitly deferred options. The first version reconstructs original images with explicit camera groups; it does not promise automatic semantic cleanup, automatic hole repairs, or mask propagation across every tool version.

Return results to workspace `local/validation/windows-capture/<run-id>/`. New tests, samples, game images, masters, downloads, and logs stay off GitHub. Production code, schemas, and operating instructions are committed by feature.
