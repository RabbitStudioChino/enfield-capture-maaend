"""Export an immutable WorldRelease using pinned local contracts."""

import argparse
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from tools.blender.common import (
    REPO,
    VERSION,
    content_hash,
    digest,
    identifier,
    load_json,
    profile_filename,
    profile_lods,
    require,
    run_blender,
    workspace_path,
    write_json,
)

from .validate import CONTRACTS, SCHEMAS, glb_document, validate_release, validators


def vendor(repository, commit):
    repository = workspace_path(repository)
    require(len(commit) == 40 and all(c in "0123456789abcdef" for c in commit), "provide a full immutable commit SHA")
    origin = subprocess.check_output(["git", "-C", str(repository), "remote", "get-url", "origin"], text=True).strip()
    require("RabbitStudioChino/endfield-world-web" in origin, "world contracts must come from the owning repository")
    contents = {
        name: subprocess.check_output(["git", "-C", str(repository), "show", f"{commit}:contracts/world/{name}"])
        for name in SCHEMAS
    }
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, data in contents.items():
        path = CONTRACTS / name
        path.write_bytes(data)
        hashes[name] = digest(path)
    write_json(
        REPO / "contracts.lock",
        {"repository": "https://github.com/RabbitStudioChino/endfield-world-web", "commit": commit, "schemas": hashes},
    )
    validators()


def build(args):
    master, config_path, releases = (
        workspace_path(args.master),
        workspace_path(args.config),
        workspace_path(args.releases),
    )
    require(master.is_file() and master.suffix == ".blend", "master must be an existing .blend")
    config = load_json(config_path)
    for key in ("scene_id", "release_id"):
        identifier(config[key])
    require(
        set(config["profiles"]) <= {"mobile", "balanced", "high"} and "mobile" in config["profiles"],
        "mobile profile is required",
    )
    for profile in config["profiles"].values():
        lods = profile_lods(profile)
        require(isinstance(lods, list) and lods, "profile LOD list must not be empty")
        require(
            lods[0]["min_distance_m"] == 0 and lods[0]["hysteresis_m"] == 0, "LOD0 distance and hysteresis must be zero"
        )
        previous = -1
        for lod in lods:
            require(
                type(lod["decimate_ratio"]) in (float, int) and 0 < lod["decimate_ratio"] <= 1,
                "profile decimate_ratio must be in (0,1]",
            )
            require(
                type(lod["min_distance_m"]) in (float, int) and lod["min_distance_m"] > previous,
                "LOD distances must increase",
            )
            require(
                type(lod["hysteresis_m"]) in (float, int) and lod["hysteresis_m"] >= 0,
                "LOD hysteresis must be nonnegative",
            )
            if previous >= 0:
                require(
                    0 < lod["hysteresis_m"] < lod["min_distance_m"] - previous,
                    "LOD hysteresis must be positive and below the distance gap",
                )
            previous = lod["min_distance_m"]
    require(type(config["unlit"]) is bool, "explicit unlit boolean required")
    require(config["source_kind"] in ("synthetic_fixture", "reconstruction"), "explicit source_kind required")
    require(bool(config["description"]), "provenance description required")
    character = workspace_path(config_path.parent / config["character_glb"])
    character_document = glb_document(character)
    validators()
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / config["release_id"]
    require(not destination.exists(), "release already exists; choose a new release_id")
    lock = releases / ("." + config["release_id"] + ".lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    staging = releases / ("." + config["release_id"] + ".staging-" + uuid.uuid4().hex)
    try:
        staging.mkdir()
        require(not destination.exists(), "release already exists")
        snapshot = staging / "_export-config.json"
        write_json(snapshot, config)
        master_hash = digest(master)
        run_blender(args.blender, ["export", str(snapshot), str(staging)], master)
        require(digest(master) == master_hash, "input master changed during export")
        snapshot.unlink()
        result_path = staging / "export-result.json"
        result = load_json(result_path)
        result_path.unlink()
        shutil.copyfile(character, staging / "character.glb")
        master_record = result["master"]
        assets = []
        for name, kind, filename in [
            ("collision", "collision", "collision.json"),
            ("character", "character", "character.glb"),
        ]:
            extensions = character_document.get("extensionsRequired", []) if kind == "character" else []
            path = staging / filename
            assets.append(
                {
                    "asset_id": name,
                    "type": kind,
                    "uri": filename,
                    "sha256": digest(path),
                    "byte_length": path.stat().st_size,
                    "dependencies": [],
                    "required_extensions": extensions,
                }
            )
        variants = {}
        for profile, settings in config["profiles"].items():
            variants[profile] = []
            for lod_index, lod in enumerate(profile_lods(settings)):
                filename = profile_filename(profile, lod_index)
                path = staging / filename
                document = glb_document(path)
                asset_id = f"render-{profile}-{lod_index}"
                assets.append(
                    {
                        "asset_id": asset_id,
                        "type": "render_mesh",
                        "uri": filename,
                        "sha256": digest(path),
                        "byte_length": path.stat().st_size,
                        "dependencies": [],
                        "required_extensions": document.get("extensionsRequired", []),
                    }
                )
                variants[profile].append(
                    {"asset_id": asset_id, "min_distance_m": lod["min_distance_m"], "hysteresis_m": lod["hysteresis_m"]}
                )
        index = staging / "asset-index.json"
        write_json(index, {"schema_version": "world.assets/1.0", "assets": assets})
        source_path = staging / "master-source.json"
        source = {"uri": source_path.name, "sha256": digest(source_path), "bytes": source_path.stat().st_size}
        world = {
            "schema_version": "world.release/1.0",
            "scene_id": config["scene_id"],
            "release_id": config["release_id"],
            "world_frame": {
                "id": master_record["world_frame_id"],
                "handedness": "right",
                "up_axis": "+Y",
                "unit": "meter",
                "scale_status": master_record["scale_status"],
                "scale_reference": config["scale_reference"],
            },
            "roam_bounds_world": config["roam_bounds_world"],
            "spawn": result["spawn"],
            "anchors": result["anchors"],
            "profiles": list(config["profiles"]),
            "chunks": [
                {
                    "id": "region",
                    "bounds_world": result["bounds_world"],
                    "transform": {"position": [0, 0, 0], "rotation_xyzw": [0, 0, 0, 1], "scale": 1},
                    "render_variants": variants,
                    "collision_asset_id": "collision",
                    "neighbors": [],
                    "shared_dependencies": [],
                }
            ],
            "asset_index_uri": index.name,
            "asset_index_sha256": digest(index),
            "character_asset_id": "character",
            "camera": config["camera"],
            "provenance": {
                "source_kind": config["source_kind"],
                "input_batch_ids": master_record["source"]["input_batch_ids"],
                "reconstruction_revision": master_record["active_reconstruction"],
                "master_revision": master_record["master_revision"],
                "exporter_version": VERSION,
                "description": config["description"],
            },
            "build": {
                "master_blend_sha256": master_hash,
                "export_config_sha256": content_hash(config),
                "contracts_lock_sha256": digest(REPO / "contracts.lock"),
                "master_source": source,
            },
        }
        write_json(staging / "world.json", world)
        validate_release(staging)
        require(not destination.exists(), "release already exists")
        staging.rename(destination)
    except BaseException:
        # Keep failed staging for diagnostics. Previously published releases remain intact.
        print(f"Failed staging retained: {staging}", file=sys.stderr)
        raise
    finally:
        os.close(descriptor)
        lock.unlink()
    print(f"Published {destination}\nworld.json SHA-256 {digest(destination / 'world.json')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("build")
    package.add_argument("--master", required=True, type=Path)
    package.add_argument("--config", required=True, type=Path)
    package.add_argument("--releases", required=True, type=Path)
    package.add_argument("--blender", default="/Applications/Blender.app/Contents/MacOS/Blender")
    check = commands.add_parser("validate")
    check.add_argument("release", type=Path)
    lock = commands.add_parser("vendor")
    lock.add_argument("--repository", required=True, type=Path)
    lock.add_argument("--commit", required=True)
    args = parser.parse_args()
    if args.command == "vendor":
        vendor(args.repository, args.commit)
    elif args.command == "validate":
        validate_release(workspace_path(args.release))
        print("WorldRelease producer validation passed")
    else:
        build(args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from None
