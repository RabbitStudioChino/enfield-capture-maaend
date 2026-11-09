# Independent Implementation and Hardware Boundaries

Updated 2026-11-09 (Asia/Shanghai). This document separates implementation, tool integration, and actual game acceptance. The [detailed architecture](architecture.en.md) remains the design baseline.

## Implementation Independent of Windows Results

| Module | Implementation and entry point | Acceptance boundary |
| --- | --- | --- |
| Native same-frame records | Completion subscription, raw localization and normalized snapshots, bounded queue, optional passive PNG / JSONL diagnostics; [native integration](native-capture.en.md) | Full macOS build and independent regressions passed; Windows navigation remains untested, and diagnostics are not integrated into formal exports |
| Manual capture tasks | start / status / pause / stop / resume / logs / export, OS locks, command ACKs, checkpoint recovery, per-frame commits, frozen bundles; [manual CLI](manual-cli.en.md) | Null input with operator-controlled movement and camera; new Windows task acceptance remains outstanding, with automatic navigation and settling recognition unimplemented |
| Capture handoff | CaptureBundle validation, frozen input preparation, provenance and calibration groups, immutable copies, and transfer verification; [input preparation](offline-preparation.en.md) | Probe directories are not converted into fabricated capture sessions |
| Offline reconstruction | Tool capabilities and fingerprints, plans, staged execution, status, cancellation, checkpoint recovery, and artifact checks; [pipeline](reconstruction-pipeline.en.md) | Runs actual COLMAP / OpenMVS; process success does not establish geometry acceptance |
| Coordinates and feedback | COLMAP model parsing, camera conversion, explicit scale and coordinate revisions, statistics, PreviewSnapshot / RescanRequest | Without a reliable navigation mapping, suggestions remain for manual review and send no game commands |
| Blender master | Separate source revisions, Working / ManualPatches / Collision / Anchors, repeated imports and protection, coordinate round trips; [master tools](blender-export.en.md) | Arbitrary-scale sources can be retained; publication needs explicit scale evidence and an accepted revision |
| Release construction | Schemas pinned by Git commit and SHA, quality profiles and LOD derived from the master, embedded GLB, triangle collision, indexes, provenance, staging validation, and preservation of existing releases; [release tools](release-publishing.en.md) | Repairs, spawn placement, and collision still need checks in the actual scene |
| Independent roaming | Boxes / trimesh, keyboard and touch, free camera, safe return, context recovery, character heading, and available animations | The browser connects to neither MaaEnd nor the game |
| Resources and performance | Hash checks, bounded concurrency, content sharing and disposal, chunk residency, unavailable-boundary protection, LOD hysteresis, quality settings, optional automatic resolution, and runtime reports | Desktop or synthetic scenes cannot establish mobile frame rates or thermal behavior |

The [independent web repository](https://github.com/RabbitStudioChino/endfield-world-web) owns browser implementation and instructions. Triangle collision uses the separate `world.collision/2.0` format while retaining `1.0` boxes. The capture repository pins schemas without copying web runtime code.

## Actual Tool Checks Performed

The native module completed a full production `cpp-algo` compile and link using official MaaDeps v3.0.0, MaaFramework 5.14.3, OpenCV 4.12.0, and AppleClang 21. Independent tests and AddressSanitizer / UndefinedBehaviorSanitizer checks passed. Independent tests substitute controller screenshots and localization, so they do not establish in-game navigation acceptance. LeakSanitizer was not run in this macOS environment.

Local substituted-image task tests cover control commands, cross-process locks, pause and stop, restart continuation, corruption rejection, single-session exports, and multisession offline input. Actual local subprocesses exercised SIGINT and recovery after forced termination. CPython launch safety, dependency hashes, PowerShell argument binding, and setup-lock release passed local checks. PowerShell parsing and mocked execution used version 7.6.9 on macOS, which cannot replace Windows PowerShell 5.1 or actual SDK task acceptance. New `capture.plan/1.1` retains unknown positions and camera intent without relaxing the original 1.0 contract.

This workspace uses official COLMAP 4.2.1, OpenMVS 2.4.0, and the installed Blender. Downloads, original help output, logs, self-authored multiview images, and all check scripts remain in `local/`. A self-authored Blender scene has passed through sparse reconstruction, densification, meshing, and texturing. Output remains `needs_review`; geometry is not automatically accepted.

Actual Blender GLB / triangle-collision releases have been checked by the web validator and loaded in Chrome for character movement, quality switching, and runtime reports. Chunking, LOD, cancellation, and recovery have local checks. These do not establish acceptance of the real game world or mobile devices.

## Windows Hardware Evidence Received

Actual game reports and screenshots were received from the user on 2026-11-09. A normal `doctor` completed one frame, and a normal `probe` completed 12/12 frames, all recorded at the original 2560×1440 size. Their workers were reaped. SHA-256 checks matched all 29 files listed by the normal `probe` report.

Two subsequent runs rejected window conditions while awaiting connection, captured zero frames, and reaped their workers. A third run attempted 11 captures. Frames 1, 6, and 7 succeeded at 1920×1200, while eight attempts were rejected by window checks. Its capture report is `interrupted`, with `finished_utc` and a worker return code. SHA-256 checks matched all 29 files listed across these three exceptional capture runs. The old logs used a shared focus-loss and minimization rejection message, so they cannot establish that both conditions were separately observed.

The interrupted run's outer PowerShell report finalized, but the old Python dependency report remained `installed` without `finished_utc`. The installation report now covers only the `dependency_setup` stage in `capture.probe.bootstrap/1.1` and writes its terminal state before starting the probe. The outer PowerShell report continues to summarize the whole launcher, and the probe report records capture results. Rejected-window snapshots and state booleans have local regression checks. Windows behavior with the fix has not been retested.

On 2026-11-09 the user explicitly requested skipping abnormal-window tests and continuing development. These checks are deferred. Existing normal captures, window rejections, and interruption records remain available; deferred checks are not marked as passed and no longer block independent development.

## Remaining Work Requiring Actual Evidence

| Work | Missing evidence | Next action |
| --- | --- | --- |
| Fixed Windows launcher interruption and window diagnostics | Updated installation terminal state, finalized outer and capture reports, and live snapshots distinguishing focus loss from minimization | Defer abnormal-window checks at the user's request, retain the hardware-validation limits, and continue independent development |
| Manual tasks and frozen exports | Concurrent controls, ACKs, restart continuation, and actual frozen bundles from the new Windows entry point | Verify the normal start / pause / resume / stop / export loop, actual images, and immutable old sessions, without abnormal-window checks |
| Native integration and automated capture | Native Windows build and actual navigation, localization input, and Pipeline recognition templates | Verify C02 passive records, then integrate photography actions, input leases, and formal tasks; the implemented manual C04 subset does not establish automated-mode completion |
| Game reconstruction settings and quality | Real multiview data, dynamic occlusion, HUD, usable camera groups, and held-out views | Calibrate filtering, masks, and reconstruction parameters; choose recapture or manual repairs |
| Automatic recapture and cross-region registration | Actual floor mappings, reachable viewpoints, trustworthy anchors, and errors | Add validated automated decisions to explicit transforms and revision constraints; bounding-box placement does not establish registration |
| Real-scene and mobile experience | Actual assets, target iOS / Android devices, and sustained interaction records | Check doorways, slopes, steps, boundaries, sustained performance, background recovery, and thermal behavior before setting final parameters |

macOS game capture, PlayCover, ADB, first person, WebGPU, learned geometry, and splat branches remain explicitly deferred options. The first version reconstructs original images with explicit camera groups; it does not promise automatic semantic cleanup, automatic hole repairs, or mask propagation across every tool version.

Return results to workspace `local/validation/windows-capture/<run-id>/`. New tests, samples, game images, masters, downloads, and logs stay off GitHub. Production code, schemas, and operating instructions are committed by feature.
