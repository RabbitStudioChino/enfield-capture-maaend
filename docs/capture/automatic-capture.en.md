# Automatic region capture

This page describes the local controls for automatic navigation and photography from a real map region or place name.  
Updated 2026-11-12.

## Launch

Extract the complete Windows x64 capture delivery into a new real directory. It needs matching `cpp-algo.exe`, `go-service.exe`, MaaFramework 5.14.3 DLLs, navigation meshes, localization models, map images and Pipeline resources. The package includes the official Python 3.12.10 cache and all locked dependencies in `local/cache/wheels/`. First launch prepares the environment offline and verifies wheel hashes. Later launches reuse the environment without downloading or reinstalling dependencies while Python and the lock file are unchanged. Run from the repository root.

```cmd
scripts\capture\capture.cmd serve
```

The local page defaults to `http://127.0.0.1:8780/`. Once Python is prepared, the service can also be launched directly. `--no-browser` keeps the address in the terminal.

```cmd
local\envs\capture-windows-py312\Scripts\python.exe -B -m tools.capture.server --workspace . --port 8780
```

The service binds the local loopback address. Choose another port if the default is occupied. Tasks, plans, logs and temporary files stay under the selected workspace's `local/` directory.

After moving the complete package to another real directory, double-click `Start-Region-Capture.cmd` there or run `scripts\capture\capture.cmd doctor`. The launcher continues resolving the root from its current script and checks the virtual environment's actual base Python and location. If it finds an earlier location or a broken environment, it refreshes `local/envs/capture-windows-py312` in place using the current package's Python and installs locked dependencies from the bundled wheels. Do not rewrite `pyvenv.cfg`'s `home` as a relative path or delete other `local/` data. Tasks, selections, photos and logs remain intact. Direct controls such as `status`, `pause` and `stop` only check the environment and do not automatically install dependencies. They send no command when the environment points to an earlier location. Stop any task in its original running window, then repair the environment through the launch entry in the new directory.

## Select a region and start

Enter the overworld in the game, then type a complete place name or select a region marker on the map. The catalog joins existing `teleport_anchors.json`, the SceneTeleport anchor registry and real teleport entries. “缘缘庄” corresponds to the real Yuanyuan Manor teleport anchor in Wuling, Si's Painting. An unmatched name or a name shared by multiple anchors requires an explicit region selection.

A name identifies an anchor location. The default capture range has a radius of 60 BaseNav map-image pixels around that anchor. Adjust the radius or click a polygon on the map. Defaults use 20-pixel spacing, 8 directions per point at 45° increments, and a budget of 160 sample points. Select a navigation tier or enter a floor height to distinguish stacked walkable surfaces at the same horizontal position.

“开始采集” (Start capture) plans reachable samples and immediately starts the task. “预览采集点” (Preview samples) separately displays the planned route, reachable point count, view count and rejection count. Drag to pan and scroll to zoom. Selection polygons, routes and sample points share the BaseNav map-image coordinate frame. The mesh display draws a sample of triangles, while planning uses the full navigation data.

After completing a polygon, enter “选区名称” (Selection name) in the map toolbar and click “保存选区” (Save selection). Its name, region anchor, map, 3–64 vertices and current radius, spacing, view count, point budget, tier and floor height are saved to `local/capture-ui/selections.json` in the selected workspace, without browser storage. Opening the same workspace again automatically reads the “已保存选区” (Saved selections) list. Click an entry to restore its range and parameters and clear the old plan. Loading does not start a task or automatically select the last entry. A tier that is still awaiting lookup or has become unavailable retains its original value and shows a notice; check it before previewing or starting. Saving the same name creates another entry. Click “删除” (Delete) to remove an entry.

The list holds at most 100 entries in a file of up to 1 MiB. Saves use a file lock and atomic replacement. A corrupt or unsupported file produces an explicit error and is preserved, rather than overwritten with a new list. Resource updates do not silently remove entries whose anchors or maps have become unavailable; loading them requests a new selection. Saving, reading and loading selections only access local files and the region catalog, without native navigation queries or game control.

Return to the game during the five-second preparation period after starting or resuming, and keep the game in the foreground with its window visible and restored. The task enters the selected area through an existing teleport entry, navigates to samples through `MapNavigateAction`, and uses the native photography action to adjust camera heading in a closed loop and receive same-frame images and localization. The game screen, unlocked maps, unlocked teleports and current account state affect execution.

Photography settling requires three consecutive valid position and camera-heading observations, with adjacent position changes no greater than 0.35 BaseNav map-image pixels and adjacent camera-heading changes no greater than 1°. Arrival radius, target heading, zone, black-screen, raw-resolution and same-frame localization checks remain enforced. Full-screen pixel changes from character animations, effects and moving objects no longer block settling. The native `result.json` records the method, consecutive observation count and last position and heading changes under `stability`. This check does not detect image blur or moving objects. Image selection and geometric quality still require later processing; settling does not constitute acceptance.

After updating the native photography program, create a new task from a saved selection or the original parameters. Earlier tasks freeze the SHA-256 values of their native runtime files and cannot resume with a different native program version. Preserve earlier tasks, images and logs as existing records.

## Task controls and export

The page displays the task ID, state, committed count, session, generation and local events. Pause, resume, stop or enter an earlier task ID to read its state. The navigation executor releases input and acknowledges commands. Pausing an automatic task closes its native connection. Resume uses a new session and clock while continuing the committed count. Committed batches remain immutable.

When a task pauses because its window loses focus or for another reason, the page continues reading checkpoints and events and displays the committed count and original pause reason. A reason containing `foreground=False` means the game was not in the foreground when checked. Keep its window visible and restored, click “恢复” (Resume), and return to the game within five seconds. Investigate other causes using the original error. Window protection remains enforced.

When a native teleport task fails or reaches its existing deadline, its error includes the last available node name and a diagnostic path. The `teleport` event's `result.diagnostic_uri` is relative to the workspace and points to JSON under `local/capture/<task-id>/runtime/navigation-<id>/diagnostics/`. The report records the teleport entry, actual SDK task status, node ID list, available recognition and action details for the last eight nodes, and log paths. Long raw details are truncated. The last recorded node is not a confirmed failure cause; `Native navigation operation failed` alone does not prove map recognition failed. The adjacent PNG preserves the SDK's last available scaled navigation screenshot and its actual dimensions. Its capture time is unknown, so it is not evidence of an original-resolution or exact failure-frame photograph. Keep the diagnostic JSON, PNG and listed logs. Detail retrieval, screenshot or write failures add an explanation while preserving the original task error.

“导出冻结包” (Export frozen bundle) selects a session observed by the current page or exports the latest session. Export returns a local directory after formal CaptureBundle validation. Uncommitted attempts and diagnostics remain in the task directory. Image selection stays `pending`. Navigation position and heading are coarse priors. Camera pose, pitch, FOV, native render time and geometric quality remain unknown. Offline processing starts with [input preparation](offline-preparation.en.md).

The CLI can start directly from a name. The source command entry defines the complete argument set. Use a new task ID.

```cmd
scripts\capture\capture.cmd auto --region "缘缘庄" --radius 60 --spacing 20 --views 8 --task yuanyuan-auto
```

## Data and execution boundaries

Planning freezes the catalog, map coordinate frame, selection, resource SHA-256 values, samples, real mesh heights and camera intents. Coordinate conversion uses existing BaseNav data. The overworld overview's `region3d` click coordinates serve the existing teleport interface. Resource changes require a new plan.

The local service checks Host, Origin, a random control token, JSON content type and body size. Static resources use an allowlist. Page loading and planning queries read resources. Explicit start or resume actions trigger game control. The independent world website continues to consume WorldRelease, while this page controls local capture tasks.

At the user's request, this round skips all tests, simulations and real-device validation while completing code and delivery builds. The feature remains unverified. See [implementation status](implementation-status.en.md) and [native same-frame records](native-capture.en.md) for related boundaries.
