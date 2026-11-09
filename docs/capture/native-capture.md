# 原生同帧完成记录与被动写入

更新日期为 2026-11-09。C02 在既有 `PositionProvider` 上增加完成订阅，并通过现有 `MapNavigateAction` 提供可选的被动诊断录制。任务层的人工采集与正式包交付见对应 CLI 说明，原生诊断数据使用独立格式。

## 完成订阅

`capture()` 和 `captureForStrictGoal()` 每次调用分配本次 `session_id`、`clock_id` 与递增 `frame_id`。每个 provider 持有独立会话及 UTC / 单调时钟锚点，作用域内所有尝试共用该时钟。完成记录包含截图请求、截图完成、定位起止与整体完成的主机单调时间。`native_render_timestamp` 保留为 `null`，`frame_freshness` 为 `unknown`。

完成出口覆盖空输出参数、截图失败、无图、小地图提取失败、定位失败、精确位置拒绝、朝向缺失与异常。`LocateResult` 保存本帧原始位置、人物朝向和镜头朝向。归一化前后位置分别记录，调用方可通过 `setPositionNormalizer(normalizer, normalization_id)` 指定转换标识。现有导航调用未提供转换标识时保留 `null`。本帧定位没有测得高度，因此完成快照的 `floor_y` 为 `null`。

旧的 `setFrameObserver()` 仍在定位前运行，严格目标采集的黑屏检查仍先于旧 observer。新增 `subscribeCompletion()` 返回不可复制的作用域 token，单个 provider 同时接受一个完成订阅。注册、注销和 provider 调用均在原有导航线程进行。token 可以安全晚于 provider 销毁。

完成回调同步接收 `const FrameCompletion&` 与借用的 `const cv::Mat&`，图像内存在回调返回前有效。异步消费者应先申请队列容量再复制图像。完成回调异常由 provider 隔离，旧 observer、定位器和归一化器的异常继续沿原调用路径传播。

## 有界写入与故障

`BoundedFrameWriter` 使用单个后台线程，默认同时限制 8 帧与 256 MiB 未压缩图像。容量统计覆盖正在复制、排队和写盘中的图像。预留成功后才 `clone()`，队列满、单帧超限、图像格式不支持或停止后提交均立即返回具体 `SubmitResult`，调用方同时持有对应的完成记录与 `frame_id`。

`stats()` 提供接受、写成、丢弃、中止、当前容量、高水位与错误信息。停止默认排空已接受帧，`stop(false)` 丢弃排队帧并允许当前写入结束。磁盘或编码失败会关闭接收、记录失败并通知错误回调，后台线程上的回调应只传递故障信号。

`NativeFrameStore` 在后台将 PNG 写入 `.partial`，完成 flush / fsync 后改名，再追加并同步 JSONL。同一 provider 的帧号允许出现丢帧缺口，重复或逆序记录会被拒绝。诊断输出按新会话创建目录，已有目录和文件受到覆盖保护。故障时保留已写数据、临时文件与可能的孤儿图像，供本地诊断。

`summary.json` 保存丢帧总数、最后丢帧 ID 和原因。当前被动诊断提供有界的丢帧摘要，逐次拒绝明细需要在正式任务接入时扩展。带 `.partial`、缺少 summary 或 summary 失败的目录应作为未完成诊断保留。正式 `CaptureBundle` 的冻结、哈希清单和恢复校验由任务层负责。

## 通过既有导航入口启用

编译更新后的 `cpp-algo` 并让宿主加载该版本。在启动宿主的 PowerShell 中设置真实工作区绝对路径，然后按原有方式运行已配置的 `MapNavigateAction`。

```powershell
$env:MAAEND_NATIVE_CAPTURE_WORKSPACE = (Resolve-Path '.').Path
```

输出位于该工作区的 `local/native-capture/<session_id>/`，包含 `session.json`、`frames.jsonl`、`images/` 和停止时的 `summary.json`。工作区及输出目录必须为真实目录，路径中的符号链接和 Windows reparse point 会被拒绝。环境变量未设置时保持现有导航行为。

`PassiveRecorder` 在导航调用内持有 writer 与订阅，退出时先注销再排空。写入故障只从后台设置原子故障标志，导航线程通过现有 `should_stop` 路径停止扫描器并释放移动输入。最后排空阶段的写入或 summary 故障同样使该导航调用返回失败。录制只观察既有导航截图，摄影采样、输入租约和新的 Pipeline 状态属于后续任务接入。

## 本地验证与实机边界

本工作区使用官方 MaaDeps v3.0.0、MaaFramework 5.14.3 macOS arm64 SDK、OpenCV 4.12.0 和 AppleClang 21 完成生产 `cpp-algo` 全量编译链接。独立回归使用真实 SDK 图像 buffer、OpenCV、PNG 和 JSONL，截图请求及定位结果通过测试替身注入。覆盖所有完成出口、旧 observer 顺序、异常与 token 生命周期、内存所有权、包含写入中图像的容量限制、拒绝、排空、中止、磁盘故障和中文路径。

新增测试、构建副本、依赖、样本与日志均保存在工作区 `local/`。独立测试入口位于 `local/qa/capture/native/`，通过独立 CMake 目标编译，测试 `main` 位于生产源码递归收集范围之外。Windows 原生运行、真实定位与持续导航中的被动录制仍待对应实机证据，当前可继续独立任务开发。
