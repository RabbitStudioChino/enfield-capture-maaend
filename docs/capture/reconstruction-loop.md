# 多轮重建、审阅与补采

本文说明可恢复工程、COLMAP 粗重建、外部 GPT 审阅和受约束补采。更新日期 2026-11-12。英文版见 [Reconstruction review loop](./reconstruction-loop.en.md)。

## 支持范围与当前验证状态

每轮把历史冻结采集包与新增冻结包一起重建，只执行 COLMAP 特征、匹配、联合 mapper 与稀疏质量分析。历史输入、轮次、审阅结果和最终任务分别保留。特征及已验证匹配可以复用，mapper 仍对本轮全部输入联合求解；当前没有局部增量 SfM、局部 MVS、自动补洞或透明材质恢复。

数值门槛和外部图像审阅都通过后，才把该轮最大的连通重建模型冻结复制到独立最终任务，执行去畸变、完整 OpenMVS 稠密重建、网格、精修和贴图。较小模型不能替代仍不合格的最大模型。最终产物只代表通过门槛的模型部分，继续标为 `needs_review`，不代表整个区域或游戏已验收。

本次开发遵照用户要求跳过全部测试、模拟和游戏实机验证；仅进行了源码审查、格式整理、Python 3.12 静态编译和交付打包。本文命令为实际使用步骤，尚未在真实多轮游戏数据上验收。图像遮罩、孔洞、动态污染、尺度、碰撞与设备表现仍需检查。

## 第一轮采集与搬运

在 Windows 中打开运行中的游戏，从采集仓库根目录执行。区域名称必须对应本机真实资源；`auto` 会控制游戏移动与镜头，启动后切回游戏。也可使用既有一键脚本打开采集页并选区。

```powershell
$Workspace = (Resolve-Path ..).Path
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m tools.capture auto --workspace $Workspace --region "真实区域名称" --task first-round --max-points 24 --views 8
python -m tools.capture status --workspace $Workspace --task first-round
python -m tools.capture export --workspace $Workspace --task first-round
```

记录 `export` 返回的完整 `bundle` 目录。跨机复制整个冻结目录到离线工作区 `local/inputs/first-round/`，保持 `bundle.json`、图像和元数据相对路径。原始图像、自动计划和候选点均留在本地。只有新版本自动采集冻结包包含可执行补采需要的 `auto-plan.json` 与 `rescan-candidates.json`；旧包仍可重建和人工审阅。

在另一台机器继续同一个重建工程或通过 Windows 页面补采时，单独搬运审阅目录不够。还需保持同版代码与契约，并复制 `local/reconstruction-projects/<project_id>/` 整个目录、其引用的 `local/reconstruction/<job>/` 和 `local/reconstruction-runs/<run_id>/`，保持相对结构。原始 CaptureBundle 可单独用于新工程；已有审阅则必须验证完整轮次与粗解证据。冻结 JSON 中的绝对路径仅为原机器定位记录，不应为换机而篡改；新的实际工具路径通过新 `doctor` 清单提供。

## 创建工程与执行粗轮

以下离线命令从采集仓库根执行，Python 需安装 `tools/capture/requirements.lock` 依赖。工具自行安装，`doctor` 只探测本机工具，不下载安装。缺少 OpenMVS 时 `ready=false`，只要 COLMAP 可用仍能执行粗轮；最终精修才要求全部工具。

```sh
export WORKSPACE="$(cd .. && pwd)"
export PYTHONDONTWRITEBYTECODE=1
python3 -m tools.reconstruction doctor --workspace "$WORKSPACE" \
    --output "$WORKSPACE/local/validation/reconstruction-tools/loop-001"
```

将以下配置保存为 `$WORKSPACE/local/reconstruction-config.json`，位于父工作区而非仓库内。`camera_group_mapping` 的占位项用于满足既有配置格式，工程会按每个真实会话和标定组生成独立映射。相机模式或 FOV 未知时不共享内参；不会根据相同分辨率盲目合并标定。`camera_params=null` 表示分别估计内参。每轮联合求解重新建立 SfM 坐标，因此工程会清除旧世界变换，保留任意尺度，制作阶段再显式对齐。

```json
{
    "schema_version": "reconstruction.config/1.0",
    "camera_model": "PINHOLE",
    "camera_params": null,
    "camera_group_mapping": {"project-generated": "camera-01"},
    "stage_timeout_seconds": 3600,
    "max_image_size": 1600,
    "world_frame_id": null,
    "scale_status": "arbitrary",
    "transform_world_from_sfm": null,
    "parent_revision": null,
    "random_seed": 0
}
```

```sh
python3 -m tools.reconstruction project-create --workspace "$WORKSPACE" --project region-loop \
    --config "$WORKSPACE/local/reconstruction-config.json" \
    --toolchain "$WORKSPACE/local/validation/reconstruction-tools/loop-001/toolchain.json"
python3 -m tools.reconstruction project-add-round --workspace "$WORKSPACE" --project region-loop \
    --bundle "$WORKSPACE/local/inputs/first-round"
python3 -m tools.reconstruction project-run --workspace "$WORKSPACE" --project region-loop
python3 -m tools.reconstruction project-status --workspace "$WORKSPACE" --project region-loop
```

工程位于 `local/reconstruction-projects/region-loop/`。`inputs/<sha256>/` 保存完整冻结包；`rounds/round-0001/round.json` 绑定该轮全部来源、准备作业和运行 ID；实际日志、阶段、缓存证据与耗时账位于 `local/reconstruction-runs/<run_id>/`。后续轮始终追加全部历史来源，按 `(session_id,frame_id)` 去重并拒绝冲突身份。

每轮验证全部准备输入、二进制指纹、配置与检查点。上一轮特征和匹配数据库只有在旧图像内容、相机映射、提取配置、COLMAP 指纹以及文件哈希一致时才作为种子；无效配置重新提取，损坏缓存明确失败。数据库复制后再次校验哈希。恢复复用已成功且文件清单一致的阶段前缀，其他旧尝试移入 `attempts/`。`features/cache.json` 记录实际复用或失效原因。

## 导出与导入外部审阅

```sh
python3 -m tools.reconstruction project-review --workspace "$WORKSPACE" --project region-loop \
    --round round-0001 --review review-001
```

输出包含 `review_file`、`response_template`、冻结来源目录和审阅包目录。`review.json` 绑定工程、轮次、真实重建修订、来源包、自动计划、候选目录和资源哈希。`frame-index.json` 列出全部帧、会话、航点、地图坐标、可用层高、朝向观测、标定与原图哈希；`sources/` 保存来源元数据，`cameras.json` 和 `sparse-model/` 保存实际粗解。失败轮输出真实错误及有界日志尾部，不生成假几何。

包内最多复制 16 张问题或参考原图，未生成裁剪图；全部原图仍保留在冻结来源。原图清单和少量附件不等于全图审阅，默认图像及区域都是 `unreviewed`。如外部审阅需要更多图像，按索引从对应本地来源提供；没有查看的图像必须保持未审阅。文件夹可手动压缩，压缩包须放在审阅目录之外，避免破坏冻结清单。

```sh
mkdir -p "$WORKSPACE/local/review-exports"
python3 -m zipfile -c "$WORKSPACE/local/review-exports/review-001.zip" \
    "$WORKSPACE/local/reconstruction-projects/region-loop/reviews/review-001"
```

把包交给自己选择的外部 GPT，按 `REVIEW.txt` 审阅并返回填写后的 `response-template.json`；系统不绑定 API、不读取模型密钥，也不自动上传图像。响应要求真实审阅者说明、完整互斥的 `reviewed/unreviewed/failed` 帧清单、区域覆盖、问题证据及 `accepted_models`。问题分类为 `source_image_quality`、`insufficient_observation`、`difficult_material`、`localization_or_tool_failure`、`insufficient_evidence`。仅凭注册率或重投影误差不能断言材质、模糊或缺拍原因。难材质和证据不足不会因生成补采动作就自动解决。

将返回文件保存到本地新路径，再导入。导入检查冻结审阅清单、当前轮修订、全量覆盖、模型摘要、图像证据与补采来源；旧轮结果不可用于当前补采。结果按内容哈希存档，并用追加收据保存明确顺序，重复导入相同内容不会改变优先级。`project-status` 返回最后导入的覆盖与剩余未解决问题。

```sh
python3 -m tools.reconstruction project-import --workspace "$WORKSPACE" --project region-loop \
    --response "$WORKSPACE/local/review-results/review-001.json"
```

## 受约束补采与下一轮

在 Windows 采集页刷新工程列表、选择工程和轮次，可导出审阅、导入结果并预览 `rescan`。也可直接使用 `python -m tools.capture serve --workspace <工作区>` 启动页面。预览展示每个问题、来源图像、动作、候选目标、朝向、预算及不可执行原因；预览只读文件。点击开始后再次校验来源、轮次、去重、预算及当前原生导航可达性，通过后使用现有任务的暂停、恢复、停止和进度。

`reconstruction.rescan/2.0` 只能引用该冻结源的可达候选 ID，并给出朝向和问题理由。单份请求绑定一个源包及其原自动计划，问题证据可引用审阅包全部来源；跨来源动作应分成不同请求。模型不能填写任意地图坐标、楼层或执行路径。本实现没有新的 3D→导航映射或新点生成；候选无法表达的孔洞区域应使用 `classification=insufficient_evidence`、`status=unresolved`，在说明中写明 `needs_mapping`，保持 `rescan=null`，不能执行。既有 `reconstruction.rescan/1.0` 继续作为 `automatic_navigation=false` 的人工协议，不能当成自动请求。

补采任务结束后用既有 `export` 导出新冻结包，搬到离线工作区，再追加一轮。补采帧保留动作与问题关联，不会覆盖旧图。补采启动成功、截图已保存以及新一轮数值改善分别是不同状态，都不等于问题已解决。

```sh
python3 -m tools.reconstruction project-add-round --workspace "$WORKSPACE" --project region-loop \
    --bundle "$WORKSPACE/local/inputs/rescan-round-002"
python3 -m tools.reconstruction project-run --workspace "$WORKSPACE" --project region-loop
python3 -m tools.reconstruction project-review --workspace "$WORKSPACE" --project region-loop \
    --round round-0002 --review review-002
```

## 预算、停机与恢复

工程默认策略如下，都是未经过真实游戏标定的工程门槛。可在创建时用 `--policy <本地JSON>` 提供完整策略，字段定义见 `contracts/reconstruction/project.schema.json`。策略与工程配置冻结后不覆盖修改。

| 字段 | 默认值 |
| --- | --- |
| `max_rounds` | 8 轮 |
| `max_images` | 4000 张，累计去重后的已采截图，包含被筛除图像 |
| `max_compute_seconds` | 28800 秒，包含失败、重试和最终精修 |
| `max_stagnant_rounds` | 连续 2 次无明显改善 |
| `min_relative_improvement` | 0.02 |
| `max_rescan_actions` | 工程累计 128 个动作 |
| `thresholds.min_registered_images` | 6 |
| `thresholds.min_registration_ratio` | 0.6 |
| `thresholds.min_sparse_points` | 500 |
| `thresholds.min_mean_track_length` | 2.5 |
| `thresholds.max_reprojection_error_p95_px` | 3 像素 |
| `thresholds.max_connected_components` | 1 |

质量停滞比较真实粗解的 `registered_images × registration_ratio / (1 + p95_error)`，相对改善未超过策略值则累加；工具失败不算质量停滞。达到轮数、图像或计算预算时给出明确停止原因，刚获准的最后一轮仍可执行。补采预览还会扣除未并入重建轮次的已有任务截图预留。计算账在进程启动前持久化；崩溃后无法证明的活动时间保守计入预算，可能包含停机时间。

最新轮失败后重复 `project-run` 恢复。取消使用状态中的真实 `run_id`：`python3 -m tools.reconstruction cancel --workspace "$WORKSPACE" --run <run_id>`。缺工具显示 `tools_missing`；无可解析模型的失败显示 `awaiting_review` 并保留原生错误与日志。修复二进制后重新 `doctor` 到新目录，再基于原冻结输入建立新轮，不覆盖旧计划。

```sh
python3 -m tools.reconstruction project-add-round --workspace "$WORKSPACE" --project region-loop \
    --toolchain "$WORKSPACE/local/validation/reconstruction-tools/loop-002/toolchain.json"
python3 -m tools.reconstruction project-run --workspace "$WORKSPACE" --project region-loop
```

中断的来源复制与准备尝试保留在本地，后续尝试使用新目录。机器崩溃留下的 `.lock` 目录必须先确认旧进程及子进程均已停止，再手工移除，避免双进程同时改同一作业。代码或正式 schema 发生变化会使旧计划失效；应使用同一代码版本恢复，或用原冻结输入创建新工程，不能修改旧计划哈希伪装兼容。

## 最终精修、Blender 与 WorldRelease

最终任务需要最大模型通过全部数值门槛，且最后导入的真实外部结果明确接受其 `model_digest`，该模型所有注册图像均已审阅，没有关联未解决问题。其他未注册图像可以保持 `unreviewed`。两道门槛都通过才执行最终精修；GPT 的满意判断不能替代实际数值与证据。

先对 `review-002` 重复实际外部审阅，按索引提供该模型需要的全部原图，并将合格结果保存为 `local/review-results/review-002.json`；只有真实完成图像审阅后才能填写 `accepted_models`。随后导入第二轮结果，并在安装好最终工具后探测新的清单。

```sh
python3 -m tools.reconstruction project-import --workspace "$WORKSPACE" --project region-loop \
    --response "$WORKSPACE/local/review-results/review-002.json"
python3 -m tools.reconstruction doctor --workspace "$WORKSPACE" \
    --output "$WORKSPACE/local/validation/reconstruction-tools/final-001"
python3 -m tools.reconstruction project-final --workspace "$WORKSPACE" --project region-loop \
    --round round-0002 --final final-001 \
    --toolchain "$WORKSPACE/local/validation/reconstruction-tools/final-001/toolchain.json"
python3 -m tools.reconstruction project-status --workspace "$WORKSPACE" --project region-loop
```

只新增 OpenMVS 时可使用新的工具清单，但 COLMAP 二进制与能力指纹必须和已审阅粗解一致。缺少最终工具时不生成卡死的最终任务；补齐后可重用同一 `final` ID。已存在任务更换工具必须用新 ID。最终任务从已冻结且复制后再次验证的粗阶段开始，不重跑 mapper；失败重试沿用其检查点和计算账。

从状态的 `finals[].run_id` 读取实际最终 ID，替换下面的 `FINAL_RUN`。产物 `stages/bundle/reconstruction.json` 接入既有 Blender 流程。任意尺度粗解可导入独立母版，但 WorldRelease 导出前必须有实际尺度与世界对齐依据、人工制作的碰撞和出生点。不能从导航地图臆造 SfM 变换。

后续 Blender 和 WorldRelease 命令要求完整源码工作区、已有 Blender 与网页仓库；Windows 交付包仅包含采集和重建工具，不包含完整制作发布链。把真实最终重建包搬到完整工作区后再执行。下面默认 Blender 位于 macOS 标准路径，Linux 或 Windows 须在 `tools.blender import` 和 `tools.world_export build` 命令中分别增加 `--blender <本机可执行文件>`。

```sh
export FINAL_RUN="REPLACE_WITH_ACTUAL_FINAL_RUN_ID_FROM_STATUS"
python3 -m tools.reconstruction verify-bundle \
    --bundle "$WORKSPACE/local/reconstruction-runs/$FINAL_RUN/stages/bundle"
python3 -m tools.blender import \
    --reconstruction "$WORKSPACE/local/reconstruction-runs/$FINAL_RUN/stages/bundle/reconstruction.json" \
    --output "$WORKSPACE/local/masters/region-r1.blend"
python3 -m tools.blender import \
    --reconstruction "$WORKSPACE/local/reconstruction-runs/$FINAL_RUN/stages/bundle/reconstruction.json" \
    --transform "$WORKSPACE/local/alignment.json" --world-frame region-world-v1 --scale-status estimated \
    --output "$WORKSPACE/local/masters/region-aligned.blend"
python3 -m tools.world_export build --master "$WORKSPACE/local/masters/region-r2.blend" \
    --config "$WORKSPACE/local/export/region.json" --releases "$WORKSPACE/local/releases"
python3 -m tools.world_export validate "$WORKSPACE/local/releases/region-r1"
python3 "$WORKSPACE/endfield-world-web/tools/asset-validation/validate_world_release.py" \
    "$WORKSPACE/local/releases/region-r1"
```

`alignment.json` 的 `s/R/t/coordinate_revision/scale_evidence` 必须来自实际尺度与对齐依据。第二次导入建立独立的已对齐母版，不传任意坐标旧母版的 `--master`；只在 Blender 中缩放并另存不会更新来源记录的 `scale_status`。在 `region-aligned.blend` 中完成修补、碰撞与锚点制作后另存为 `region-r2.blend`；`region.json` 的 `accepted_reconstruction_revision` 必须填写人工认可的真实重建修订。详细参数见 [Blender 母版与导出](./blender-export.md)和 [WorldRelease 发布流程](./release-publishing.md)。网页仍只消费 WorldRelease，不读取采集数据库、原始图像或粗解工程。
