# Automatic region capture

This page describes the local controls for automatic navigation and photography from a real map region or place name.  
Updated 2026-11-09.

## Launch

Extract the complete Windows x64 capture delivery into a new real directory. It needs matching `cpp-algo.exe`, `go-service.exe`, MaaFramework 5.14.3 DLLs, navigation meshes, localization models, map images and Pipeline resources. The launcher prepares Python 3.12.10 and locked dependencies on first use. SDK dependency installation requires network access. Run from the repository root.

```cmd
scripts\capture\capture.cmd serve
```

The local page defaults to `http://127.0.0.1:8780/`. Once Python is prepared, the service can also be launched directly. `--no-browser` keeps the address in the terminal.

```cmd
local\envs\capture-windows-py312\Scripts\python.exe -B -m tools.capture.server --workspace . --port 8780
```

The service binds the local loopback address. Choose another port if the default is occupied. Tasks, plans, logs and temporary files stay under the selected workspace's `local/` directory.

## Select a region and start

Enter the overworld in the game, then type a complete place name or select a region marker on the map. The catalog joins existing `teleport_anchors.json`, the SceneTeleport anchor registry and real teleport entries. “缘缘庄” corresponds to the real Yuanyuan Manor teleport anchor in Wuling, Si's Painting. An unmatched name or a name shared by multiple anchors requires an explicit region selection.

A name identifies an anchor location. The default capture range has a radius of 60 BaseNav map-image pixels around that anchor. Adjust the radius or click a polygon on the map. Defaults use 20-pixel spacing, 8 directions per point at 45° increments, and a budget of 160 sample points. Select a navigation tier or enter a floor height to distinguish stacked walkable surfaces at the same horizontal position.

“开始采集” (Start capture) plans reachable samples and immediately starts the task. “预览采集点” (Preview samples) separately displays the planned route, reachable point count, view count and rejection count. Drag to pan and scroll to zoom. Selection polygons, routes and sample points share the BaseNav map-image coordinate frame. The mesh display draws a sample of triangles, while planning uses the full navigation data.

Return to the game during the five-second preparation period after starting. The task enters the selected area through an existing teleport entry, navigates to samples through `MapNavigateAction`, and uses the native photography action to adjust camera heading in a closed loop and receive same-frame images and localization. The game screen, unlocked maps, unlocked teleports and current account state affect execution.

## Task controls and export

The page displays the task ID, state, committed count, session, generation and local events. Pause, resume, stop or enter an earlier task ID to read its state. The navigation executor releases input and acknowledges commands. Pausing an automatic task closes its native connection. Resume uses a new session and clock while continuing the committed count. Committed batches remain immutable.

“导出冻结包” (Export frozen bundle) selects a session observed by the current page or exports the latest session. Export returns a local directory after formal CaptureBundle validation. Uncommitted attempts and diagnostics remain in the task directory. Image selection stays `pending`. Navigation position and heading are coarse priors. Camera pose, pitch, FOV, native render time and geometric quality remain unknown. Offline processing starts with [input preparation](offline-preparation.en.md).

The CLI can start directly from a name. The source command entry defines the complete argument set. Use a new task ID.

```cmd
scripts\capture\capture.cmd auto --region "缘缘庄" --radius 60 --spacing 20 --views 8 --task yuanyuan-auto
```

## Data and execution boundaries

Planning freezes the catalog, map coordinate frame, selection, resource SHA-256 values, samples, real mesh heights and camera intents. Coordinate conversion uses existing BaseNav data. The overworld overview's `region3d` click coordinates serve the existing teleport interface. Resource changes require a new plan.

The local service checks Host, Origin, a random control token, JSON content type and body size. Static resources use an allowlist. Page loading and planning queries read resources. Explicit start or resume actions trigger game control. The independent world website continues to consume WorldRelease, while this page controls local capture tasks.

At the user's request, this round skips all tests, simulations and real-device validation while completing code and delivery builds. The feature remains unverified. See [implementation status](implementation-status.en.md) and [native same-frame records](native-capture.en.md) for related boundaries.
