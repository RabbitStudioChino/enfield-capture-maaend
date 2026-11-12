# Windows 有限截图验证

本文保留 M0/C01 诊断探针说明。最新指令跳过所有测试，本轮不要求执行以下步骤或回传结果。正常使用见[自动区域采集](automatic-capture.md)。探针使用独立 `capture.probe/1.0` 诊断格式，正式 CaptureBundle 由人工或自动任务生成。

## 环境准备

- 使用 Windows 10 或 Windows 11。启动脚本会自动准备项目内 CPython 3.12.10 x64。
- 手动启动游戏并进入可自由观察的场景，保持唯一标题包含 `Endfield`、窗口类为 `UnityWndClass` 的游戏窗口。
- 关闭 Windows HDR 与自动颜色管理，固定游戏分辨率与显示缩放。保持窗口可见、未最小化且位于前台，整个窗口完整位于显示器内，避免覆盖弹窗。
- 采集进程与游戏使用相同权限等级。遇到权限错误时按报告确认游戏与终端的权限配置，再由用户重新启动对应程序。
- 将新版交付 ZIP 解压到新的普通本地目录，例如 `D:\EndfieldCapture`，避免沿用旧目录的虚拟环境。目录及其 `local` 子目录应为真实目录，避免符号链接或 NTFS junction。

探针只连接已经运行的游戏，输入方式固定为 `Null`，用户自行移动角色和镜头。连接和截图运行在独立 SDK 子进程中，发生超时或 Ctrl+C 后回收该子进程。`ScreenDC` 内部可能恢复游戏前台或移动窗口，前台与最小化检查只在检查边界生效，截图期间仍存在竞态。

## 一键运行

1. 运行 `scripts\capture\capture.cmd doctor`。它先校验本地缓存的官方 Python 包，再展开到 `local\runtime\python-3.12.10`，随后创建 `local\envs\capture-windows-py312`，从包内 `local\cache\wheels` 离线安装锁定依赖。Python 与锁文件未变化时，后续运行复用已有环境。控制台显示环境与依赖安装进度，报告和日志留在 `local\validation\windows-capture\<run-id>`，失败后窗口保留。
2. 安装结束后出现 5 秒倒计时，立即切回游戏并保持前台。`doctor` 检查系统、Python、SDK 版本、候选窗口、连接与一张原尺寸截图。
3. 打开命令提示符并切换到解压目录，运行以下命令。出现倒计时后切回游戏，约每两秒手动换一个观察方向或位置，采集 12 次。

```bat
scripts\capture\capture.cmd probe
```

`.cmd` 使用系统 PowerShell，并仅为本次子进程设置 `ExecutionPolicy Bypass`。运行时、虚拟环境、缓存和临时文件全部位于解压目录的 `local` 下。也可在 PowerShell 中运行以下命令。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\capture\capture.ps1 -Mode doctor
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\capture\capture.ps1 -Mode probe
```

## 命令行参数

完成本地依赖安装后，可直接调用独立环境。以下示例中的工作区为解压目录，输出固定进入该目录的 `local` 子目录。

```bat
local\envs\capture-windows-py312\Scripts\python.exe -m tools.capture doctor --workspace .
local\envs\capture-windows-py312\Scripts\python.exe -m tools.capture probe --workspace . --count 12 --interval 2 --start-delay 5
```

`count` 范围为 1–60，`interval` 范围为 0.25–30 秒，`start-delay` 范围为 0–60 秒。`doctor` 固定等待 5 秒并尝试一帧。间隔从上次尝试结束开始计算，整体耗时还包含连接、截图和写盘时间。控制台按 Ctrl+C 可以结束当前运行，已写入的记录保留，进行中的尝试记录为失败或中断。

## 检查与返回结果

每次运行使用新的时间戳与随机标识目录，保留旧结果。Python 引导报告、依赖安装报告和采集报告可能分别占用一个目录。Python 引导目录中有 `report.json`、`install.log` 与版本检查日志，依赖安装日志为 `logs/install.log`。

PowerShell 外层报告使用 `capture.probe.bootstrap/1.0`，`stage=setup_and_capture` 汇总依赖准备与后续采集。Python 依赖安装报告使用 `capture.probe.bootstrap/1.1`，`stage=dependency_setup`，在安装成功后写入终态 `installed` 与 `finished_utc`，随后才启动探针。安装报告只描述安装阶段，采集终态以 `capture.probe/1.0` 报告为准。采集失败或中断后，已经完成的安装报告保持 `installed`。旧版安装报告中的 `capture_exit_code` 已移除。

```text
local/validation/windows-capture/<run-id>/
  report.json
  frames.jsonl
  images/frame-000001.png
  logs/worker.log
  sdk/
  .worker/
```

- 打开成功帧 PNG，确认画面完整、颜色正常、确实来自目标游戏，宽高符合当时的游戏客户区设置。报告中记录 SDK 原始分辨率和 `raw_size_enabled`，探针要求返回尺寸与 SDK 原始分辨率一致。
- 查看采集 `report.json` 的 `status`、`connection`、`errors`、`attempted`、`captured`、`failed` 和 `worker`。截图记录包含相对路径、SHA-256、宽高、时钟与独立失败原因。窗口检查拒绝时，连接或帧记录的 `window` 保留本次检查快照，拒绝文本附 `visible`、`minimized`、`foreground` 布尔值。窗口已经关闭或未取得快照时为 `null`。
- 手动改变视角后应看到不同画面。重复内容会计入 `duplicate_images`，帧内记录 `duplicate_of_frame_id`。静止场景也可能产生相同图片，需结合实际操作判断。
- 分别检查游戏失焦、最小化及 Ctrl+C 的情况。在检查时观察到失焦和最小化应产生拒绝或失败记录，SDK 内部仍可能先恢复前台或调整窗口，需一并记录实际现象。超时与中断后 `worker.returncode` 应存在。未完整写完的 `.partial` 文件排除在成功帧之外。
- 把本次 `local\validation\windows-capture` 下相关完整运行目录压缩返回。Mac 接收位置为工作区根目录的 `local/validation/windows-capture/`，保留 `run-id`。包含失败报告与日志的目录同样需要返回。

探针全部成功退出码为 `0`，部分成功或失败为 `1`，参数错误为 `2`，用户中断为 `130`。PowerShell 中断流水线时，外层 `child_exit_code` 可能为 `null`，应同时检查外层报告与采集报告。磁盘不可写时控制台会显示报告写入失败，保留控制台错误文本供排查。

## 可选的异常窗口与中断补测

2026-11-09 返回的正常 `doctor` 与 12 张 `probe` 截图已核对报告及文件哈希，无需重复。用户已明确跳过异常窗口专项并继续开发；以下步骤作为后续可选检查保留，不作为当前开发前置条件。需要恢复该专项时，使用包含修复的交付目录运行 `scripts\capture\capture.cmd probe`。

1. 倒计时内切回游戏，让探针成功截图。随后短暂切出游戏、将游戏最小化并恢复，每种状态保留约两秒，以便本轮记录窗口快照。
2. 在 12 次采集结束前切回控制台，按一次 Ctrl+C，等待入口退出或显示暂停提示。保留控制台中断及退出文字。
3. 检查本轮三个报告目录。依赖安装报告应为 `capture.probe.bootstrap/1.1`、`stage=dependency_setup`、`status=installed` 且已有 `finished_utc`。外层报告应有中断终态与 `finished_utc`。采集报告应为 `interrupted` 且有 `finished_utc`，`worker.returncode` 应存在。连接或失败帧的 `window` 与布尔值应能说明实际观察到的窗口条件。
4. 将本轮三个完整报告目录和控制台文字一起返回工作区 `local/validation/windows-capture/<run-id>/`，保留各自 `run-id`。SDK 可能在检查间恢复窗口前台，未记录到某个状态时照实说明。

此前一次中断已证明探针报告能够收尾并回收工作进程，但旧版 Python 安装报告缺少 `finished_utc`。安装报告生命周期已经调整，修复后的 Windows 入口中断收尾尚未复测；该限制继续保留，不标记为实机通过。

## 失败处理

| 现象 | 返回内容与处理 |
| --- | --- |
| Python 包下载、哈希或运行时校验失败 | 返回 Python 引导报告与 `install.log`，确认使用新版交付包并解压到新目录；缓存哈希不符时按错误提示移除损坏的 `.nupkg` 后重试 |
| 离线依赖缺失、哈希验证或安装失败 | 保留安装报告与 `logs/install.log`，将完整新版交付包解压到新目录；离线安装失败不会改用联网下载 |
| SDK 缺失、版本错误或 DLL 加载失败 | 返回报告中的包版本、连接错误与 `logs/worker.log` |
| 游戏窗口为零个或多个 | 返回候选窗口列表，确认游戏正在运行且只有一个匹配窗口 |
| 失焦、最小化或窗口身份变化 | 回到游戏前台并恢复窗口，再开始新的运行 |
| 黑屏、空图或尺寸变化 | 返回失败帧原因，确认 HDR、窗口状态与固定分辨率后重新运行 |
| 连接或截图超时 | 保留报告和工作进程日志，由报告确认 PID、退出原因与退出码 |

## SDK 与来源依据

Python 使用官方 NuGet `python/3.12.10` x64 包，缓存路径为 `local/cache/python/python.3.12.10.nupkg`。新版交付 ZIP 内置此缓存，首次准备 Python 可直接离线展开。固定 SHA-256 为 `0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8`，包内保留 CPython 许可。缓存缺失时下载限制为 120 秒并允许最多五次重定向，哈希通过后才展开，运行前检查版本、x64 架构、`venv` 与 `ensurepip`。[Python 官方 NuGet 说明](https://docs.python.org/3.12/using/windows.html#the-nuget-org-packages) [固定包版本](https://www.nuget.org/packages/python/3.12.10)

全部 11 个锁定的 Windows wheel 随包保存在 `local/cache/wheels/`。打包时逐项检查版本、完整性与 SHA-256；首次安装使用 `--no-index --find-links`，保留 `--require-hashes --only-binary=:all:`，无需访问 PyPI。已有匹配 Python 版本和锁文件哈希的环境直接复用。交付包缺少 wheel 目录时拒绝安装；目录内缺包或哈希不符时安装失败，不联网回退。源码目录未提供 wheel 缓存时仍可从 PyPI 安装，300 秒整体超时或安装失败后会提示检查 `pypi.org`、`files.pythonhosted.org`，或改用完整离线交付包。

锁文件固定 `maafw==5.14.3`、`numpy==2.5.4`、`pillow==13.0.0`、`strenum==0.5.0` 和 `maaagentbinary==1.0.1`，使用 Windows Python 3.12 x64 wheel 哈希。5.14.3 Windows x64 wheel 的 SHA-256 为 `5fcd48d85b2110150f19848da777564abe64e4ca64966704cb8dec948d243ce5`，与上游 `uv.lock` 及 PyPI 发布元数据一致。[PyPI 发布元数据](https://pypi.org/pypi/maafw/5.14.3/json)

窗口选择依据为 MaaEnd 基线 `8cccaaec8f59f4b4689b59db84a39d25066ff5ec` 的 `assets/interface.json` 中 `Win32-Front` 配置。探针沿用 `Win32Controller` 与 `ScreenDC`，输入方式设为 `Null`。该版本 `ScreenDC` 会调用恢复前台和确保窗口位于屏幕内的辅助函数，可能改变窗口前台或位置。报告明确记录这些副作用及检查边界，无法保证截图期间窗口状态绝对不变。[ScreenDC 实现](https://github.com/MaaXYZ/MaaFramework/blob/v5.14.3/source/MaaWin32ControlUnit/Screencap/ScreenDCScreencap.cpp) [前台辅助实现](https://github.com/MaaXYZ/MaaFramework/blob/v5.14.3/source/MaaWin32ControlUnit/Base/ForegroundUtils.h) [连接实现](https://github.com/MaaXYZ/MaaFramework/blob/v5.14.3/source/MaaWin32ControlUnit/Manager/Win32ControlUnitMgr.cpp)

已核验 5.14.3 wheel 中的构造参数、原尺寸开关、作业状态轮询、`get(wait=False)`、原始分辨率和 BGR 数据约定。PNG 写入前转换为 RGB。连接内部等待上限 10 秒、截图 5 秒，主进程分别以 25 秒、12 秒限制整个工作进程请求。[Python 绑定](https://github.com/MaaXYZ/MaaFramework/blob/v5.14.3/source/binding/Python/maa/controller.py)

原生渲染帧时间不可获取，因此 `native_render_timestamp` 为 `null`，`frame_freshness` 为 `unknown`。API 完成时间用于主机操作记录。本地验证覆盖参数、路径、超时回收、失败持久化、颜色转换、安装报告收尾及窗口失败快照。已收到真实 Windows 游戏连接、正常原尺寸截图和中断记录，详细范围见[实现状态](implementation-status.md)。不同 DPI / 权限组合、失焦与最小化分别命中的证据，以及修复后的入口中断收尾仍待核验。自动跑图、定位、相机求解与重建属于后续阶段。
