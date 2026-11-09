# 离线图像输入准备

本文说明 R01 的输入准备子集：把已冻结的 `CaptureBundle` 整理成可跨机搬运的原图输入。它不运行 COLMAP、OpenMVS 或 Blender，不是正式 `ReconstructionBundle`，也不表示图像质量、相机、尺度或重建已验收。R00、C04 和完整重建 DAG 的前置验收仍待完成；[Windows 截图诊断](windows-validation.md) 的 `capture.probe/1.0` 目录不能直接作为冻结包输入。

## 运行条件

使用 Python 3.9 或更高版本，以及现有 CaptureBundle 校验器依赖 `jsonschema`、`referencing`。从采集仓库根目录运行。依赖环境、下载缓存和字节码缓存均应放在工作区 `local/`；可设置 `PYTHONDONTWRITEBYTECODE=1` 避免生成源码旁的字节码。无需安装游戏 SDK 或重建工具。

`--workspace` 是工作区的真实目录绝对路径；每个 `--bundle` 也使用该工作区内的真实目录绝对路径。输出固定进入工作区 `local/reconstruction/<job>/`。`--job` 使用 1–64 个 ASCII 字母、数字、连字符或下划线，以字母或数字开头，排除 Windows 保留名称。

```bash
python -m tools.reconstruction --help
python -m tools.reconstruction prepare --help
python -m tools.reconstruction verify --help
python -m tools.reconstruction prepare --workspace /absolute/workspace --bundle /absolute/workspace/local/capture/bundle-a --job region-a-001
python -m tools.reconstruction verify --workspace /absolute/workspace --job region-a-001
```

合并多个包时重复传入 `--bundle`，每个包只列一次。人工走位任务的冻结导出见[任务 CLI](manual-cli.md)，真实 Windows 任务闭环仍待验收。本地自制合成包只能检查工具逻辑，必须保留其 `source.kind=synthetic` 来源。

```bash
python -m tools.reconstruction prepare --workspace /absolute/workspace --bundle /absolute/workspace/local/capture/bundle-a --bundle /absolute/workspace/local/capture/bundle-b --job region-a-002
```

## 输入筛选与兼容边界

- 每个包先由 `tools.capture.validate.validate_bundle` 校验正式 schema、冻结状态、完整文件索引、SHA-256、帧分片及 committed 批次。不接受活动会话、探针目录、坏尾 JSONL、缺文件或哈希不符的包；复制结束后再次验证源包。
- 同一作业要求 `region_id`、完整 `map_coordinates`、`controller_kind` 和除 `description` 外的全部 `source` 字段一致。来源检查包含 `kind`、`game_version`、采集器版本、上游和子模块提交。契约没有独立地图版本字段，地图身份以 `coordinate_frame_id` 及其余地图坐标字段判断；未知的 `game_version=null` 保持未知，不能证明游戏版本一致。
- 只复制 `image.status=captured` 且 `quality.selection_status` 不是 `rejected` 的原图。`pending` 仍是待质量检查，不升级成 accepted。失败、拒选帧保留映射与原因，但不放入 `images/`。全部无可用原图时作业失败。
- 图像扩展名限于 `.png`、`.jpg`、`.jpeg`、`.bmp`、`.tif`、`.tiff`，保留原字节和扩展名类型。只验证字节来源与哈希，不解码图片，不确认实际尺寸、画面内容或特征可匹配性。
- 输出图名由 `(session_id, frame_id)` 的规范序列化 SHA-256 生成，避免不同会话的同名帧覆盖。重复帧只有在完整原始记录一致时才去重，所有来源仍被记录；同一会话的标定快照冲突会失败。
- 标定组按 `(session_id, calibration_group_id)` 分开保存，即使尺寸或原始组名相同也不自动合并。后续 COLMAP 必须显式决定每组与求解相机的映射，不可直接默认所有图像共享单相机。

## 输出与复核

```text
local/reconstruction/<job>/
  preparation.json
  frames.jsonl
  calibration-groups.json
  images/<identity-sha256>.png
  sources/<bundle-sha256>/bundle.json
  sources/<bundle-sha256>/session.json
  sources/<bundle-sha256>/plan.json
  sources/<bundle-sha256>/frames.jsonl
  sources/<bundle-sha256>/batches/...
  sources/<bundle-sha256>/records/...
```

`preparation.json` 使用内部格式 `reconstruction.preparation/1.0`，记录工具版本、作业标识、兼容性、来源包哈希和全部输出文件的 SHA-256 与字节数，排除清单自身以避免循环哈希。来源目录保存原冻结包的 JSON / JSONL 元数据及其原哈希关系，不复制被排除的原图。`frames.jsonl` 保存每个源帧的完整原始记录、来源包、选入图像路径和标定组映射，原导航观察和未知相机字段均不修改。

`images/` 是实际副本，不是硬链接或符号链接，可作为后续 COLMAP 图像输入候选。源原图不修改；准备目录里的文件提交前设为只读，工具拒绝覆盖已有作业。文件只读属性不是防篡改权限；搬运整个作业目录后，在新工作区的相同 `local/reconstruction/<job>/` 位置运行 `verify`。复核不需要原始包或原工作站绝对路径。

`verify` 重新计算所有输出文件哈希和字节数，拒绝缺失与额外文件，核对来源元数据、帧映射、标定组和兼容性闭包。它确认准备输入的自洽与字节完整性，不提供发布者认证或重建质量验收；修改清单并重新计算哈希不能建立可信来源。

成功输出类似 `PREPARED INPUTS 12 images; reconstruction=not_run; quality=not_assessed`，退出码为 `0`；校验或文件操作失败为 `1`，命令行参数错误为 `2`，Ctrl+C 为 `130`。所有状态固定表明尚未处理图像、去除 HUD、求解相机或重建，尺度为 unknown，标定组仍需显式映射。有效图像或成功准备不等于真实重建可行。

## 写入与失败恢复

作业使用同一输出父目录的 `.<job>.lock` 排他目录锁，先写 `.<job>.<random>.partial/`，完整复核后在同一卷重命名为最终目录。已存在的作业、同名并发作业、路径逃逸、符号链接、Windows reparse point / junction 均被拒绝；输入包不能包含输出 staging。

进入 staging 后失败会保留 `failure.json`（Ctrl+C 记录 cancelled 并释放锁） 与已写入的诊断内容，控制台指出该未提交目录；最终作业目录不会发布部分内容。目录不可写、锁已存在或尚未进入 staging 时，仅能保留控制台错误。存储不可写也可能阻止失败报告落盘，应保存终端输出。确认进程已退出后，可人工检查或删除失败 staging；异常终止遗留的锁需确认无进程使用后手工移除，工具不会自动接管锁或续跑失败任务。

当前只完成不依赖 Windows 实机的离线输入准备。HUD / 角色遮罩、图像预处理、相机组人工映射、COLMAP 特征与配准、OpenMVS、尺度依据、质量门槛及 Blender 制作仍未执行；Windows 实机结果仍按[验证说明](windows-validation.md)返还。

## 本地代码检查

工作区的测试与合成样本不提交 GitHub。具备本地测试工作区时，在采集仓库根目录运行以下命令；`/absolute/workspace/local/envs/offline/bin/python` 表示已安装校验依赖的本地解释器。

```bash
PYTHONDONTWRITEBYTECODE=1 /absolute/workspace/local/envs/offline/bin/python /absolute/workspace/local/qa/capture/test_reconstruction.py -v
```

测试覆盖原图副本、跨机会话映射、重复与冲突帧、来源和地图冲突、空输入、损坏与活动包、复制期源变化、锁与覆盖保护、路径链接和复核拒绝篡改。Windows junction 只做标志模拟，未代替真实 NTFS 或 Windows 游戏验收。
