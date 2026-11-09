"""Read the repository's real named teleport anchors without changing map data."""

import hashlib
from pathlib import Path

from tools.MapNavigator.model import BASE_NAV_ZONE_IMAGE_PARTS

from .task import check_path
from .validate import load_json, require

SOURCE_ROOT = Path(__file__).resolve().parents[2]
CATALOG_FILES = (
    "tools/zmdmap/data/teleport_anchors.json",
    "tools/pipeline-generate/SceneTeleport/anchors.json",
    "assets/resource/pipeline/Interface/SceneTeleportAnchor.json",
)


def resource_hashes(uris):
    result = []
    for uri in sorted(set(uris)):
        path = check_path(SOURCE_ROOT / uri)
        require(path.is_file() and path.is_relative_to(SOURCE_ROOT), f"资源文件不存在或路径非法 {uri}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result.append({"uri": uri, "sha256": digest.hexdigest()})
    return result


def region_catalog():
    data, config, entries = [load_json(check_path(SOURCE_ROOT / uri).read_bytes(), uri) for uri in CATALOG_FILES]
    mappings = {}
    for level_id, level in config["levels"].items():
        prefix = f"SceneEnterWorld{level['region']}{level['sub_region']}"
        for name, anchor_id in {**level["anchors"], **level.get("manual", {})}.items():
            entry = prefix + name
            if entry in entries:
                require(str(anchor_id) not in mappings, "重复传送点映射")
                mappings[str(anchor_id)] = (level_id, entry)
    maps = []
    for map_id, frame in data["maps"].items():
        image_parts = BASE_NAV_ZONE_IMAGE_PARTS.get(frame["zone"])
        if image_parts:
            name = next((item["name"] for item in config["regions"].values() if item["map"] == map_id), map_id)
            maps.append(
                {"id": map_id, "name": name, **frame, "image_uri": "assets/resource/image/" + "/".join(image_parts)}
            )
    by_map = {item["id"]: item for item in maps}
    regions = []
    for anchor in data["anchors"]:
        mapping = mappings.get(anchor["id"])
        if mapping is None or anchor["map"] not in by_map:
            continue
        require(mapping[0] == anchor["level"], "传送入口与锚点关卡不一致")
        names = anchor["area_names"]
        name = names.get("zh_cn") or names.get("en_us") or anchor["id"]
        regions.append(
            {
                "id": anchor["id"],
                "name": name,
                "names": names,
                "level_name": data["levels"][anchor["level"]]["names"].get("zh_cn", anchor["level"]),
                "map": anchor["map"],
                "level": anchor["level"],
                "zone_id": by_map[anchor["map"]]["zone"],
                "navmesh_zone_id": by_map[anchor["map"]]["zone_id"],
                "anchor_position": [anchor["u"], anchor["v"]],
                "teleport_id": anchor["teleport_id"],
                "teleport_entry": mapping[1],
            }
        )
    require(bool(regions), "未找到同时具有真实坐标和传送入口的区域")
    return {"version": data["version"], "regions": regions, "maps": maps, "resources": resource_hashes(CATALOG_FILES)}


def resolve_region(value, catalog=None):
    require(isinstance(value, str) and bool(value.strip()), "请输入区域名称或选中地图上的区域")
    needle = value.strip().casefold()
    records = (catalog or region_catalog())["regions"]
    matches = [
        item
        for item in records
        if needle in {item["id"].casefold(), *(name.casefold() for name in item["names"].values())}
    ]
    require(bool(matches), f"未匹配区域 {value!r}，请选择搜索结果中的完整地名")
    require(len(matches) == 1, "该地名对应多个传送点，请在地图或列表中选择具体区域")
    return dict(matches[0])
