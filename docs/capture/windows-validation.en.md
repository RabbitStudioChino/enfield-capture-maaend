# Finite Windows Capture Validation

This document covers the M0/C01 Windows connection and finite capture probe. User acceptance on a Windows machine is required. Results use the `capture.probe/1.0` diagnostic format, with navigation and camera pose both set to `null`. A later same-frame localization and capture workflow will produce formal `CaptureBundle` packages.

## Environment Preparation

- Use Windows 10 or Windows 11, with Python 3.12 x64 installed.
- Manually start the game and enter a scene suitable for observation. Keep exactly one game window with `Endfield` in its title and the `UnityWndClass` window class.
- Disable Windows HDR and automatic color management. Keep game resolution and display scaling fixed. Keep the entire window visible within the monitor, restored, and in the foreground, with no overlay dialogs.
- Use the same privilege level for the capture process and the game. For permission errors, inspect the report and the game and terminal privilege settings, then restart the relevant programs yourself.
- Extract the minimal source ZIP to a normal local directory such as `D:\EndfieldCapture`. The directory and its `local` descendants should be real directories, without symlinks or NTFS junctions.

The probe connects only to an already running game. Input methods are fixed to `Null`. You move the character and camera manually. Connection and capture run in a separate SDK subprocess, which is terminated and reaped after a timeout or Ctrl+C. Internally, `ScreenDC` may restore game focus or move its window. Focus and minimization checks apply at checking boundaries, so races remain possible during capture.

## One-Click Execution

1. Double-click `scripts\capture\capture.cmd`, which runs `doctor` by default. It creates an isolated environment under `local\envs\capture-windows-py312` in the extracted directory and downloads locked dependencies. Installation logs and reports remain under `local\validation\windows-capture\<run-id>`.
2. After installation, a five-second countdown appears. Switch back to the game immediately and keep it in the foreground. `doctor` checks the system, Python and SDK versions, candidate windows, connection, and one original-size screenshot.
3. Open Command Prompt in the extracted directory and run the command below. Switch back to the game during the countdown, then manually change the viewing direction or position about every two seconds for 12 attempts.

```bat
scripts\capture\capture.cmd probe
```

If your PowerShell environment permits local scripts, these commands are also available. If policy blocks `.ps1`, use the `.cmd` entry point above. The scripts preserve the system execution policy and current process privileges.

```powershell
.\scripts\capture\capture.ps1 -Mode doctor
.\scripts\capture\capture.ps1 -Mode probe
```

## Command-Line Parameters

After local dependency installation, you can invoke the isolated environment directly. The workspace in this example is the extracted directory. Output always goes under its `local` directory.

```bat
local\envs\capture-windows-py312\Scripts\python.exe -m tools.capture doctor --workspace .
local\envs\capture-windows-py312\Scripts\python.exe -m tools.capture probe --workspace . --count 12 --interval 2 --start-delay 5
```

`count` ranges from 1–60, `interval` from 0.25–30 seconds, and `start-delay` from 0–60 seconds. `doctor` waits five seconds and attempts one frame. Intervals start when the previous attempt finishes. Overall duration also includes connection, capture, and disk writes. Press Ctrl+C in the console to stop. Completed records remain, and an in-flight attempt is recorded as failed or interrupted.

## Inspecting and Returning Results

Every run uses a new timestamp and random identifier, preserving earlier results. Dependency installation and capture reports may occupy separate directories.

```text
local/validation/windows-capture/<run-id>/
  report.json
  frames.jsonl
  images/frame-000001.png
  logs/worker.log
  sdk/
  .worker/
```

- Open successful PNG frames and confirm complete images, correct colors, the intended game, and dimensions matching the game client-area settings at capture time. The report records SDK raw resolution and `raw_size_enabled`. Returned dimensions must match the SDK raw resolution.
- Inspect `status`, `connection`, `errors`, `attempted`, `captured`, `failed`, and `worker` in `report.json`. Frame records include relative paths, SHA-256, dimensions, clocks, and independent failure reasons.
- Changing viewpoints manually should produce different images. Repeated content increments `duplicate_images` and records `duplicate_of_frame_id` in the frame. Static scenes can also produce identical images, so compare this with your actual actions.
- Separately check game focus loss, minimization, and Ctrl+C. Focus loss and minimization observed at a check should produce rejection or failure records. SDK internals may already have restored focus or adjusted the window, so record the actual behavior as well. After timeout or interruption, `worker.returncode` should be present. Incomplete `.partial` files are excluded from successful frames.
- Compress the relevant complete run directories under `local\validation\windows-capture` and return them. The Mac destination is `local/validation/windows-capture/` under the workspace root, preserving each `run-id`. Include directories containing failure reports and logs.

The exit code is `0` when all attempts succeed, `1` for partial success or failure, `2` for invalid arguments, and `130` for user interruption. If disk writes fail, the console reports that the report could not be written. Preserve that console error text for diagnosis.

## Failure Handling

| Symptom | Results to return and action |
| --- | --- |
| Missing Python or wrong version | Inspect the `capture.probe.bootstrap/1.0` setup report, install Python 3.12 x64, and run again |
| Dependency download or hash verification fails | Preserve the setup report and `logs/install.log`, check connectivity, and run again |
| Missing SDK, wrong version, or DLL load failure | Return package versions, the connection error, and `logs/worker.log` |
| Zero or multiple game windows | Return the candidate window list and confirm exactly one matching running game window |
| Focus loss, minimization, or changed window identity | Restore the game to the foreground and start a new run |
| Black image, empty image, or changed dimensions | Return the failed-frame reason, check HDR, window state, and fixed resolution, then run again |
| Connection or capture timeout | Preserve the report and worker log, including the PID, exit reason, and return code |

## SDK and Provenance

The lock fixes `maafw==5.14.3`, `numpy==2.5.4`, `pillow==13.0.0`, `strenum==0.5.0`, and `maaagentbinary==1.0.1`, using Windows Python 3.12 x64 wheel hashes. The 5.14.3 Windows x64 wheel SHA-256 is `5fcd48d85b2110150f19848da777564abe64e4ca64966704cb8dec948d243ce5`, matching upstream `uv.lock` and PyPI release metadata. [PyPI release metadata](https://pypi.org/pypi/maafw/5.14.3/json)

Window selection follows the `Win32-Front` configuration in `assets/interface.json` at MaaEnd baseline `8cccaaec8f59f4b4689b59db84a39d25066ff5ec`. The probe retains `Win32Controller` and `ScreenDC`, with input methods set to `Null`. This version's `ScreenDC` invokes helpers that restore foreground status and keep the window on screen, potentially changing its focus or position. The report records these side effects and checking boundaries. Window state cannot be guaranteed to remain unchanged throughout capture. [ScreenDC implementation](https://github.com/MaaXYZ/MaaFramework/blob/v5.14.3/source/MaaWin32ControlUnit/Screencap/ScreenDCScreencap.cpp) [Foreground helpers](https://github.com/MaaXYZ/MaaFramework/blob/v5.14.3/source/MaaWin32ControlUnit/Base/ForegroundUtils.h) [Connection implementation](https://github.com/MaaXYZ/MaaFramework/blob/v5.14.3/source/MaaWin32ControlUnit/Manager/Win32ControlUnitMgr.cpp)

Constructor parameters, raw-size settings, job polling, `get(wait=False)`, raw resolution, and BGR data conventions were verified against the 5.14.3 wheel. PNG writing converts BGR to RGB. Internal connection and capture waits are bounded at 10 and five seconds. The parent process bounds complete worker requests at 25 and 12 seconds respectively. [Python bindings](https://github.com/MaaXYZ/MaaFramework/blob/v5.14.3/source/binding/Python/maa/controller.py)

Native render-frame time is unavailable, so `native_render_timestamp` is `null` and `frame_freshness` is `unknown`. API completion times describe host operations. Local checks cover parameters, paths, worker timeout and reaping, failure persistence, and color conversion. Windows game capture, DPI, privileges, and user-operation acceptance still require live results. Automatic navigation, localization, camera solving, and reconstruction belong to later stages.
