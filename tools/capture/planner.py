"""Freeze a real navmesh coverage plan for automatic capture."""

import math
import uuid

from .probe import utc_now
from .regions import CATALOG_FILES, SOURCE_ROOT, region_catalog, resolve_region, resource_hashes
from .validate import require


def number(value, name, lower, upper):
    require(type(value) in (int, float) and math.isfinite(value) and lower <= value <= upper, f"{name} 超出允许范围")
    return value


def selection_parameters(request):
    require(isinstance(request, dict), "选区请求必须为 JSON 对象")
    radius = number(request.get("radius", 60), "半径", 10, 600)
    spacing = number(request.get("spacing", 20), "采样间距", 5, 200)
    views = request.get("views", 8)
    require(type(views) is int and 1 <= views <= 12, "每点视角数必须为 1 至 12 的整数")
    max_points = request.get("max_points", 160)
    require(type(max_points) is int and 1 <= max_points <= 512, "采样点预算必须为 1 至 512 的整数")
    tier = request.get("tier")
    require(
        tier is None
        or (
            isinstance(tier, str) and 0 < len(tier) <= 120 and tier == tier.strip() and all(ord(c) >= 32 for c in tier)
        ),
        "层级名称必须为 1 至 120 个可显示字符",
    )
    floor_y = request.get("floor_y")
    if floor_y is not None:
        number(floor_y, "层高", -100000, 100000)
    return {
        "radius": radius,
        "spacing": spacing,
        "views": views,
        "max_points": max_points,
        "tier": tier,
        "floor_y": floor_y,
    }


def validate_polygon(polygon, size=None):
    require(isinstance(polygon, list) and 3 <= len(polygon) <= 64, "选区需要 3 至 64 个顶点")
    for point in polygon:
        require(isinstance(point, list) and len(point) == 2, "选区顶点需要两个底图坐标")
        number(point[0], "选区 X", 0, size[0] if size else 100000)
        number(point[1], "选区 Y", 0, size[1] if size else 100000)
    require(len({tuple(point) for point in polygon}) >= 3, "选区至少需要三个不同顶点")
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(polygon, [*polygon[1:], polygon[0]], strict=True))
    require(abs(area) > 0.000001, "选区面积为零或过小，请重新圈选")
    return polygon


def build_plan(workspace, request):
    from .navigation_worker import query_navigation, verify_resource_mirror

    parameters = selection_parameters(request)
    catalog = region_catalog()
    region = resolve_region(request.get("region"), catalog)
    frame = next(item for item in catalog["maps"] if item["id"] == region["map"])
    radius, spacing, views, max_points = (parameters[key] for key in ("radius", "spacing", "views", "max_points"))
    center = region["anchor_position"]
    polygon = request.get("polygon")
    if polygon is None:
        polygon = [
            [
                max(0, min(frame["size"][0] - 0.001, center[0] + radius * math.cos(index * math.tau / 48))),
                max(0, min(frame["size"][1] - 0.001, center[1] + radius * math.sin(index * math.tau / 48))),
            ]
            for index in range(48)
        ]
    validate_polygon(polygon, frame["size"])
    floor_y = parameters["floor_y"]
    tier = parameters["tier"]
    if tier:
        require(isinstance(tier, str), "层级名称必须为字符串")
        zones = query_navigation(workspace, "zones")
        require(zones.get("ok"), zones.get("error", "层级查询失败"))
        match = next((item for item in zones["zones"] if item["name"] == tier), None)
        require(match is not None and match["geometry_zone_id"] == region["navmesh_zone_id"], "层级与区域底图不一致")
        if floor_y is None:
            floor_y = match["floor_y"]
        require(floor_y is not None, "该层级未提供高度，请指定层高")
    params = {
        "zone_id": region["navmesh_zone_id"],
        "start": center,
        "polygon": polygon,
        "spacing": spacing,
        "max_points": max_points,
    }
    if floor_y is not None:
        params["floor_y"] = number(floor_y, "层高", -100000, 100000)
    uris = [*CATALOG_FILES, frame["image_uri"]]
    for directory in (SOURCE_ROOT / "assets/resource/model/map", SOURCE_ROOT / "assets/resource/pipeline/SceneManager"):
        uris.extend(path.relative_to(SOURCE_ROOT).as_posix() for path in directory.rglob("*") if path.is_file())
    nogo = SOURCE_ROOT / "assets/data/MapNavigator/nogo_zones.json"
    if nogo.is_file():
        uris.append(nogo.relative_to(SOURCE_ROOT).as_posix())
    resources = resource_hashes(uris)
    runtime_resources = verify_resource_mirror(workspace, resources)
    result = query_navigation(workspace, "coverage", **params)
    require(result.get("ok"), result.get("error", "可达采样点规划失败"))
    zone = result.get("zone", {})
    require(
        zone.get("name") == region["zone_id"]
        and zone.get("zone_id") == region["navmesh_zone_id"]
        and zone.get("geometry_zone_id") == region["navmesh_zone_id"],
        "规划器返回的底图坐标系与区域目录不一致",
    )
    points = result.get("points")
    require(isinstance(points, list) and 0 < len(points) <= max_points, "选区中没有可达采样点")
    plan_id = "auto-plan-" + uuid.uuid4().hex
    waypoints = []
    for index, point in enumerate(points):
        target = point.get("target")
        require(isinstance(target, list) and len(target) == 2, "规划器返回无效坐标")
        number(target[0], "目标 X", 0, frame["size"][0])
        number(target[1], "目标 Y", 0, frame["size"][1])
        deck = number(point.get("target_deck_y"), "目标层高", -100000, 100000)
        for view in range(views):
            waypoints.append(
                {
                    "waypoint_id": f"point-{len(waypoints) + 1:05d}",
                    "segment_id": "segment-" + plan_id,
                    "position_xy": target,
                    "target_deck_y": deck,
                    "yaw_degrees": view * 360 / views,
                    "pitch_degrees": None,
                    "sample_index": index,
                    "view_index": view,
                }
            )
    require(resource_hashes(uris) == resources, "规划期间资源发生变化，请重新规划")
    require(verify_resource_mirror(workspace, resources) == runtime_resources, "规划期间运行资源发生变化，请重新规划")
    plan = {
        "schema_version": "capture.auto-plan/1.0",
        "plan_id": plan_id,
        "created_utc": utc_now(),
        "region": region,
        "map_coordinates": {
            "coordinate_frame_id": region["zone_id"],
            "unit": "map_pixel",
            "axes": "x_right_y_down",
            "heading_unit": "degree",
            "heading_convention": "clockwise_from_map_up",
            "scale_status": "arbitrary",
        },
        "selection": {
            "polygon": polygon,
            "radius": radius,
            "spacing": spacing,
            "views": views,
            "tier": tier,
            "floor_y": floor_y,
            "max_points": max_points,
            "scope": "anchor_surroundings" if request.get("polygon") is None else "drawn_polygon",
        },
        "waypoints": waypoints,
        "resources": resources,
        "runtime_resources": runtime_resources,
        "coverage": {
            "point_count": len(points),
            "rejected": result.get("rejected", 0),
            "route": result.get("route", []),
        },
    }
    from .rescan import candidate_catalog

    for candidate in candidate_catalog(plan)["candidates"]:
        for point in waypoints:
            if point["waypoint_id"] in candidate["source_waypoint_ids"]:
                point["candidate_id"] = candidate["candidate_id"]
    return plan
