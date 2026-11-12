"""Resolve untrusted review suggestions through frozen reachable candidates."""

import copy
import hashlib
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator

from . import task
from .probe import utc_now
from .validate import load_json, read_frames, read_verified, require, safe_path, validate_bundle

SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_KEYS = ("bundle_sha256", "auto_plan_sha256", "catalog_sha256", "resources_sha256")
MAX_ACTIONS = 128


def digest(record):
    return hashlib.sha256(task.encoded(record)).hexdigest()


def candidate_catalog(plan):
    """Only the native planner's frozen targets may become executable candidates."""
    resources_sha256 = digest(sorted(plan["resources"], key=lambda entry: entry["uri"]))
    candidates = {}
    for point in plan["waypoints"]:
        identity = {
            "region_id": str(plan["region"]["id"]),
            "coordinate_frame_id": plan["map_coordinates"]["coordinate_frame_id"],
            "resources_sha256": resources_sha256,
            "position_xy": point["position_xy"],
            "target_deck_y": point["target_deck_y"],
        }
        candidate_id = "candidate-" + digest(identity)[:48]
        item = candidates.setdefault(
            candidate_id,
            {
                "candidate_id": candidate_id,
                "position_xy": point["position_xy"],
                "target_deck_y": point["target_deck_y"],
                "source_waypoint_ids": [],
            },
        )
        item["source_waypoint_ids"].append(point["waypoint_id"])
    return {
        "schema_version": "capture.rescan-candidates/1.0",
        "auto_plan_id": plan["plan_id"],
        "region_id": "region-" + str(plan["region"]["id"]),
        "resources_sha256": resources_sha256,
        "candidates": sorted(candidates.values(), key=lambda item: item["candidate_id"]),
    }


def source_context(bundle_path):
    root = task.check_path(bundle_path)
    result = validate_bundle(root)
    manifest = result["manifest"]
    bundle_bytes = (root / "bundle.json").read_bytes()
    require(load_json(bundle_bytes, "bundle.json") == manifest, "冻结采集包在读取时发生改变")
    hashes = {entry["uri"]: entry["sha256"] for entry in manifest["files"]}
    plan = load_json(read_verified(root / "plan.json", hashes["plan.json"], True), "plan.json")
    context = {
        "bundle_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        "auto_plan_sha256": None,
        "catalog_sha256": None,
        "resources_sha256": None,
        "region_id": plan["region_id"],
        "session_id": manifest["session_id"],
        "candidates": [],
        "automatic_rescan": False,
    }
    if manifest.get("auto_plan_uri") is None:
        return context
    catalog = load_json(
        read_verified(root / manifest["rescan_candidates_uri"], hashes[manifest["rescan_candidates_uri"]], True),
        "rescan-candidates.json",
    )
    context.update(
        auto_plan_sha256=hashes[manifest["auto_plan_uri"]],
        catalog_sha256=hashes[manifest["rescan_candidates_uri"]],
        resources_sha256=catalog["resources_sha256"],
        candidates=catalog["candidates"],
        automatic_rescan=True,
    )
    return context


def schema_check(request):
    version = request.get("schema_version") if isinstance(request, dict) else None
    require(version != "reconstruction.rescan/1.0", "1.0 是人工补拍协议，不可自动执行")
    path = SOURCE_ROOT / "contracts/reconstruction/rescan-v2.schema.json"
    schema = load_json(path.read_bytes(), path.name)
    errors = list(Draft202012Validator(schema).iter_errors(request))
    require(not errors, "补采请求 schema 无效: " + (errors[0].message if errors else ""))


def workspace_path(workspace, value):
    path = task.check_path(value)
    require(path.is_relative_to(workspace), "补采来源必须位于工作区内")
    return path


def context_for(workspace, request, review_path, bundle_path):
    from tools.reconstruction.project import project_status, read_project, read_rounds, verify_review

    schema_check(request)
    workspace = task.check_path(workspace)
    review_path = workspace_path(workspace, review_path)
    bundle_path = workspace_path(workspace, bundle_path)
    review = verify_review(workspace, request["project_id"], review_path)
    require(review.get("schema_version") == "reconstruction.review/1.0", "不支持的审阅包")
    for key in ("project_id", "round_id", "reconstruction_revision"):
        require(review.get(key) == request[key], f"补采请求的 {key} 与审阅轮次不一致")
    source = source_context(bundle_path)
    require(source["automatic_rescan"], "旧采集包未冻结完整自动计划，不能自动补采")
    require(request["source"] == {key: source[key] for key in SOURCE_KEYS}, "补采请求的采集来源哈希不一致")
    require(request["region_id"] == source["region_id"], "补采区域与采集包不一致")
    require(
        any(all(entry.get(key) == source[key] for key in SOURCE_KEYS) for entry in review.get("sources", [])),
        "采集包不属于此审阅轮次",
    )
    _, project_root, project = read_project(workspace, request["project_id"])
    require(
        review_path.name == "review.json" and review_path.parent.parent == project_root / "reviews",
        "审阅包不属于绑定的工程目录",
    )
    rounds = read_rounds(project_root)
    require(rounds and rounds[-1]["round_id"] == request["round_id"], "审阅包已过期，请使用当前轮次")
    state = project_status(workspace, request["project_id"])
    require(not state["stop_reason"], "工程已停止补采: " + str(state["stop_reason"]))
    current_sources = {item["sha256"] for item in rounds[-1]["sources"]}
    require(
        {entry.get("bundle_sha256") for entry in review["sources"]} == current_sources,
        "审阅包来源与当前轮次不一致",
    )
    require(source["bundle_sha256"] in current_sources, "补采来源不属于当前轮次")
    frames = {}
    for entry in review["sources"]:
        root = project_root / "inputs" / entry["bundle_sha256"]
        actual = source if root == bundle_path else source_context(root)
        require(all(entry.get(key) == actual[key] for key in SOURCE_KEYS), "审阅包采集来源已改变")
        bundle_bytes = (root / "bundle.json").read_bytes()
        require(hashlib.sha256(bundle_bytes).hexdigest() == entry["bundle_sha256"], "采集包在读取时发生改变")
        manifest = load_json(bundle_bytes, "bundle.json")
        hashes = {item["uri"]: item["sha256"] for item in manifest["files"]}
        data = read_verified(safe_path(root, "frames.jsonl"), hashes["frames.jsonl"], True)
        for frame in read_frames(data, task.make_validators()).values():
            key = (frame["session_id"], frame["frame_id"])
            require(key not in frames or frames[key]["frame"] == frame, "来源包含冲突的会话与帧")
            frames[key] = {"frame": frame, "root": root}
    return {
        "workspace": workspace,
        "review_path": review_path,
        "bundle_path": bundle_path,
        "source": source,
        "frames": frames,
        "auto_plan": load_json(
            read_verified(safe_path(bundle_path, "auto-plan.json"), source["auto_plan_sha256"], True),
            "auto-plan.json",
        ),
        "project": project,
    }


def existing_actions(workspace, request, ignore_task=None):
    result = set()
    request_ids = set()
    parent = workspace / "local/capture"
    for path in sorted(parent.glob("*/task.json")):
        if path.parent.name == ignore_task:
            continue
        record = task.read_json(path).get("auto_plan", {}).get("rescan_request")
        if not record or record.get("project_id") != request["project_id"]:
            continue
        request_ids.add(record["request_id"])
        result.update((item["target_candidate_id"], item["heading_degrees"]) for item in record["actions"])
    return request_ids, result


def inspect_request(workspace, request, review_path, bundle_path, ignore_task=None):
    context = context_for(workspace, request, review_path, bundle_path)
    candidates = {item["candidate_id"]: item for item in context["source"]["candidates"]}
    issues = {}
    errors = []
    for issue in request["issues"]:
        require(issue["issue_id"] not in issues, "问题 ID 重复")
        issues[issue["issue_id"]] = issue
        for reference in issue["evidence"]:
            frame = context["frames"].get((reference["session_id"], reference["frame_id"]))
            require(frame is not None, "问题引用未知的帧或会话")
    request_ids, seen = existing_actions(context["workspace"], request, ignore_task)
    if request["request_id"] in request_ids:
        errors.append("该补采请求已生成任务，请查看或恢复已有任务")
    if len(request["actions"]) > request["budget"]["max_actions"]:
        errors.append("补采动作超出请求预算")
    if len(seen) + len(request["actions"]) > context["project"]["policy"]["max_rescan_actions"]:
        errors.append("补采动作超出工程累计预算")
    action_ids = set()
    actions = []
    for action in request["actions"]:
        status = "ready"
        reason = None
        key = (action["target_candidate_id"], action["heading_degrees"])
        if action["action_id"] in action_ids or key in seen:
            status, reason = "duplicate", "动作 ID 或目标与朝向重复"
        elif action["issue_id"] not in issues:
            status, reason = "invalid", "动作引用未知问题"
        elif action["target_candidate_id"] not in candidates:
            status, reason = "unmapped", "目标不属于冻结的可达候选目录"
        action_ids.add(action["action_id"])
        seen.add(key)
        if reason:
            errors.append(f"{action['action_id']}: {reason}")
        target = candidates.get(action["target_candidate_id"])
        actions.append(
            {
                **action,
                "status": status,
                "error": reason,
                "position_xy": target["position_xy"] if target else None,
                "target_deck_y": target["target_deck_y"] if target else None,
            }
        )
    displayed_issues = []
    for issue in request["issues"]:
        images = []
        for reference in issue["evidence"]:
            record = context["frames"][(reference["session_id"], reference["frame_id"])]
            image = record["frame"]["image"]
            require(image["status"] == "captured", "问题证据必须有已采集图像")
            images.append(
                {**reference, "image_uri": str(safe_path(record["root"], image["uri"])), "sha256": image["sha256"]}
            )
        displayed_issues.append({**issue, "evidence_images": images})
    return context, {
        "schema_version": "capture.rescan-preview/1.0",
        "request_id": request["request_id"],
        "executable": not errors,
        "issues": displayed_issues,
        "actions": actions,
        "errors": errors,
        "source": context["source"],
    }


def preview_rescan(workspace, request, review_path, bundle_path):
    """Read-only import: no tasks, runtime queries or game input are created here."""
    try:
        _, report = inspect_request(workspace, request, review_path, bundle_path)
        return report
    except (ValueError, OSError, KeyError, TypeError) as error:
        return {
            "schema_version": "capture.rescan-preview/1.0",
            "request_id": request.get("request_id") if isinstance(request, dict) else None,
            "executable": False,
            "issues": [],
            "actions": [],
            "errors": [str(error)],
            "source": None,
        }


def validate_rescan(workspace, request, review_path, bundle_path, ignore_task=None):
    context, report = inspect_request(workspace, request, review_path, bundle_path, ignore_task)
    require(report["executable"], "; ".join(report["errors"]))
    return context


def resolve_rescan(workspace, request, review_path, bundle_path, ignore_task=None):
    """Resolve candidate IDs locally, then ask the existing navmesh engine again."""
    from .auto_task import verify_plan
    from .navigation_worker import query_navigation

    context = validate_rescan(workspace, request, review_path, bundle_path, ignore_task)
    plan = copy.deepcopy(verify_plan(context["auto_plan"], context["workspace"]))
    candidates = {item["candidate_id"]: item for item in context["source"]["candidates"]}
    plan_id = "auto-plan-" + uuid.uuid4().hex
    waypoints = []
    for index, action in enumerate(request["actions"]):
        candidate = candidates[action["target_candidate_id"]]
        waypoints.append(
            {
                "waypoint_id": f"point-{index + 1:05d}",
                "segment_id": "segment-" + plan_id,
                "position_xy": candidate["position_xy"],
                "target_deck_y": candidate["target_deck_y"],
                "candidate_id": candidate["candidate_id"],
                "yaw_degrees": action["heading_degrees"],
                "pitch_degrees": None,
                "sample_index": index,
                "view_index": 0,
                "rescan_action_id": action["action_id"],
                "rescan_issue_id": action["issue_id"],
            }
        )
    parameters = {
        "position": plan["region"]["anchor_position"],
        "position_zone": plan["region"]["zone_id"],
        "custom_action_param": {
            "map_name": plan["region"]["zone_id"],
            "heading_source": "character",
            "zip": False,
            "enable_sprint": False,
            "path": [
                {
                    "action": "NAVMESH",
                    "target": item["position_xy"],
                    "target_deck_y": item["target_deck_y"],
                    "required": True,
                }
                for item in waypoints
            ],
        },
    }
    if plan["selection"]["floor_y"] is not None:
        parameters["floor_y"] = [plan["selection"]["floor_y"]]
    route = query_navigation(context["workspace"], "route_preview", **parameters)
    require(route.get("ok"), route.get("error", "补采路径当前不可达"))
    plan.update(
        plan_id=plan_id,
        created_utc=utc_now(),
        waypoints=waypoints,
        rescan_request=copy.deepcopy(request),
        rescan_source={
            "review_uri": context["review_path"].relative_to(context["workspace"]).as_posix(),
            "bundle_uri": context["bundle_path"].relative_to(context["workspace"]).as_posix(),
        },
    )
    plan["coverage"] = {"point_count": len({item["candidate_id"] for item in waypoints}), "rejected": 0, "route": []}
    verify_plan(plan, context["workspace"])
    return plan
