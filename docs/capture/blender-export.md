# Blender 母版与导出

本文说明离线制作 CLI、母版修订与坐标约束。更新日期 2026-11-09。

## 环境

使用已有 Blender，当前实际验证版本为 Blender 5.3.0 Release Candidate `f78a5e153839`。外层 Python 安装 `tools/capture/requirements.lock`，Blender 自带 Python 运行制作脚本。以下命令从采集仓库根执行。

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.blender --help
```

CLI 把 Blender 用户配置、扩展、脚本目录放到工作区 `local/cache/blender/`，临时文件与进程日志放到 `local/tmp/blender/`。所有输入、母版和输出必须位于本工作区。后台进程关闭母版自动执行脚本，单次运行最多一小时，超时或中断会结束进程树。

## 导入重建

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.blender import \
  --reconstruction ../local/reconstruction/region/revisions/REV/reconstruction.json \
  --output ../local/masters/region-r1.blend \
  --blender /Applications/Blender.app/Contents/MacOS/Blender
```

正式 `reconstruction.json` 使用 `reconstruction.bundle/1.0` 并按采集仓库 schema 检查。手工准备的 OBJ/PLY/glTF 最小清单使用 `blender.source/1.0`，提供下述消费字段。导入器消费 `revision`、`world_frame_id`、`scale_status`、`mesh_file`、`files`、`source.input_batches`（正式包）或 `source.input_batch_ids`（手工清单） 和 `transform_world_from_sfm`。每个 `files` 条目提供 `uri`、`sha256`、`bytes`，网格与引用纹理必须在索引内。支持 OBJ、PLY、glTF 和 embedded GLB。OBJ 每条 `mtllib` 只能有一个文件，MTL 贴图引用只能是无选项的单个相对路径。PLY 外部 `TextureFile` 引用会被拒绝。

变换为列向量 `X_world = s R X_sfm + t`。`R` 为行排列的正交 3×3 旋转矩阵，`s > 0`，`t` 为米单位。必须显式提供 `coordinate_revision`、`scale_evidence` 和 `world_frame_id`。可用 `--transform ../local/alignment.json` 显式覆盖变换。未对齐 arbitrary 输入允许进入独立 SfM 母版，身份轴变换仅保留其任意坐标，导出会拒绝。对齐时同时显式传入 `--transform`、`--world-frame`、`--scale-status estimated`，在新母版中导入。导航坐标不能填充该变换。

```json
{
    "s": 1,
    "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
    "t": [0, 0, 0],
    "coordinate_revision": "alignment-1",
    "scale_evidence": "制作人员提供的尺度与对齐依据"
}
```

Blender 制作为 Z-up，导入时执行 `(x_b,y_b,z_b)=(x_w,-z_w,y_w)`。glTF 输入先恢复其原始几何坐标，再应用显式 SfM 到世界变换。最终 glTF 导出使用 Blender 内建 Y-up 转换，发布清单的 chunk transform 为单位变换。

## 母版制作

| Collection | 规则 |
| --- | --- |
| `Source_<revision>` | 冻结重建来源，隐藏并禁止选择，重新导入前验证几何与变换指纹 |
| `Working` | 当前重建的制作副本，新增修订只替换带 `generated_source` 标记的对象 |
| `ManualPatches` | 人工修补，跨修订保留，发布时记录几何指纹与坐标修订 |
| `Collision` | 制作人员提供的 Mesh 代理，导出时合并为静态三角网格 |
| `Anchors` | 必需 `Spawn` 脚底出生 Empty，其余对象作为具名锚点 |
| `Export_mobile` | 生成区，导出时按 profile 从母版重新派生 |

在 Blender 中编辑 `Working` 副本，并将需要跨重建保留的修补放进 `ManualPatches`。创建碰撞代理与 `Spawn`，保存为新的本地 `.blend`。导出器使用 evaluated mesh，包含工作副本的修改器结果。碰撞三角形须绕序朝外，出生地面朝上。

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.blender import \
  --reconstruction ../local/reconstruction/region/revisions/NEXT/reconstruction.json \
  --master ../local/masters/region-r1.blend \
  --output ../local/masters/region-r2.blend
```

`--output` 必须是尚不存在的新路径。重复导入相同 revision 验证 manifest hash 后保持 Collection 幂等。新修订保留旧 Source、人工修补、碰撞与锚点。世界 frame、坐标修订或显式变换发生变化时拒绝自动迁移，需在新母版中人工重新对齐这些内容。

母版内 `MasterAssetSet.json` 文本记录来源 manifest hash、文件 hash、坐标修订、Blender 版本和导出器版本。正式发布另记录实际 `.blend` hash、各档导出资产 hash、人工修补指纹和导出配置 hash。

## 发布

配置、固定契约与原子发布操作见[发布流程](./release-publishing.md)。先用自有样例完成坐标往返与浏览器加载，再对实际重建母版进行视角、纹理、孔洞、门洞、台阶和碰撞验收。
