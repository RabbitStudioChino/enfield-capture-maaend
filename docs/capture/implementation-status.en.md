# Independent Implementation and Hardware Boundaries

Updated 2026-11-09 (Asia/Shanghai). This document separates implementation, tool integration, and actual game acceptance. The [detailed architecture](architecture.en.md) remains the design baseline. The latest instruction skips all tests for the new automatic-region work. Static review and production packaging continue; no hardware handoff is required to finish coding.

## Implementation Independent of Windows Results

| Module | Implementation and entry point | Acceptance boundary |
| --- | --- | --- |
| Native same-frame records | Completion subscription, raw and normalized localization, bounded passive diagnostics and native `CaptureViewAction`; [native integration](native-capture.en.md) | Earlier checks cover the passive implementation only; new photography is untested |
| Manual capture tasks | start / status / pause / stop / resume / logs / export, locks, ACKs, immutable commits and checkpoints; [manual CLI](manual-cli.en.md) | Manual mode retains Null input; shared lifecycle changes are untested this round |
| Automatic region capture | Search or select an anchor, draw an area, plan reachable Navmesh coverage, teleport, navigate, confirm yaw and settling, capture the same raw frame, resume and automatically export; [automatic capture](automatic-capture.en.md) | Code integrated; all tests and game validation skipped. Named anchors do not define full area boundaries; settling thresholds remain uncalibrated |
| Capture handoff | CaptureBundle validation, frozen input preparation, provenance and calibration groups, immutable copies, and transfer verification; [input preparation](offline-preparation.en.md) | Probe directories are not converted into fabricated capture sessions |
| Offline reconstruction | Tool capabilities and fingerprints, plans, staged execution, status, cancellation, checkpoint recovery, and artifact checks; [pipeline](reconstruction-pipeline.en.md) | Runs actual COLMAP / OpenMVS; process success does not establish geometry acceptance |
| Coordinates and feedback | COLMAP model parsing, camera conversion, explicit scale and coordinate revisions, statistics, PreviewSnapshot / RescanRequest | Without a reliable navigation mapping, suggestions remain for manual review and send no game commands |
| Blender master | Separate source revisions, Working / ManualPatches / Collision / Anchors, repeated imports and protection, coordinate round trips; [master tools](blender-export.en.md) | Arbitrary-scale sources can be retained; publication needs explicit scale evidence and an accepted revision |
| Release construction | Schemas pinned by Git commit and SHA, quality profiles and LOD derived from the master, embedded GLB, triangle collision, indexes, provenance, staging validation, and preservation of existing releases; [release tools](release-publishing.en.md) | Repairs, spawn placement, and collision still need checks in the actual scene |
| Independent roaming | Boxes / trimesh, keyboard and touch, free camera, safe return, context recovery, character heading, and available animations | The browser connects to neither MaaEnd nor the game |
| Resources and performance | Hash checks, bounded concurrency, content sharing and disposal, chunk residency, unavailable-boundary protection, LOD hysteresis, quality settings, optional automatic resolution, and runtime reports | Desktop or synthetic scenes cannot establish mobile frame rates or thermal behavior |

The [independent web repository](https://github.com/RabbitStudioChino/endfield-world-web) owns browser implementation and instructions. Triangle collision uses the separate `world.collision/2.0` format while retaining `1.0` boxes. The capture repository pins schemas without copying web runtime code.

## Historical Tool Checks Before Automatic Capture

The native module completed a full production `cpp-algo` compile and link using official MaaDeps v3.0.0, MaaFramework 5.14.3, OpenCV 4.12.0, and AppleClang 21. Independent tests and AddressSanitizer / UndefinedBehaviorSanitizer checks passed. Independent tests substitute controller screenshots and localization, so they do not establish in-game navigation acceptance. LeakSanitizer was not run in this macOS environment.

Local substituted-image task tests cover control commands, cross-process locks, pause and stop, restart continuation, corruption rejection, single-session exports, and multisession offline input. Actual local subprocesses exercised SIGINT and recovery after forced termination. CPython launch safety, dependency hashes, PowerShell argument binding, and setup-lock release passed local checks. PowerShell parsing and mocked execution used version 7.6.9 on macOS, which cannot replace Windows PowerShell 5.1 or actual SDK task acceptance. New `capture.plan/1.1` retains unknown positions and camera intent without relaxing the original 1.0 contract.

This workspace uses official COLMAP 4.2.1, OpenMVS 2.4.0, and the installed Blender. Downloads, original help output, logs, self-authored multiview images, and all check scripts remain in `local/`. A self-authored Blender scene has passed through sparse reconstruction, densification, meshing, and texturing. Output remains `needs_review`; geometry is not automatically accepted.

Actual Blender GLB / triangle-collision releases have been checked by the web validator and loaded in Chrome for character movement, quality switching, and runtime reports. Chunking, LOD, cancellation, and recovery have local checks. These do not establish acceptance of the real game world or mobile devices.

## Windows Hardware Evidence Received

Actual game reports and screenshots were received from the user on 2026-11-09. A normal `doctor` completed one frame, and a normal `probe` completed 12/12 frames, all recorded at the original 2560×1440 size. Their workers were reaped. SHA-256 checks matched all 29 files listed by the normal `probe` report.

Two subsequent runs rejected window conditions while awaiting connection, captured zero frames, and reaped their workers. A third run attempted 11 captures. Frames 1, 6, and 7 succeeded at 1920×1200, while eight attempts were rejected by window checks. Its capture report is `interrupted`, with `finished_utc` and a worker return code. SHA-256 checks matched all 29 files listed across these three exceptional capture runs. The old logs used a shared focus-loss and minimization rejection message, so they cannot establish that both conditions were separately observed.

The interrupted run's outer PowerShell report finalized, but the old Python dependency report remained `installed` without `finished_utc`. The installation report now covers only the `dependency_setup` stage in `capture.probe.bootstrap/1.1` and writes its terminal state before starting the probe. The outer PowerShell report continues to summarize the whole launcher, and the probe report records capture results. Rejected-window snapshots and state booleans have local regression checks. Windows behavior with the fix has not been retested.

On 2026-11-09 the user explicitly requested skipping abnormal-window tests and continuing development. These checks are deferred. Existing normal captures, window rejections, and interruption records remain available; deferred checks are not marked as passed and no longer block independent development.

## Unverified Behavior and Data-Dependent Quality

| Work | Missing evidence | Next action |
| --- | --- | --- |
| Fixed Windows launcher interruption and window diagnostics | Actual finalized reports and separate focus/minimization evidence | Deferred; no testing requested in this round |
| Manual tasks and frozen exports | Actual task ACKs, restart continuation and immutable bundles | Preserve the code and historical evidence; all new testing is skipped |
| Native integration and automated capture | Actual teleport, routes, raw same-frame photography, camera/settling calibration | Complete code and Windows production packaging; no tests or device gate this round |
| Game reconstruction settings and quality | Real multiview data, dynamic occlusion, HUD, usable camera groups, and held-out views | Calibrate filtering, masks, and reconstruction parameters; choose recapture or manual repairs |
| Automatic recapture and cross-region registration | Actual floor mappings, reachable viewpoints, trustworthy anchors, and errors | Add validated automated decisions to explicit transforms and revision constraints; bounding-box placement does not establish registration |
| Real-scene and mobile experience | Actual assets, target iOS / Android devices, and sustained interaction records | Check doorways, slopes, steps, boundaries, sustained performance, background recovery, and thermal behavior before setting final parameters |

macOS game capture, PlayCover, ADB, first person, WebGPU, learned geometry, and splat branches remain explicitly deferred options. The first version reconstructs original images with explicit camera groups; it does not promise automatic semantic cleanup, automatic hole repairs, or mask propagation across every tool version.

No test results are requested in this round. Runtime data stays in workspace `local/capture/<task-id>/`, and delivery archives stay in `local/deliverables/`. New tests, samples, game images, masters, downloads and logs stay off GitHub. Production code, schemas and instructions are committed by feature.
