# 人工走位采集 CLI

本文说明 Windows 人工走位任务的命令、文件提交和恢复边界。  
更新日期 2026-11-09。

移动、转向和镜头由操作者控制。CLI 连接已运行的游戏窗口，使用 MaaFramework `ScreenDC` 截图，键鼠后端均为 `Null`。任务不运行导航、停稳识别或自动跑图。有效 PNG 的质量标记保持 `pending`，未知位置、FOV、相机位姿和原生渲染时间保持未知。

## Windows 操作

以下命令在解压后的采集仓库根目录运行。`capture.cmd` 默认使用该目录作为工作区，所有依赖、任务、日志、原图和导出均落在其 `local/` 内。使用另一工作区时可直接调用 PowerShell 脚本的 `-Workspace` 参数。`doctor`、`probe`、`start` 运行受锁保护的环境准备。其余命令使用已准备的环境。

先启动游戏并进入要采集的区域。在终端 A 执行以下命令，然后在五秒内切回游戏。每个观察点只要求采到一张完整图像，操作者自行改变站位和镜头以获得可重建的视角。

```cmd
scripts\capture\capture.cmd doctor
scripts\capture\capture.cmd start --task manual-demo --region-label "缘缘庄首轮" --count 12 --interval 5 --start-delay 5
```

在终端 B 查询或控制同一任务。切出游戏可能使正在进行的截图被窗口门控拒绝，任务进入 `paused`，已提交图像保留。`resume` 后有五秒切回游戏的预检准备时间，预检重新连接并截图。不要把这类操作当作异常窗口状态的实机验收通过。

```cmd
scripts\capture\capture.cmd status --task manual-demo
scripts\capture\capture.cmd pause --task manual-demo --command-id pause-001
scripts\capture\capture.cmd logs --task manual-demo
scripts\capture\capture.cmd resume --task manual-demo --command-id resume-001
scripts\capture\capture.cmd stop --task manual-demo --command-id stop-001
scripts\capture\capture.cmd export --task manual-demo
```

`start` 与进程重启后的 `resume` 在当前终端持续运行。`Ctrl+C` 停止该任务并保留已确认批次。`logs --follow` 持续输出事件，日志查看器的 `Ctrl+C` 只退出查看。已完成任务不能再次恢复。新任务应使用新的 `--task`。

`status` 输出 `generation` 和 `session_id`。控制命令可显式传 `--generation <generation>`，旧 generation 收到拒绝 ACK。相同 `--command-id` 和动作重复提交返回原 ACK，相同 ID 换动作或 generation 被拒绝。等待 ACK 超时会保留命令，重复同一 ID 可查询原处理结果。

直接使用准备好的 Python 环境时，与脚本调用相同的业务入口如下。命令执行位置仍为仓库根目录。

```cmd
local\envs\capture-windows-py312\Scripts\python.exe -B -m tools.capture status --workspace . --task manual-demo
```

## 持久化与恢复

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

任务锁和工作区控制器锁由操作系统持有，进程退出即释放。本工作区的人工任务只有持锁进程连接采集控制器。锁不覆盖外部 MaaEnd 或其他工作区。命令通过原子文件交接，CLI 不监听网络端口。控制检查在工作器等待期间按 25 ms 轮询，收到 `pause` 或 `stop` 后终止工作器，保存检查点，再确认 ACK。清理有有限等待，真实 Windows 的停止延迟仍需测量。

每张成功截图依次验证 PNG、字节哈希、API 时间和标定尺寸，持久化原图与不可变帧分片，最后原子发布单帧 `committed` 批次。只有批次完成后推进观察点。失败或取消保留原始尝试诊断，不产生完成观察点。没有完成批次的孤儿图像、记录和 `.partial` 保留诊断用途，不进入冻结包。

恢复先重新核验所有已提交批次的哈希及逻辑观察点，再计算下一未完成点。检查点写入落后于批次时，恢复不会重拍已经确认的点。已提交文件缺失或损坏会拒绝恢复和导出，原件保持可诊断。任务不自动用重复尝试掩盖失败，截图、连接或画幅异常进入 `paused`，文件系统错误进入 `failed`。

同进程暂停和恢复保留不可变会话、时钟及标定。画幅变化会暂停，需先 `stop`，再从另一进程 `resume`，以新会话接续逻辑观察点。进程重启后始终新建 `session_id`、`clock_id`、帧 ID、计划及路段 ID。每个观察点独立一个路段，不能由编号推断连续轨迹。任务级来源、区域及地图坐标定义保持一致，便于离线消费者显式组合多个会话。

## 冻结与离线读取

`export` 默认导出最新会话，输出一个新目录并打印完整路径。可用 `--session <session_id>` 导出旧会话。导出仅复制该会话已提交批次和其输入，生成聚合 `frames.jsonl` 与文件清单，运行正式 `validate_bundle` 后才原子发布。原始会话和已有导出不覆盖。

以下示例中的路径用 `export` 输出替换。多会话输入可重复提供 `--bundle`。离线制作读取质量待审图像，并不代表图像匹配、相机求解或几何质量已经验收。

```cmd
local\envs\capture-windows-py312\Scripts\python.exe -B -m tools.capture.validate local\capture\manual-demo\exports\bundle-<id>
```

离线 `prepare` 的依赖环境和命令见[重建 CLI 文档](offline-preparation.md)。人工计划使用 `capture.plan/1.1`，位置及镜头意图为 `null`，导航观测为 `not_run/null`。原 `capture.plan/1.0` 的坐标要求保持原有语义，其余 CaptureBundle 记录继续使用 `1.0`。

## Windows 验证结果返还

真实 Windows 任务闭环尚待用户验证。请记录 `start`、`status`、`pause`、`resume`、`stop`、进程退出后再 `resume` 和各会话 `export` 的输出，确认截图来自当前游戏画面，旧导出字节不变，恢复后观察点编号接续且会话 ID 更新。

预期成功输出包含 `committed` 进度、控制命令的 `applied` ACK、结束后的 `completed` 或主动停止的 `stopped`，冻结包验证输出 `VALID`。窗口失焦、尺寸变化、磁盘或连接错误必须如实保留，不能记为完成。对正在进行的请求取消时，未确认帧不会进入完成计数。

将本次任务整个 `local/capture/<task>/`、相关 `local/validation/windows-capture/` 启动报告及终端输出，复制到工作区 `local/validation/windows-capture/<run-id>/` 后返还。真实游戏素材和日志只留本地，不上传 GitHub。异常窗口状态专项测试按用户选择跳过，未标记通过。自动导航及原控制器的 C++ 同帧接入另行验证。
