"""Append-only reconstruction rounds and offline, externally reviewed capture feedback."""

import shutil
import uuid
from pathlib import Path

from tools.capture.validate import load_json, read_jsonl, require, safe_path, validate_bundle

from .model import camera_records, read_model
from .pipeline import (
    Runner,
    exclusive,
    images_for,
    inventory,
    plan,
    read_plan,
    run,
    run_root,
    schema_check,
    status,
    usage_seconds,
)
from .prepare import check_path, check_tree, digest, encoded, identifier, output_path, prepare, within, write_bytes
from .toolchain import file_hash, fingerprint, load_toolchain

DEFAULT_POLICY = {
    "max_rounds": 8,
    "max_images": 4000,
    "max_compute_seconds": 28800,
    "max_stagnant_rounds": 2,
    "max_rescan_actions": 128,
    "min_relative_improvement": 0.02,
    "thresholds": {
        "min_registered_images": 6,
        "min_registration_ratio": 0.6,
        "min_sparse_points": 500,
        "min_mean_track_length": 2.5,
        "max_reprojection_error_p95_px": 3.0,
        "max_connected_components": 1,
    },
}
ISSUE_KINDS = (
    "source_image_quality",
    "insufficient_observation",
    "difficult_material",
    "localization_or_tool_failure",
    "insufficient_evidence",
)


def project_root(workspace, project_id):
    workspace, _ = output_path(workspace, project_id)
    return workspace, within(workspace, workspace / "local/reconstruction-projects" / project_id)


def read_project(workspace, project_id):
    workspace, root = project_root(workspace, project_id)
    record = load_json(check_path(root / "project.json").read_bytes(), "project")
    schema_check("project", record)
    require(record["project_id"] == project_id, "project ID mismatch")
    require(
        record["revision"] == identifier({key: value for key, value in record.items() if key != "revision"}),
        "project metadata changed",
    )
    return workspace, root, record


def create_project(workspace, project_id, config_path, toolchain_path, policy_path=None):
    workspace, root = project_root(workspace, project_id)
    config = load_json(within(workspace, Path(config_path)).read_bytes(), "project config")
    schema_check("config", config)
    toolchain = load_toolchain(within(workspace, Path(toolchain_path)))
    policy = (
        load_json(within(workspace, Path(policy_path)).read_bytes(), "project policy")
        if policy_path
        else DEFAULT_POLICY
    )
    record = {
        "schema_version": "reconstruction.project/1.0",
        "project_id": project_id,
        "config": config,
        "toolchain": toolchain,
        "policy": policy,
        "threshold_status": "project_policy_not_calibrated",
        "verification": "not_run",
    }
    record["revision"] = identifier(record)
    schema_check("project", record)
    root.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(root):
        require(not root.exists(), "project already exists")
        staging = root.with_name("." + project_id + "-" + uuid.uuid4().hex + ".partial")
        staging.mkdir()
        write_bytes(staging / "project.json", encoded(record))
        for name in ("rounds", "inputs", "reviews", "review-index", "imports", "import-index", "finals"):
            (staging / name).mkdir()
        staging.rename(root)
    return project_status(workspace, project_id)


def read_rounds(root):
    rounds = []
    for path in sorted((root / "rounds").iterdir()):
        if path.name.startswith("."):
            continue
        check_path(path)
        item = load_json((path / "round.json").read_bytes(), "round")
        require(
            item["round_id"] == path.name
            and item["revision"] == identifier({key: value for key, value in item.items() if key != "revision"}),
            "round metadata changed",
        )
        rounds.append(item)
    return rounds


def round_item(root, round_id):
    require(isinstance(round_id, str) and round_id in {item["round_id"] for item in read_rounds(root)}, "unknown round")
    return next(item for item in read_rounds(root) if item["round_id"] == round_id)


def evaluate_quality(report, policy):
    """Only the largest connected reconstruction may pass the local numeric gate."""
    components = sorted(report["components"], key=lambda item: (-item["metrics"]["registered_images"], item["model"]))
    require(bool(components), "no coarse model evidence")
    component = components[0]
    metrics, limits = component["metrics"], policy["thresholds"]
    checks = {
        "registered_images": metrics["registered_images"] >= limits["min_registered_images"],
        "registration_ratio": metrics["registration_ratio"] >= limits["min_registration_ratio"],
        "sparse_points": metrics["sparse_points"] >= limits["min_sparse_points"],
        "mean_track_length": metrics["mean_track_length"] >= limits["min_mean_track_length"],
        "reprojection_error_p95_px": metrics["reprojection_error_p95_px"] <= limits["max_reprojection_error_p95_px"],
        "connected_components": len(metrics["connected_components"]) <= limits["max_connected_components"],
    }
    return {
        "status": "quality_passed" if all(checks.values()) else "awaiting_review",
        "model": component["model"],
        "model_digest": component["model_digest"],
        "metrics": metrics,
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "threshold_status": "project_policy_not_calibrated",
        "geometry_acceptance": "not_assessed",
        "score": metrics["registered_images"]
        * metrics["registration_ratio"]
        / (1 + metrics["reprojection_error_p95_px"]),
        "scope": "largest_reconstructed_component_only",
    }


def round_status(workspace, root, item, project):
    _, run_directory = run_root(workspace, item["run_id"])
    result = {
        "round_id": item["round_id"],
        "run_id": item["run_id"],
        "input_images": item["image_count"],
        "bundle_paths": [
            {"source_id": source["sha256"], "path": str(root / "inputs" / source["sha256"])}
            for source in item["sources"]
        ],
    }
    state = status(workspace, item["run_id"])
    run_plan = load_json(check_path(run_directory / "plan.json").read_bytes(), "round plan")
    result.update(
        status=state["status"],
        stage=state.get("stage"),
        compute_seconds=usage_seconds(run_directory),
        stop_reason=state.get("error"),
    )
    if state["status"] == "awaiting_review":
        read_plan(workspace, item["run_id"])
        quality_directory = run_directory / "stages/coarse_quality"
        saved = load_json((run_directory / "checkpoints/coarse_quality.json").read_bytes(), "coarse checkpoint")
        require(saved["files"] == inventory(quality_directory), "coarse quality evidence changed")
        report = load_json((quality_directory / "quality.json").read_bytes(), "coarse quality")
        result["quality"] = evaluate_quality(report, project["policy"])
        result["status"] = result["quality"]["status"]
    elif state["status"] in ("failed", "cancelled"):
        result["status"] = "awaiting_review"
        result["run_status"] = state["status"]
        result["quality"] = None
    elif state["status"] == "queued" and run_plan["blockers"]:
        result.update(
            status="tools_missing",
            run_status="blocked",
            quality=None,
            stop_reason="missing toolchain capabilities: " + ", ".join(run_plan["blockers"]),
        )
    reviews = []
    for path in sorted((root / "reviews").iterdir()):
        if path.name.startswith("."):
            continue
        review = load_json(check_path(path / "review.json").read_bytes(), "review package")
        if review["round_id"] == item["round_id"]:
            reviews.append(
                {"review_id": review["review_id"], "path": str(path), "review_file": str(path / "review.json")}
            )
    result["reviews"] = reviews
    result["review"] = reviews[-1] if reviews else None
    imports = []
    for sequence, receipt_path in enumerate(sorted((root / "import-index").glob("*.json")), start=1):
        receipt = load_json(check_path(receipt_path).read_bytes(), "import receipt")
        require(receipt["sequence"] == sequence and receipt_path.stem == f"{sequence:08d}", "invalid import sequence")
        path = safe_path(root / "imports", receipt["response_sha256"] + ".json")
        response = load_json(check_path(path).read_bytes(), "imported review")
        require(file_hash(path) == path.stem, "imported review changed")
        if response["round_id"] == item["round_id"]:
            imports.append(
                {
                    "path": str(path),
                    "review_id": response["review_id"],
                    "coverage": response["coverage"],
                    "issues": response["issues"],
                    "accepted_models": response["accepted_models"],
                }
            )
    result["imports"] = imports
    result["external_review"] = imports[-1] if imports else None
    result["remaining_issues"] = (
        [issue for issue in imports[-1]["issues"] if issue["status"] == "unresolved"] if imports else []
    )
    return result


def project_status(workspace, project_id):
    workspace, root, project = read_project(workspace, project_id)
    rounds = [round_status(workspace, root, item, project) for item in read_rounds(root)]
    finals = []
    for path in sorted((root / "finals").iterdir()):
        if path.name.startswith("."):
            continue
        item = load_json(check_path(path / "final.json").read_bytes(), "final job")
        _, directory = run_root(workspace, item["run_id"])
        finals.append({**item, "state": status(workspace, item["run_id"]), "compute_seconds": usage_seconds(directory)})
    total = sum(item["compute_seconds"] for item in rounds + finals)
    policy, reason = project["policy"], None
    stagnant, previous = 0, None
    for item in rounds:
        if item["status"] in ("queued", "running"):
            continue
        quality = item.get("quality")
        if quality is None:
            continue
        score = quality["score"]
        if previous is not None:
            stagnant = stagnant + 1 if score <= previous * (1 + policy["min_relative_improvement"]) else 0
        previous = score
    if total >= policy["max_compute_seconds"]:
        reason = "compute_budget_exhausted"
    elif rounds and rounds[-1]["input_images"] >= policy["max_images"]:
        reason = "image_budget_reached"
    elif stagnant >= policy["max_stagnant_rounds"]:
        reason = "quality_stagnation"
    elif len(rounds) >= policy["max_rounds"]:
        reason = "round_budget_reached"
    state = rounds[-1]["status"] if rounds else "awaiting_input"
    if reason:
        state = "stopped"
    return {
        "project_id": project_id,
        "path": str(root),
        "status": state,
        "stop_reason": reason,
        "rounds": rounds,
        "finals": finals,
        "stagnant_rounds": stagnant,
        "budget": {
            **policy,
            "compute_seconds_used": total,
            "compute_seconds_remaining": max(0, policy["max_compute_seconds"] - total),
        },
        "threshold_status": project["threshold_status"],
        "verification": "not_run",
    }


def add_round(workspace, project_id, bundles, toolchain_path=None):
    workspace, root, project = read_project(workspace, project_id)
    with exclusive(root):
        state = project_status(workspace, project_id)
        require(not state["stop_reason"], "project stopped: " + str(state["stop_reason"]))
        require(
            bool(bundles) or toolchain_path is not None, "new frozen bundles or a replacement toolchain are required"
        )
        prior = read_rounds(root)
        require(
            not prior or state["rounds"][-1]["status"] in ("awaiting_review", "quality_passed", "tools_missing"),
            "complete or cancel the active round before appending inputs",
        )
        sources = list(prior[-1]["sources"]) if prior else []
        known = {source["sha256"] for source in sources}
        incoming = []
        for path in bundles:
            path = within(workspace, Path(path))
            validated = validate_bundle(path)
            sha = file_hash(path / "bundle.json")
            require(sha not in known, "frozen bundle is already in this project")
            known.add(sha)
            incoming.append((path, sha, validated))
        all_frames = {}
        for path in [root / "inputs" / source["sha256"] for source in sources] + [item[0] for item in incoming]:
            for frame in read_jsonl((path / "frames.jsonl").read_bytes(), "source frames"):
                key = (frame["session_id"], frame["frame_id"])
                require(key not in all_frames or all_frames[key] == frame, "conflicting source frame identity")
                all_frames[key] = frame
        captured = sum(frame["image"]["status"] == "captured" for frame in all_frames.values())
        require(captured <= project["policy"]["max_images"], "new inputs exceed the cumulative screenshot budget")
        for path, sha, _ in incoming:
            target = root / "inputs" / sha
            if target.exists():
                validate_bundle(target)
                require(file_hash(target / "bundle.json") == sha, "stored frozen bundle changed")
            else:
                staging = root / "inputs" / ("." + sha + ".partial")
                if staging.exists():
                    staging.rename(root / "inputs" / ("." + sha + "-" + uuid.uuid4().hex + ".interrupted"))
                check_tree(path)
                shutil.copytree(path, staging)
                validate_bundle(staging)
                require(file_hash(staging / "bundle.json") == sha, "bundle changed during snapshot")
                staging.rename(target)
            sources.append({"sha256": sha})
        round_id = f"round-{len(prior) + 1:04d}"
        directory = root / "rounds" / round_id
        staging = root / "rounds" / ("." + round_id + ".partial")
        if staging.exists():
            # Keep interrupted attempt evidence; a new attempt gets independent IDs.
            staging.rename(root / "rounds" / ("." + round_id + "-" + uuid.uuid4().hex))
        staging.mkdir()
        token = identifier(project["revision"], round_id, sources, uuid.uuid4().hex)[:32]
        job, run_id = "project-" + token, "coarse-" + token
        preparation = prepare(workspace, [root / "inputs" / source["sha256"] for source in sources], job)
        _, prepared = output_path(workspace, job)
        groups = load_json((prepared / "calibration-groups.json").read_bytes(), "calibration groups")["groups"]
        config = dict(project["config"])
        config.update(
            camera_group_mapping={
                group["camera_group_id"]: "camera-" + group["camera_group_id"][4:52] for group in groups
            },
            transform_world_from_sfm=None,
            world_frame_id=None,
            scale_status="arbitrary",
            parent_revision=None,
        )
        write_bytes(staging / "config.json", encoded(config))
        if toolchain_path is not None:
            toolchain = load_toolchain(within(workspace, Path(toolchain_path)))
        elif prior:
            toolchain = load_json(
                (root / "rounds" / prior[-1]["round_id"] / "toolchain.json").read_bytes(), "round toolchain"
            )
        else:
            toolchain = project["toolchain"]
        write_bytes(staging / "toolchain.json", encoded(toolchain))
        seed = prior[-1]["run_id"] if prior and state["rounds"][-1].get("quality") else None
        plan(
            workspace,
            job,
            run_id,
            staging / "config.json",
            staging / "toolchain.json",
            mode="coarse",
            seed_run=seed,
            max_seconds=state["budget"]["compute_seconds_remaining"],
        )
        item = {
            "round_id": round_id,
            "run_id": run_id,
            "job": job,
            "sources": sources,
            "image_count": captured,
            "eligible_images": preparation["image_count"],
            "parent_round": prior[-1]["round_id"] if prior else None,
        }
        item["revision"] = identifier(item)
        write_bytes(staging / "round.json", encoded(item))
        staging.rename(directory)
    return project_status(workspace, project_id)


def run_round(workspace, project_id, round_id=None):
    workspace, root, _ = read_project(workspace, project_id)
    with exclusive(root):
        rounds = read_rounds(root)
        require(bool(rounds), "project has no rounds")
        item = round_item(root, round_id) if round_id else rounds[-1]
        require(item == rounds[-1], "historical rounds are immutable; only the latest interrupted round can resume")
        current = status(workspace, item["run_id"])
        if current["status"] == "awaiting_review":
            return project_status(workspace, project_id)
        state = project_status(workspace, project_id)
        require(state["budget"]["compute_seconds_remaining"] > 0, "compute budget exhausted")
        # The last admitted round may run even when the input/round cap has just been reached.
        require(
            state["stop_reason"] not in ("compute_budget_exhausted", "quality_stagnation"),
            "project stopped: " + str(state["stop_reason"]),
        )
        try:
            run(
                workspace,
                item["run_id"],
                resume=current["status"] != "queued",
                time_budget_seconds=state["budget"]["compute_seconds_remaining"],
            )
        except (Exception, KeyboardInterrupt):
            # Native failure is evidence for external review, never a synthetic geometry result.
            if status(workspace, item["run_id"])["status"] not in ("failed", "cancelled"):
                raise
    return project_status(workspace, project_id)


def finalize(workspace, project_id, round_id=None, final_id="final-001", toolchain_path=None):
    workspace, root, project = read_project(workspace, project_id)
    output_path(workspace, final_id)
    with exclusive(root):
        rounds = read_rounds(root)
        require(bool(rounds), "project has no coarse rounds")
        item = round_item(root, round_id) if round_id else rounds[-1]
        state = project_status(workspace, project_id)
        selected = next(entry for entry in state["rounds"] if entry["round_id"] == item["round_id"])
        quality = selected.get("quality")
        require(
            quality and quality["status"] == "quality_passed", "largest coarse component did not pass every threshold"
        )
        _, coarse_root, prepared, preparation, coarse_plan = read_plan(workspace, item["run_id"])
        runner = Runner(workspace, coarse_root, prepared, preparation, coarse_plan)
        model = read_model(runner.verified_stage(runner, "map") / "models" / quality["model"])
        require(identifier(model) == quality["model_digest"], "quality-passed model changed")
        by_name = {image["name"]: frame_key(image["record"]["frame"]) for image in runner.images}
        registered = {by_name[image["name"]] for image in model["images"].values()}
        accepted = None
        for review in selected["imports"][-1:]:
            if quality["model_digest"] not in review["accepted_models"]:
                continue
            reviewed = {frame_key(frame) for frame in review["coverage"]["reviewed"]}
            blocking = [
                issue
                for issue in review["issues"]
                if issue["status"] == "unresolved"
                and (not issue["evidence"] or registered & {frame_key(frame) for frame in issue["evidence"]})
            ]
            if registered <= reviewed and not blocking:
                accepted = review
                break
        require(
            accepted is not None,
            "final requires imported external acceptance of this model, all registered images reviewed, and no unresolved model issues",
        )
        require(state["budget"]["compute_seconds_remaining"] > 0, "compute budget exhausted")
        directory = root / "finals" / final_id
        if directory.exists():
            final = load_json((directory / "final.json").read_bytes(), "final run")
            require(
                final["round_id"] == item["round_id"] and final["model_digest"] == quality["model_digest"],
                "final ID already belongs to another approved model",
            )
            if toolchain_path is not None:
                _, _, _, _, existing = read_plan(workspace, final["run_id"])
                replacement = load_toolchain(within(workspace, Path(toolchain_path)))
                require(
                    fingerprint(existing["toolchain"]) == fingerprint(replacement),
                    "existing final toolchain is immutable; use a new --final ID with the replacement toolchain",
                )
        else:
            run_id = "final-" + identifier(project["revision"], item["revision"], final_id)[:32]
            coarse_dir = root / "rounds" / item["round_id"]
            final_toolchain_path = (
                within(workspace, Path(toolchain_path)) if toolchain_path else coarse_dir / "toolchain.json"
            )
            final_toolchain = load_toolchain(final_toolchain_path)
            missing = [name for name, tool in final_toolchain["tools"].items() if tool["status"] != "available"]
            require(not missing, "final tools missing: " + ", ".join(missing) + "; refresh doctor and pass --toolchain")
            require(
                fingerprint(final_toolchain)["colmap"] == fingerprint(coarse_plan["toolchain"])["colmap"],
                "final must use the approved coarse COLMAP binary and capabilities",
            )
            final_source = {
                "run_id": item["run_id"],
                "model": quality["model"],
                "model_digest": quality["model_digest"],
            }
            _, planned_root = run_root(workspace, run_id)
            if planned_root.exists():
                _, _, _, _, existing = read_plan(workspace, run_id)
                require(
                    existing["job"] == item["job"]
                    and existing["execution"]["mode"] == "final"
                    and existing["execution"]["final_source"] == final_source
                    and existing["config"] == coarse_plan["config"]
                    and existing["toolchain"] == final_toolchain,
                    "interrupted final plan identity differs",
                )
            else:
                plan(
                    workspace,
                    item["job"],
                    run_id,
                    coarse_dir / "config.json",
                    final_toolchain_path,
                    mode="final",
                    final_source=final_source,
                    max_seconds=state["budget"]["compute_seconds_remaining"],
                )
            final = {
                "final_id": final_id,
                "round_id": item["round_id"],
                "run_id": run_id,
                "model": quality["model"],
                "model_digest": quality["model_digest"],
                "review_import": accepted["path"],
                "scope": "quality_passed_component_only",
                "geometry_acceptance": "not_assessed",
            }
            staging = root / "finals" / ("." + final_id + "-" + uuid.uuid4().hex + ".partial")
            staging.mkdir()
            write_bytes(staging / "final.json", encoded(final))
            staging.rename(directory)
        current = status(workspace, final["run_id"])
        if current["status"] != "needs_review":
            run(
                workspace,
                final["run_id"],
                resume=current["status"] != "queued",
                time_budget_seconds=state["budget"]["compute_seconds_remaining"],
            )
    return project_status(workspace, project_id)


def frame_key(item):
    return item["session_id"], item["frame_id"]


def export_review(workspace, project_id, round_id, review_id):
    from tools.capture.rescan import source_context

    workspace, root, project = read_project(workspace, project_id)
    output_path(workspace, review_id)
    with exclusive(root):
        item = round_item(root, round_id)
        state = round_status(workspace, root, item, project)
        require(
            state["status"] in ("awaiting_review", "quality_passed", "tools_missing"),
            "finish the coarse round before review",
        )
        target = root / "reviews" / review_id
        staging = root / "reviews" / ("." + review_id + ".partial")
        require(not target.exists() and not staging.exists(), "review ID already exists or has an interrupted export")
        staging.mkdir()
        sources, indices, evidence = [], {}, []
        for source in item["sources"]:
            bundle = root / "inputs" / source["sha256"]
            validate_bundle(bundle)
            require(file_hash(bundle / "bundle.json") == source["sha256"], "stored frozen bundle changed")
            context = source_context(bundle)
            context["bundle_uri"] = bundle.relative_to(workspace).as_posix()
            sources.append(context)
            session = load_json((bundle / "session.json").read_bytes(), "session")
            source_plan = load_json((bundle / "plan.json").read_bytes(), "capture plan")
            waypoints = {point["waypoint_id"]: point for point in source_plan["waypoints"]}
            auto_plan = (
                load_json((bundle / "auto-plan.json").read_bytes(), "auto plan")
                if (bundle / "auto-plan.json").is_file()
                else None
            )
            auto_waypoints = {point["waypoint_id"]: point for point in auto_plan["waypoints"]} if auto_plan else {}
            calibrations = {group["calibration_group_id"]: group for group in session["calibration_groups"]}
            prefix = staging / "sources" / source["sha256"]
            prefix.mkdir(parents=True)
            for filename in ("bundle.json", "session.json", "plan.json", "auto-plan.json", "rescan-candidates.json"):
                if (bundle / filename).is_file():
                    shutil.copyfile(bundle / filename, prefix / filename)
            for frame in read_jsonl((bundle / "frames.jsonl").read_bytes(), "frames"):
                key = frame_key(frame)
                entry = {
                    "session_id": key[0],
                    "frame_id": key[1],
                    "frame": frame,
                    "waypoint": waypoints[frame["waypoint_id"]],
                    "auto_waypoint": auto_waypoints.get(waypoints[frame["waypoint_id"]].get("auto_plan_waypoint_id")),
                    "calibration": calibrations[frame["calibration_group_id"]],
                    "region_id": source_plan["region_id"],
                    "map_coordinates": source_plan["map_coordinates"],
                    "source_bundles": [source["sha256"]],
                    "review_status": "unreviewed",
                }
                if key in indices:
                    require(indices[key]["frame"] == frame, "conflicting frame identity in review")
                    indices[key]["source_bundles"].append(source["sha256"])
                    continue
                indices[key] = entry
                if frame["image"]["status"] == "captured":
                    evidence.append(
                        {
                            "session_id": key[0],
                            "frame_id": key[1],
                            "image_uri": frame["image"]["uri"],
                            "image_sha256": frame["image"]["sha256"],
                            "bundle_sha256": source["sha256"],
                            "package_image_uri": None,
                        }
                    )
        _, run_directory = run_root(workspace, item["run_id"])
        quality = state.get("quality")
        issues, selected_names, cameras = [], set(), None
        if quality:
            _, _, prepared, preparation, record = read_plan(workspace, item["run_id"])
            runner = Runner(workspace, run_directory, prepared, preparation, record)
            model_dir = runner.verified_stage(runner, "map") / "models" / quality["model"]
            model = read_model(model_dir)
            require(identifier(model) == quality["model_digest"], "coarse model changed since quality analysis")
            selected_names = {image["name"] for image in model["images"].values()}
            image_map = {image["name"]: image["record"]["frame"] for image in images_for(prepared, record["config"])}
            cameras = camera_records(model)
            for camera in cameras["images"]:
                frame = image_map[camera["image_name"]]
                camera.update(session_id=frame["session_id"], frame_id=frame["frame_id"])
            write_bytes(staging / "cameras.json", encoded(cameras))
            shutil.copytree(model_dir, staging / "sparse-model")
            unregistered = [frame for name, frame in image_map.items() if name not in selected_names]
            if unregistered:
                issues.append(
                    {
                        "issue_id": "unregistered-images",
                        "classification": "insufficient_evidence",
                        "description": f"{len(unregistered)} eligible images are outside the largest model; the cause needs image review.",
                        "evidence": [
                            {"session_id": frame["session_id"], "frame_id": frame["frame_id"]} for frame in unregistered
                        ],
                        "origin": "measured",
                        "status": "unresolved",
                    }
                )
            if quality["failed_checks"]:
                issues.append(
                    {
                        "issue_id": "coarse-thresholds",
                        "classification": "insufficient_evidence",
                        "description": "Measured numeric gates failed: " + ", ".join(quality["failed_checks"]),
                        "evidence": [
                            {"session_id": camera["session_id"], "frame_id": camera["frame_id"]}
                            for camera in cameras["images"]
                        ],
                        "origin": "measured",
                        "status": "unresolved",
                    }
                )
        else:
            issues.append(
                {
                    "issue_id": "native-reconstruction-failure",
                    "classification": "localization_or_tool_failure",
                    "description": state.get("stop_reason") or "No parseable coarse model exists.",
                    "evidence": [],
                    "origin": "measured",
                    "status": "unresolved",
                }
            )
        problem_keys = {frame_key(reference) for issue in issues for reference in issue["evidence"]}
        selected_evidence = sorted(
            evidence, key=lambda entry: (frame_key(entry) not in problem_keys, frame_key(entry))
        )[:16]
        for entry in selected_evidence:
            bundle = root / "inputs" / entry["bundle_sha256"]
            suffix = Path(entry["image_uri"]).suffix.lower()
            image_uri = "images/" + identifier(*frame_key(entry)) + suffix
            destination = staging / image_uri
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(safe_path(bundle, entry["image_uri"]), destination)
            require(file_hash(destination) == entry["image_sha256"], "review image changed during export")
            entry["package_image_uri"] = image_uri
        frame_index = [indices[key] for key in sorted(indices)]
        regions = [
            {
                "region_id": region_id,
                "frame_ids": [
                    {"session_id": entry["session_id"], "frame_id": entry["frame_id"]}
                    for entry in frame_index
                    if entry["region_id"] == region_id
                ],
                "review_status": "unreviewed",
            }
            for region_id in sorted({entry["region_id"] for entry in frame_index})
        ]
        write_bytes(staging / "frame-index.json", encoded(frame_index))
        write_bytes(
            staging / "quality.json",
            encoded(
                quality if quality else {"status": "awaiting_review", "error": state.get("stop_reason"), "model": None}
            ),
        )
        logs = run_directory / "logs"
        if logs.is_dir():
            destination = staging / "logs"
            destination.mkdir()
            for log in sorted(logs.glob("*.log")):
                # Bound external-review attachments while retaining native diagnostics locally.
                with check_path(log).open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 65536))
                    write_bytes(destination / log.name, stream.read())
        revision = identifier(item["revision"], state.get("quality"), status(workspace, item["run_id"]))
        response = {
            "schema_version": "reconstruction.review-result/1.0",
            "project_id": project_id,
            "round_id": round_id,
            "review_id": review_id,
            "reconstruction_revision": revision,
            "reviewer": "",
            "coverage": {
                "reviewed": [],
                "failed": [],
                "unreviewed": [
                    {"session_id": entry["session_id"], "frame_id": entry["frame_id"]} for entry in frame_index
                ],
                "regions": [{"region_id": group["region_id"], "status": "unreviewed"} for group in regions],
            },
            "issues": issues,
            "rescan": None,
            "accepted_models": [],
        }
        write_bytes(staging / "response-template.json", encoded(response))
        instructions = (
            "Review reconstruction evidence without assuming image coverage from an overview. "
            "frame-index.json lists every captured/failed frame and its immutable source metadata; "
            "images/ contains at most 16 original issue/reference images, not the complete collection. "
            "Keep all unseen images and regions unreviewed. Do not infer a cause from low sparse statistics alone. "
            "Use source_image_quality, insufficient_observation, difficult_material, localization_or_tool_failure, "
            "or insufficient_evidence. Every image claim must reference a session/frame pair. "
            "Return response-template.json with an explicit reviewer and complete disjoint coverage. "
            "Put a model_digest into accepted_models only after reviewing every image registered in that model; "
            "unresolved issues on any of its images block final refinement. "
            "Never invent map coordinates, floors, routes, material recovery or measurements. "
            "Rescan actions may select only frozen source candidates; otherwise keep rescan=null and explain needs_mapping. "
            "No API or model credential is needed by this exporter."
        )
        write_bytes(staging / "REVIEW.txt", instructions.encode("utf-8"))
        package = {
            "schema_version": "reconstruction.review/1.0",
            "project_id": project_id,
            "round_id": round_id,
            "review_id": review_id,
            "reconstruction_revision": revision,
            "region_id": sources[0]["region_id"],
            "sources": sources,
            "evidence": evidence,
            "frame_index": "frame-index.json",
            "regions": regions,
            "coverage": response["coverage"],
            "issues": issues,
            "quality": "quality.json",
            "response_template": "response-template.json",
            "files": inventory(staging),
            "geometry_acceptance": "not_assessed",
            "threshold_status": "project_policy_not_calibrated",
        }
        schema_check("review", package)
        write_bytes(staging / "review.json", encoded(package))
        staging.rename(target)
        write_bytes(
            root / "review-index" / (review_id + ".json"),
            encoded(
                {
                    "sha256": file_hash(target / "review.json"),
                    "round_id": round_id,
                    "round_revision": item["revision"],
                }
            ),
        )
    return {
        "status": "awaiting_external_review",
        "round_id": round_id,
        "review_id": review_id,
        "path": str(target),
        "review_file": str(target / "review.json"),
        "response_template": str(target / "response-template.json"),
        "reconstruction_revision": revision,
        "bundle_paths": [
            {
                "source_id": source["bundle_sha256"],
                "path": str(workspace / source["bundle_uri"]),
                "automatic_rescan": source["automatic_rescan"],
            }
            for source in sources
        ],
    }


def verify_review(workspace, project_id, review_path):
    workspace, root, project = read_project(workspace, project_id)
    path = within(workspace, Path(review_path))
    require(path.name == "review.json" and path.parent.parent == root / "reviews", "review is outside this project")
    output_path(workspace, path.parent.name)
    package = load_json(path.read_bytes(), "review package")
    schema_check("review", package)
    index = load_json(check_path(root / "review-index" / (path.parent.name + ".json")).read_bytes(), "review receipt")
    require(index["sha256"] == file_hash(path), "immutable review package changed")
    require(
        package["project_id"] == project_id and package["review_id"] == path.parent.name, "review identity mismatch"
    )
    rounds = read_rounds(root)
    require(rounds and package["round_id"] == rounds[-1]["round_id"] == index["round_id"], "review is stale")
    item = rounds[-1]
    require(item["revision"] == index["round_revision"], "review round changed")
    state = round_status(workspace, root, item, project)
    revision = identifier(item["revision"], state.get("quality"), status(workspace, item["run_id"]))
    require(package["reconstruction_revision"] == revision, "review does not match current coarse evidence")
    actual = [entry for entry in inventory(path.parent) if entry["uri"] != "review.json"]
    require(actual == package["files"], "review evidence inventory changed")
    require(
        {source["bundle_sha256"] for source in package["sources"]} == {source["sha256"] for source in item["sources"]},
        "review source set mismatch",
    )
    return package


def import_review(workspace, project_id, response_path):
    from tools.capture.rescan import validate_rescan

    workspace, root, _ = read_project(workspace, project_id)
    response = load_json(within(workspace, Path(response_path)).read_bytes(), "external review result")
    schema_check("review-result", response)
    require(response["project_id"] == project_id, "review result belongs to another project")
    with exclusive(root):
        rounds = read_rounds(root)
        require(
            rounds and rounds[-1]["round_id"] == response["round_id"], "review result is stale for the active round"
        )
        output_path(workspace, response["review_id"])
        review_path = root / "reviews" / response["review_id"] / "review.json"
        package = verify_review(workspace, project_id, review_path)
        require(
            package["project_id"] == project_id
            and package["round_id"] == response["round_id"]
            and package["reconstruction_revision"] == response["reconstruction_revision"],
            "review revision mismatch",
        )
        actual = [entry for entry in inventory(review_path.parent) if entry["uri"] != "review.json"]
        require(actual == package["files"], "exported review evidence changed")
        frames = load_json(safe_path(review_path.parent, package["frame_index"]).read_bytes(), "review frame index")
        known = {frame_key(frame) for frame in frames}
        covered = []
        for state in ("reviewed", "unreviewed", "failed"):
            covered.extend(frame_key(frame) for frame in response["coverage"][state])
        require(
            len(covered) == len(set(covered)) and set(covered) == known,
            "coverage must classify every source frame exactly once",
        )
        reviewed = {frame_key(frame) for frame in response["coverage"]["reviewed"]}
        require(
            reviewed <= {frame_key(frame) for frame in frames if frame["frame"]["image"]["status"] == "captured"},
            "frames without captured images cannot be marked image-reviewed",
        )
        quality = load_json(safe_path(review_path.parent, package["quality"]).read_bytes(), "review quality")
        require(
            set(response["accepted_models"]) <= ({quality["model_digest"]} if quality.get("model_digest") else set()),
            "review accepts an unknown model",
        )
        regions = response["coverage"]["regions"]
        require(
            len({region["region_id"] for region in regions}) == len(regions)
            and {region["region_id"] for region in regions} == {group["region_id"] for group in package["regions"]},
            "region coverage must list every source region exactly once",
        )
        for region in regions:
            region_frames = {frame_key(frame) for frame in frames if frame["region_id"] == region["region_id"]}
            require(
                region["status"] != "reviewed" or region_frames <= reviewed,
                "a region with unseen or failed images cannot be marked reviewed",
            )
        issue_ids = set()
        for issue in response["issues"]:
            require(issue["issue_id"] not in issue_ids, "duplicate review issue ID")
            issue_ids.add(issue["issue_id"])
            refs = {frame_key(frame) for frame in issue["evidence"]}
            require(refs <= known, "review issue references an unknown source frame")
            if issue["classification"] in ("source_image_quality", "difficult_material"):
                require(
                    bool(refs) and refs <= reviewed, "image/material claims require actually reviewed image evidence"
                )
        rescan = response["rescan"]
        if rescan is not None:
            by_id = {issue["issue_id"]: issue for issue in response["issues"]}
            for issue in rescan.get("issues", []):
                bound = by_id.get(issue.get("issue_id"))
                require(
                    bound is not None and bound["status"] == "unresolved",
                    "rescan issue is not an unresolved reviewed issue",
                )
                require(
                    {frame_key(frame) for frame in issue.get("evidence", [])}
                    <= {frame_key(frame) for frame in bound["evidence"]},
                    "rescan evidence differs from reviewed issue",
                )
            source = next(
                (
                    source
                    for source in package["sources"]
                    if source["bundle_sha256"] == rescan.get("source", {}).get("bundle_sha256")
                ),
                None,
            )
            require(source is not None, "rescan references an unknown frozen source")
            validate_rescan(workspace, rescan, review_path, within(workspace, workspace / source["bundle_uri"]))
        revision = digest(encoded(response))
        target = root / "imports" / (revision + ".json")
        if not target.exists():
            write_bytes(target, encoded(response))
        else:
            require(file_hash(target) == revision, "immutable imported review changed")
        receipts = sorted((root / "import-index").glob("*.json"))
        if not any(load_json(path.read_bytes(), "import receipt")["response_sha256"] == revision for path in receipts):
            sequence = len(receipts) + 1
            pending = root / "import-index" / ("." + uuid.uuid4().hex + ".pending")
            write_bytes(pending, encoded({"sequence": sequence, "response_sha256": revision}))
            pending.rename(root / "import-index" / f"{sequence:08d}.json")
    return {
        "status": "review_imported",
        "round_id": response["round_id"],
        "review_id": response["review_id"],
        "path": str(target),
        "review_file": str(review_path),
        "coverage": response["coverage"],
        "issues": response["issues"],
        "rescan": rescan,
        "bundle_paths": [
            {"source_id": source["bundle_sha256"], "path": str(workspace / source["bundle_uri"])}
            for source in package["sources"]
        ],
    }
