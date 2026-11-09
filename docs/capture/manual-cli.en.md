# Manually positioned capture CLI

This document describes Windows manual capture commands, durable commits and recovery boundaries.  
Updated 2026-11-09.

The operator controls movement, heading and camera. The CLI connects to the running game and captures through MaaFramework `ScreenDC`, with both input backends set to `Null`. It runs no navigation, settling recognition or automatic exploration. Valid PNG images retain quality `pending`. Position, FOV, camera pose and native render timestamps remain unknown.

## Windows operation

Run these commands at the extracted capture repository root. `capture.cmd` uses this directory as its workspace by default, with dependencies, tasks, logs, originals and exports under `local/`. The PowerShell script also accepts `-Workspace` for another root. `doctor`, `probe` and `start` prepare the environment under an installation lock. Other commands use the prepared environment.

Start the game and enter the target region. Run the following in terminal A, then return to the game within five seconds. Each observation slot requires one complete image. The operator chooses positions and camera views suitable for reconstruction.

```cmd
scripts\capture\capture.cmd doctor
scripts\capture\capture.cmd start --task manual-demo --region-label "缘缘庄首轮" --count 12 --interval 5 --start-delay 5
```

Query or control the same task in terminal B. Switching away from the game may cause the window gate to reject an ongoing capture and enter `paused`, preserving committed images. `resume` provides five seconds to return to the game before reconnecting and taking a preflight screenshot. These operations do not establish acceptance of the exceptional-window-state tests.

```cmd
scripts\capture\capture.cmd status --task manual-demo
scripts\capture\capture.cmd pause --task manual-demo --command-id pause-001
scripts\capture\capture.cmd logs --task manual-demo
scripts\capture\capture.cmd resume --task manual-demo --command-id resume-001
scripts\capture\capture.cmd stop --task manual-demo --command-id stop-001
scripts\capture\capture.cmd export --task manual-demo
```

`start` and a `resume` after process exit keep running in their terminal. `Ctrl+C` stops the task and preserves confirmed batches. `logs --follow` streams events, and its `Ctrl+C` only exits the viewer. Completed tasks cannot resume. Use a new `--task` for another capture.

`status` returns `generation` and `session_id`. Control commands accept explicit `--generation <generation>`, and stale generations receive a rejected ACK. Repeating the same `--command-id` and action returns the original ACK. Reusing an ID with another action or generation is rejected. An ACK timeout preserves the pending command, whose original result can be queried by repeating its ID.

The prepared Python environment exposes the same business entry point. Run this command at the repository root as well.

```cmd
local\envs\capture-windows-py312\Scripts\python.exe -B -m tools.capture status --workspace . --task manual-demo
```

## Persistence and recovery

```text
local/capture/<task>/
  task.json
  checkpoint.json
  commands/<command_id>.json
  acks/<command_id>.json
  events/<time>-<id>.json
  runtime/worker-<id>/
  sessions/<session_id>/
    session.json
    plan.json
    images/raw/<frame_id>.png
    records/<frame_id>.frames.jsonl
    batches/<frame_id>.json
  exports/bundle-<id>/
```

The operating system owns the task and workspace controller locks, releasing them on process exit. A manual task in this workspace connects only while holding those locks. They do not cover external MaaEnd instances or other workspaces. Commands use atomic files, and the CLI opens no network listener. Worker waits poll controls every 25 ms. A `pause` or `stop` terminates the worker, saves the checkpoint and then acknowledges the command. Cleanup waits are bounded, while actual Windows stop latency still needs measurement.

Each successful screenshot undergoes PNG, byte hash, API timing and calibration-size validation. The original and immutable frame shard are persisted before atomically publishing a one-frame `committed` batch. Only that commit advances the observation slot. Failed or cancelled attempts preserve original diagnostics and complete no slot. Orphan images, records and `.partial` files without a completed batch remain diagnostics and stay outside frozen bundles.

Recovery rechecks all committed batch hashes and logical observation IDs before finding the next unfinished slot. A checkpoint lagging behind a batch does not cause that confirmed slot to be recaptured. Missing or damaged committed files prevent recovery and export, preserving originals for diagnosis. Failures cause no automatic retry loop. Capture, connection or dimension errors enter `paused`, while filesystem errors enter `failed`.

In-process pause and resume preserve immutable session, clock and calibration metadata. Dimension changes pause the task. Use `stop`, then `resume` from another process to continue in a new session. Process restarts always create new session, clock, frame, plan and segment identities. Each observation has its own segment, so numbering implies no continuous trajectory. Task-level source, region and map-coordinate definitions remain stable for explicit multi-session offline inputs.

## Freezing and offline consumption

`export` defaults to the latest session, creates a fresh output directory and prints its full path. Select an older session with `--session <session_id>`. Export copies only committed batches and their inputs, builds aggregate `frames.jsonl` and the file inventory, and atomically publishes after the formal `validate_bundle` succeeds. Original sessions and previous exports are preserved.

Replace the example path with the `export` result. Multiple sessions can be supplied using repeated `--bundle` arguments. Offline preparation includes images whose quality is pending, which establishes no acceptance of matching, solved cameras or geometry quality.

```cmd
local\envs\capture-windows-py312\Scripts\python.exe -B -m tools.capture.validate local\capture\manual-demo\exports\bundle-<id>
```

See [offline preparation](offline-preparation.en.md) for its environment and commands. Manual plans use `capture.plan/1.1` with null positions and camera intentions and `not_run/null` navigation observations. The existing coordinate semantics of `capture.plan/1.0` remain intact. Other CaptureBundle records continue to use `1.0`.

## Returning Windows validation results

The actual Windows task workflow awaits user validation. Record the outputs of `start`, `status`, `pause`, `resume`, `stop`, `resume` after process exit, and each session's `export`. Check that images show the current game, previous export bytes remain identical, and recovery continues observation numbering with a new session ID.

Expected successful output includes `committed` progress, `applied` control ACKs, `completed` on completion or `stopped` after an explicit stop, and `VALID` for frozen bundle validation. Preserve actual focus, dimension, disk and connection failures rather than counting them as complete. Cancelling an ongoing request excludes unconfirmed frames from the completion count.

Copy the complete `local/capture/<task>/`, relevant startup reports under `local/validation/windows-capture/` and terminal output into workspace `local/validation/windows-capture/<run-id>/` before returning results. Real game content and logs remain local and are never uploaded to GitHub. Exceptional-window-state testing was deferred at the user's request and is not marked passed. Automatic navigation and the original controller's C++ same-frame integration require separate validation.
