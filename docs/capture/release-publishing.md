# WorldRelease 发布流程

本文说明由 Blender 母版构建可独立分发的 WorldRelease。更新日期 2026-11-09。

## 固定契约

`contracts.lock` 锁定网页仓库的完整 commit 与四份 schema 的 SHA-256，`vendor/world-contracts/` 仅保存正式 schema。运行时验证本地锁与 hash。更新契约时先在网页仓库完成审查与提交，再执行以下维护命令。

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.world_export vendor \
  --repository ../endfield-world-web \
  --commit 6a1c4e8a6be8959736b00b09ab964c8f323f4d5d
```

网页仓库的三角网格碰撞使用 `world.collision/2.0`。既有盒体 `world.collision/1.0` 由网页消费者继续支持。当前生产者发布统一烘焙到世界坐标的三角网格，chunk transform 固定为单位变换。

## 导出配置

配置与角色 GLB 都放在工作区 `local/`。角色必须是自有或已授权、内嵌资源的 GLB。以下配置中的坐标、尺度说明和预算由制作人员填写。数值只演示格式。

```json
{
    "scene_id": "region",
    "release_id": "region-r1",
    "accepted_reconstruction_revision": "REPLACE_WITH_REVIEWED_REVISION",
    "profiles": {
        "mobile": {
            "lods": [
                {"decimate_ratio": 0.5, "min_distance_m": 0, "hysteresis_m": 0},
                {"decimate_ratio": 0.25, "min_distance_m": 20, "hysteresis_m": 2}
            ]
        },
        "high": {"decimate_ratio": 1}
    },
    "unlit": true,
    "source_kind": "reconstruction",
    "description": "已人工检查的区域母版",
    "character_glb": "character.glb",
    "scale_reference": {
        "method": "manual-estimate",
        "description": "制作人员填写尺度依据"
    },
    "roam_bounds_world": {"min": [-5, -1, -5], "max": [5, 5, 5]},
    "camera": {
        "mode": "third_person",
        "target_height_m": 1.1,
        "distance_m": 4,
        "yaw_degrees": 0,
        "pitch_degrees": -15,
        "vertical_fov_degrees": 55,
        "near_m": 0.1,
        "far_m": 100
    }
}
```

`mobile` 必需，`balanced` 和 `high` 可选。每档直接从同一母版的 evaluated mesh 派生，按 `decimate_ratio` 应用 Decimate。每档支持显式 `lods`，距离严格递增，LOD0 的距离与滞回都为零。每个 LOD 分别直接派生自母版，保留 UV 与材质，输出 embedded GLB。单 LOD 也可直接使用 `decimate_ratio`。`unlit=true` 从 Principled Base Color 链接生成 unlit 材质，用 `KHR_materials_unlit` 保存截图外观。需要人工复核透明材质、复杂自定义 shader、色彩与参考视角。未提供的物理材质参数不会从截图推断。纹理沿用原图，不执行压缩或设备预算自动判定。

## 构建与校验

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.world_export build \
  --master ../local/masters/region-r2.blend \
  --config ../local/export/region.json \
  --releases ../local/releases

PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.world_export validate \
  ../local/releases/region-r1
```

`accepted_reconstruction_revision` 必须等于已经人工验收的当前重建修订。正式包的来源批次按 `(source_id,batch_id)` 的规范 hash 生成独立 ID，并保留完整原始三元组，避免不同会话同名批次混淆。

构建读取母版、在同级唯一 staging 目录生成资产、运行生产者独立的 schema 与语义校验，最后把目录原子重命名为 `release_id`。相同 release ID 和已有母版输出都拒绝覆盖。同一 release ID 有独占锁，失败保留 staging 与日志用于诊断，旧 release 可继续使用。意外断电留下的锁需确认原进程已结束后手工清理。

输出包含 `world.json`、`asset-index.json`、各档 GLB、角色 GLB、`collision.json` 和 `master-source.json`。world hash 在成功后打印。world 绑定资产索引 hash，索引绑定资产 hash 与字节数，`build.master_source` 绑定母版来源记录，来源再关联重建 manifest、输入批次与原始文件 hash。发布包不携带母版或原始图片。

校验覆盖固定 schema、严格 JSON、安全相对路径、重复 ID、完整引用、GLB 内嵌资源、扩展声明、文件字节与 hash、LOD 顺序、世界坐标、三角形索引、float32 后非退化几何、出生点地面支撑和来源一致性。脚底支撑要求 0.15 米内存在朝上且坡度不超过 45 度的三角形。消费者仍需执行胶囊重叠、台阶、门洞与真实设备交互验收。

## 两仓集成验收

网页仓库可用自己的只读校验器独立检查相同发布包。

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python \
  ../endfield-world-web/tools/asset-validation/validate_world_release.py \
  ../local/releases/region-r1
```

通过后将整个 release 目录放到静态站点资产根，并使用网页的受限 release 路径加载。在浏览器确认角色、出生点、碰撞、所有档位和锚点坐标。自有样例只能证明工具链的局部行为，实际游戏资产仍需完成纹理、重建质量、尺度与设备体验验收。
