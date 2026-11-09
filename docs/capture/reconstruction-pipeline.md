# 离线重建运行器

本文说明单区域串行重建、检查点恢复和可搬运输出。更新日期 2026-11-09。英文版见 [Offline reconstruction runner](./reconstruction-pipeline.en.md)。

## 环境与工具探测

使用安装了 `tools/capture/requirements.lock` 依赖的 Python 3.9 或更新版本。所有输入、配置、日志、缓存和输出放在工作区 `local/`。以下命令从采集仓库根目录执行，`WORKSPACE` 指向其父工作区。工具分别安装到本地，运行器不会自动安装或修改系统工具。

```sh
export WORKSPACE="$(cd .. && pwd)"
export PYTHONDONTWRITEBYTECODE=1
python3 -m tools.reconstruction doctor \
    --workspace "$WORKSPACE" \
    --output "$WORKSPACE/local/validation/reconstruction-tools/run-001" \
    --colmap "$WORKSPACE/local/tools/colmap/colmap" \
    --openmvs-dir "$WORKSPACE/local/tools/openmvs"
```

`doctor` 为 COLMAP 五个子命令和 OpenMVS 五个工具实际执行 `--help`，记录原文、SHA-256、版本原文和可执行文件哈希。OpenMVS 2.4.0 的帮助命令返回 1，但完整帮助仍可作为能力证据。缺工具、不可执行或缺参数时明确拒绝相关阶段。`ready` 仅说明帮助探测可用。

构建指纹采用二进制 SHA-256 和规范化参数能力集合。帮助中的时间、机器容量、日志路径与安装路径不影响构建键。动态库与操作系统仍属于运行环境，原始帮助保留环境线索。实际执行前再次检查二进制哈希。

已在 macOS arm64 使用官方 COLMAP 4.2.1 CPU 版和 OpenMVS 2.4.0 核对实际参数并执行自有多视角样例。Windows 进程树取消与真实游戏输入仍需对应实机验证。

## 明确相机组与坐标

先按[离线输入准备](./offline-preparation.md)生成只读作业。查看 `local/reconstruction/<job>/calibration-groups.json`，将其中每个 `camera_group_id` 显式映射到输出组。配置遵循 `contracts/reconstruction/config.schema.json`。

```json
{
    "schema_version": "reconstruction.config/1.0",
    "camera_model": "PINHOLE",
    "camera_params": null,
    "camera_group_mapping": {
        "cal-REPLACE_WITH_PREPARED_GROUP_HASH": "camera-01"
    },
    "random_seed": 0,
    "stage_timeout_seconds": 3600,
    "max_image_size": 1600,
    "world_frame_id": null,
    "scale_status": "arbitrary",
    "transform_world_from_sfm": null,
    "parent_revision": null
}
```

映射必须完整。相同目标组共享内参，合并时检查原始尺寸、相机模式及 FOV 一致。每组以独立图像子目录交给 `single_camera_per_folder`，提取后查询 SQLite 验证实际相机分组。首版所有组使用同一种显式 `camera_model`，各组内参可分别估计。`camera_params` 为可选共同先验，填写时要求各组图像几何一致，参数数量必须符合该模型。

预处理为 identity 复制，记录 `A_processed_from_raw=I`。保留 raw，图像解码由真实 COLMAP 特征提取执行。HUD 去除、角色遮罩和跨 MVS 遮罩传播各自保留未验证状态。任何这类污染都进入人工质量审阅。

原始网格保持 SfM 任意坐标。只有显式提供 `s`、正交且行列式为 1 的 `R`、`t`、`coordinate_revision`、`scale_evidence`，同时指定 `world_frame_id` 和 `estimated` 或 `verified`，才生成规范世界相机。变换满足 `Xw=s R Xsfm+t`。相机中心为 `C=-Rcvᵀt`，网页相机旋转为 `R Rcvᵀ diag(1,-1,-1)`，缩放仅作用于位置和几何。矩阵按行保存，公式与 COLMAP 官方格式一致。[COLMAP 格式](https://colmap.github.io/format.html)

## 计划与执行

将配置保存到工作区 `local/`，创建一个新的 run ID。计划绑定准备清单哈希、配置、正式 schema、运行器代码、工具指纹与随机种子。COLMAP 显式设置全局种子及 mapper 种子，OpenMVS 随机行为由固定版本自身控制，输出字节可能变化。

```sh
python3 -m tools.reconstruction plan \
    --workspace "$WORKSPACE" --job area-01 --run area-01-r001 \
    --config "$WORKSPACE/local/reconstruction-config.json" \
    --toolchain "$WORKSPACE/local/validation/reconstruction-tools/run-001/toolchain.json"
python3 -m tools.reconstruction run --workspace "$WORKSPACE" --run area-01-r001
python3 -m tools.reconstruction status --workspace "$WORKSPACE" --run area-01-r001
```

| 阶段 | 产物与检查 |
| --- | --- |
| verify | 复核冻结准备输入和字节哈希 |
| preprocess | 按显式组复制图像，记录 identity 与源帧身份 |
| features | SQLite 完整性、图像名称、非空特征、描述子长度、相机组 |
| match | 独立数据库副本，至少一对几何验证匹配 |
| map | 实际 mapper 与文本转换，解析相机、姿态、点及双向 track 引用 |
| undistort | 独立去畸变图像、pinhole 模型、闭合图像引用 |
| import | MVSI 相机场景，InterfaceCOLMAP 实际回读并检查图像映射 |
| densify | MVSI 相机场景和配套 dense PLY，解析非空点云与相机引用 |
| mesh | 实际三角网格，解析二进制或文本 PLY 和索引范围 |
| refine | 显式 camera scene 加 `mesh-file`，检查 refined PLY |
| texture | 显式 refined mesh，检查 OBJ/UV/MTL 与 PNG/JPEG 纹理结构 |
| quality | 注册率、连通分量、轨迹长度、重投影误差、稠密点数、网格数量 |
| bundle | 封装、schema、路径、哈希、坐标及来源一致性校验 |

mapper 有多个模型时选择注册图像最多的模型，同数时按模型名排序，记录所有模型的图像数量。被舍弃的输入仍影响注册率，质量维持 `needs_review`。首版使用小区域 exhaustive matching。命令与 CPU 参数均以安装版本帮助为准。[COLMAP CLI](https://colmap.github.io/cli.html)

OpenMVS 2.4.0 的 InterfaceCOLMAP 读取 MVSI interface 格式。运行器显式使用 `archive-type=-1`，将相机场景和 PLY 网格分别传递给后续工具，避免混用 Boost-native scene。实际二进制不提供 `export-texture-lossless` 时消费其 JPEG 输出，并检查尺寸与标记结构。PNG 还检查 CRC、解压长度和扫描行结构。[OpenMVS InterfaceCOLMAP](https://github.com/cdcseacave/openMVS/blob/v2.4.0/apps/InterfaceCOLMAP/InterfaceCOLMAP.cpp), [OpenMVS 流程](https://github.com/cdcseacave/openMVS/blob/develop/docs/wiki/Usage.md)

## 恢复与取消

```sh
python3 -m tools.reconstruction cancel --workspace "$WORKSPACE" --run area-01-r001
python3 -m tools.reconstruction resume --workspace "$WORKSPACE" --run area-01-r001
```

每个 run 使用排他目录锁。取消通过本地请求文件传递，活动进程在轮询后终止整个进程组，Windows 使用 `taskkill /T /F`。超时限制应用于每条外部命令。Ctrl-C 同样写入取消状态。队列中的取消请求在执行时生效，显式 `resume` 清除旧请求。

检查点只有在命令成功且对应产物验证完成后写入。恢复仅复用 `succeeded`、阶段键一致且全量文件哈希匹配的前缀。首次失效阶段及所有下游重跑，旧尝试移动到本 run 的 `attempts/`。损坏的检查点重新计算。配置、代码、schema 或工具改变时创建新计划。

状态为 `queued`、`running`、`failed`、`cancelled` 或 `needs_review`。失败保留 `stage`、错误和 `logs/<stage>.log`，后续阶段保持未执行。机器崩溃留下的锁需要确认原进程及子进程已停止后再人工移除。日志和运行缓存留在 `local/reconstruction-runs/<run>/`，不会进入交付包。

## 可搬运产物与人工反馈

最终目录为 `local/reconstruction-runs/<run>/stages/bundle/`，包含 `reconstruction.json`、`cameras.json`、`quality.json`、`provenance.json` 和 `assets/`。复制整个目录即可交给 Blender 制作。清单按文件字节哈希生成内容修订，构建键单独保留，因此相同配置产生不同产物时也获得不同修订。

```sh
python3 -m tools.reconstruction verify-bundle \
    --bundle "$WORKSPACE/local/reconstruction-runs/area-01-r001/stages/bundle"
python3 -m tools.reconstruction feedback \
    --workspace "$WORKSPACE" --run area-01-r001 --request review-001
```

`provenance.json` 带配置、规范化工具指纹和准备清单。`source.input_batches` 使用 `source_id`、`batch_id`、`sha256` 联合追溯，batch ID 只在源会话范围内解释。正式 schema 位于 `contracts/reconstruction/`，全部文件引用需要闭合，清单本身作为外部搬运信任根。

反馈目录保存受来源修订和 bundle 哈希约束的 PreviewSnapshot、相机、稀疏点云、质量报告和 RescanRequest。显式变换存在时点云和相机都位于规范世界。请求携带 session/frame 身份，保持 `needs_mapping` 且 `automatic_navigation=false`，供人工选择补采视角。反馈读取与生成在同一 run 锁内执行。

重投影统计来自 COLMAP 存储的全局 bundle-adjustment 误差。数值阈值尚未通过游戏 M0 实验标定，几何孔洞、保留视角、动态污染、可行走面、游戏可达性和尺度验收各自为 `not_assessed`。自有合成场景跑通证明实际工具链和调度可运行，真实游戏图像能否稳定重建继续依赖用户返还的 Windows 数据与人工验收。
