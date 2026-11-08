# CaptureBundle 1.0

本目录是采集输入协议的权威来源。当前提供 JSON Schema 2020-12、只读离线校验器和自有合成样例。协议供后续采集器与重建消费者接入，实机采集与重建尚待实现和验收。

## 文件与版本

| 文件 | `schema_version` | 内容 |
| --- | --- | --- |
| `bundle.schema.json` | `capture.bundle/1.0` | 冻结包入口、来源和完整文件哈希索引 |
| `session.schema.json` | `capture.session/1.0` | 会话、时钟锚点、来源、控制器类型及标定组 |
| `plan.schema.json` | `capture.plan/1.0` | 单区域、单计划修订、路段、观察点与镜头意图 |
| `frame.schema.json` | `capture.frame/1.0` | 同帧身份、时间、图像、导航、方向与质量 |
| `batch.schema.json` | `capture.batch/1.0` | 已提交批次、不可变帧分片和输入来源快照 |
| `common.schema.json` | 无独立记录版本 | 上述 schema 的共享定义 |

读取器仅接受 `1.0`，未知 major 报 `unknown major`，其他版本报 `unsupported schema_version`。同版本的非关键附加字段可以被忽略。必填字段、时间、坐标或来源含义的改变须明确升级协议并更新接受版本。Schema 的 `$id` 用作本地注册标识，校验器使用本目录文件离线解析引用。

## 冻结包布局

```text
bundle.json
session.json
plan.json
frames.jsonl
batches/<batch_id>.json
records/<batch_id>.frames.jsonl
images/raw/<frame_id>.png
```

`bundle.json` 的 `status` 必须为 `frozen`。`files` 列出包内除 `bundle.json` 自身以外的全部文件及其 SHA-256，避免循环哈希。路径使用安全的 POSIX 相对路径，禁止绝对路径、反斜杠、冒号、`%`、`?`、`#`、空路径段、`.`、`..`、控制字符与符号链接。额外文件必须进入索引，未完成的写入产物须在导出前排除。

一个包限定一个会话、一个 `clock_id`、一个区域和一个计划修订。同进程暂停后恢复保留时钟。进程重启后恢复可以保留逻辑任务与检查点，但须新建 `session_id`、`clock_id` 和 `segment_id`，复制计划并更新会话身份，另行导出包。旧包保留独立，未来由重建输入的 `input_batch_ids` 关联多个会话。过图或位置断点通过新的 `segment_id` 表达，画面配置变更通过新的标定组表达。`plan.json` 中的观察点保存地图像素坐标及镜头意图，意图值不能作为实际观测。

## 不可变批次与导出

- 每个 `batches/*.json` 的 `status` 必须为 `committed`，通过 `frame_ids` 和 `frames_uri` 引用 `records/` 下已完成的独立帧分片。分片记录与冻结包的 `frames.jsonl` 聚合记录必须逐帧相同。
- `batch.files` 精确列出 `plan.json`、本批次帧分片及成功截图原图的 SHA-256。`input_revision` 对应计划修订号。批次引用已完成的版本化计划，包内计划的内容必须匹配该哈希。
- `batch.session_identity` 保存 `session_id`、`clock`、`source`、`controller_kind` 的不可变快照。`batch.calibration_groups` 保存本批次实际引用的完整标定组快照，两者均须与包内会话对应内容一致。
- 增长中的会话文件和全局帧日志不进入批次哈希。后续批次可以继续采集，导出器从已提交批次生成新的冻结聚合索引和包清单，保留旧批次文件。不同计划修订分别导出。
- 同一帧允许被多个批次引用，聚合索引内 `frame_id` 唯一，所有聚合帧均须被已提交批次引用。直接传入活动会话目录会因缺少冻结包入口或完整性条件失败。
- 所有非空 JSONL 文件均需以换行结束，逐行必须是完整的严格 JSON。校验拒绝重复 JSON 键、超出 IEEE-754 有限范围的数值、空白记录、部分记录、缺失文件和哈希不符。纳秒字符串保留整数精度。

此阶段提供校验能力。创建检查点、原子提交、冻结复制、恢复与导出执行器留待采集实现阶段。

## 帧、时间与坐标

`frame_id` 关联同一张图像与本次识别，`session_id`、`segment_id`、`waypoint_id`、`calibration_group_id` 均须可追溯。请求时间按日志顺序单调不减，且不早于会话单调锚点。纳秒使用十进制字符串，满足 `request <= complete <= locate_complete`。识别未运行时 `locate_complete` 为 `null`，各观测状态为 `not_run`。UTC 锚点使用以 `Z` 结尾的时间，未知映射误差保留 `null`。

`native_render_timestamp` 在 `1.0` 中固定为 `null`。主机请求与完成时间只描述截图 API 操作，无法证明游戏渲染帧时间。`source.kind` 显式区分 `synthetic` 和 `game_capture`，合成来源必须使用 `synthetic` 控制器。

截图成功时 `image` 包含 `uri`、`sha256`、宽高、`RGB8` 像素格式和来源后端，原图位于 `images/raw/`。截图失败时仅记录失败状态与原因，禁止图像路径、哈希、尺寸或后端字段，质量状态为 `rejected`。校验器校验字节哈希及尺寸与标定元数据的一致性，图像解码和像素质量检查由后续消费者负责。

平面位置、楼层、角色朝向、镜头水平朝向分别保存 `status` 与 `value`。`available` 必须有值，`unavailable`、`failed`、`not_run` 必须为 `null`。有效图像可以缺少导航结果，各种方向与定位可以独立失败。地图平面采用像素单位、X 向右、Y 向下，朝向采用从地图上方向顺时针的角度，楼层高度保留导航单位，尺度为 `arbitrary`。

导航观测仅服务于控制与粗先验。相机真值、FOV 和内参尚未获取时保留未知，`camera_estimate_ref` 与 `intrinsics_prior` 在本版本固定为 `null`。后续重建相机另行关联 `frame_id`，不得用小地图位置、输入时长或水平朝向填充完整相机位姿。

## 使用与检查

独立校验环境要求 Python 3.9+，依赖锁定在 [`tools/capture/requirements.lock`](../../tools/capture/requirements.lock)。MaaEnd 上游的 Python 3.14 工具链保持独立。以下命令在仓库根目录使用 POSIX shell 执行，虚拟环境、安装缓存和临时目录均位于仓库内。

```bash
mkdir -p .cache/tmp
python3 -m venv .cache/capture-venv
TMPDIR="$PWD/.cache/tmp" .cache/capture-venv/bin/python -m pip install --cache-dir .cache/pip -r tools/capture/requirements.lock
PYTHONDONTWRITEBYTECODE=1 .cache/capture-venv/bin/python -m tools.capture.validate contracts/capture/fixtures/synthetic-minimal
PYTHONDONTWRITEBYTECODE=1 .cache/capture-venv/bin/python -m unittest discover -s tools/capture/tests -v
```

命令校验通过返回 `0`，数据或文件错误返回 `1`，参数错误返回 `2`。Python 调用入口为 `tools.capture.validate.validate_bundle(directory)`，成功返回清单与帧、批次、文件数，失败抛出 `BundleValidationError` 或文件系统 `OSError`。测试临时副本固定在仓库 `.cache/capture-tests/` 内，测试结束自动清理。

[`fixtures/synthetic-minimal/`](./fixtures/synthetic-minimal/) 包含一个 16×12 RGB PNG 和两条合成帧记录。PNG 由 Python 标准库 `struct`、`zlib` 生成，每行使用 PNG filter `0`，像素为 `(x * 16, y * 20, (x + y) * 9)`，其中 `x=0..15`、`y=0..11`。单元测试验证 PNG 分块 CRC、尺寸及解压像素。第一帧模拟截图有效而导航缺失，第二帧模拟截图失败。所有身份、地图和时间均为合成数据，未运行游戏控制、实机导航或视觉重建。

后续阶段将定义 `ReconstructionBundle`、`PreviewSnapshot`、`RescanRequest`、相机求解与可执行补采协议，并接入真实同帧采集、事件日志、标定与导出验收。
