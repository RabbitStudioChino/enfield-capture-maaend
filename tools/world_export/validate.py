"""Validate producer output without importing the web repository's runtime."""

import math
import struct
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from tools.blender.common import REPO, content_hash, decode_json, digest, load_json, require, safe_file, workspace_path

CONTRACTS = REPO / "vendor/world-contracts"
SCHEMAS = (
    "world-release.schema.json",
    "asset-index.schema.json",
    "collision.schema.json",
    "collision-trimesh.schema.json",
)


def validators():
    lock = load_json(REPO / "contracts.lock")
    require(
        lock["repository"] == "https://github.com/RabbitStudioChino/endfield-world-web", "unexpected contract owner"
    )
    require(
        len(lock["commit"]) == 40 and all(c in "0123456789abcdef" for c in lock["commit"]),
        "contracts must pin a full commit",
    )
    require(set(lock["schemas"]) == set(SCHEMAS), "contract lock must pin all four world schemas")
    schemas = {}
    for name in SCHEMAS:
        path = safe_file(CONTRACTS, name)
        require(digest(path) == lock["schemas"][name], f"vendored contract changed: {name}")
        schema = load_json(path)
        Draft202012Validator.check_schema(schema)
        schemas[name] = schema
    registry = Registry().with_resources((schema["$id"], Resource.from_contents(schema)) for schema in schemas.values())
    return {name: Draft202012Validator(schema, registry=registry) for name, schema in schemas.items()}


def check_schema(checks, name, data):
    error = next(checks[name].iter_errors(data), None)
    require(error is None, f"{name}: {error.message if error else ''}")


def glb_document(path):
    data = Path(path).read_bytes()
    require(len(data) >= 20, "truncated GLB")
    magic, version, length = struct.unpack_from("<III", data)
    require(magic == 0x46546C67 and version == 2 and length == len(data), "invalid GLB header")
    chunks, cursor = [], 12
    while cursor < length:
        require(cursor + 8 <= length, "truncated GLB chunk")
        size, kind = struct.unpack_from("<II", data, cursor)
        require(size % 4 == 0 and cursor + 8 + size <= length, "invalid GLB chunk length")
        chunks.append((kind, data[cursor + 8 : cursor + 8 + size]))
        cursor += 8 + size
    require(
        len(chunks) == 2 and chunks[0][0] == 0x4E4F534A and chunks[1][0] == 0x004E4942,
        "GLB must embed one JSON and one BIN chunk",
    )
    document = decode_json(chunks[0][1])
    require(document.get("asset", {}).get("version") == "2.0", "unsupported glTF")
    require(
        len(document.get("buffers", [])) == 1 and "uri" not in document["buffers"][0], "GLB buffers must be embedded"
    )
    require(document["buffers"][0]["byteLength"] <= len(chunks[1][1]), "GLB buffer exceeds BIN")
    require(
        all("uri" not in image and "bufferView" in image for image in document.get("images", [])),
        "GLB images must be embedded",
    )
    extensions = document.get("extensionsRequired", [])
    require(
        set(extensions + document.get("extensionsUsed", [])) <= {"KHR_materials_unlit"},
        "unsupported required glTF extension",
    )
    require(document.get("meshes"), "GLB contains no meshes")
    for view in document.get("bufferViews", []):
        require(
            view.get("buffer", 0) == 0
            and view.get("byteOffset", 0) >= 0
            and view["byteLength"] > 0
            and view.get("byteOffset", 0) + view["byteLength"] <= document["buffers"][0]["byteLength"],
            "GLB bufferView out of bounds",
        )
    return document


def inside(bounds, point):
    return all(bounds["min"][axis] - 1e-6 <= value <= bounds["max"][axis] + 1e-6 for axis, value in enumerate(point))


def area_squared(a, b, c):
    u, v = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
    cross = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    return sum(value * value for value in cross)


def check_collision(collision, bounds):
    vertices = collision["vertices"]
    rounded = []
    for vertex in vertices:
        require(inside(bounds, vertex), "collision exceeds chunk bounds")
        try:
            rounded.append(struct.unpack("fff", struct.pack("fff", *vertex)))
        except (OverflowError, struct.error) as error:
            raise ValueError("collision exceeds finite float32 coordinates") from error
        require(all(math.isfinite(value) for value in rounded[-1]), "collision exceeds finite float32 coordinates")
    for triangle in collision["triangles"]:
        require(all(index < len(vertices) for index in triangle), "triangle index out of range")
        for points in (vertices, rounded):
            area = area_squared(*(points[index] for index in triangle))
            require(math.isfinite(area) and area > 1e-20, "degenerate collision triangle")


def check_spawn_support(collision, spawn):
    x, y, z = spawn
    for triangle in collision["triangles"]:
        a, b, c = (collision["vertices"][index] for index in triangle)
        determinant = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
        if abs(determinant) < 1e-12:
            continue
        u = ((b[2] - c[2]) * (x - c[0]) + (c[0] - b[0]) * (z - c[2])) / determinant
        v = ((c[2] - a[2]) * (x - c[0]) + (a[0] - c[0]) * (z - c[2])) / determinant
        height = u * a[1] + v * b[1] + (1 - u - v) * c[1]
        normal_y = (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2])
        if (
            u >= -1e-6
            and v >= -1e-6
            and u + v <= 1 + 1e-6
            and -1e-4 <= y - height <= 0.15
            and normal_y / math.sqrt(area_squared(a, b, c)) >= math.cos(math.pi / 4)
        ):
            return
    raise ValueError("Spawn lacks an upward walkable support triangle within 0.15 m")


def validate_release(root):
    root = workspace_path(root)
    checks = validators()
    world = load_json(safe_file(root, "world.json"))
    check_schema(checks, "world-release.schema.json", world)
    require(world["world_frame"]["scale_status"] in ("estimated", "verified"), "arbitrary scale cannot be released")
    if world["world_frame"]["scale_status"] == "verified":
        require("max_error_m" in world["world_frame"]["scale_reference"], "verified scale requires max_error_m")
    require(world["camera"]["near_m"] < world["camera"]["far_m"], "camera clipping planes inverted")
    bounds = world["roam_bounds_world"]
    require(all(a < b for a, b in zip(bounds["min"], bounds["max"], strict=True)), "inverted roam bounds")
    require(inside(bounds, world["spawn"]["position"]), "Spawn outside roam bounds")
    require(abs(sum(v * v for v in world["spawn"]["rotation_xyzw"]) - 1) < 1e-6, "Spawn quaternion must be unit length")
    index_path = safe_file(root, world["asset_index_uri"])
    require(digest(index_path) == world["asset_index_sha256"], "asset index hash mismatch")
    index = load_json(index_path)
    check_schema(checks, "asset-index.schema.json", index)
    assets, uris, collisions = {}, set(), {}
    for asset in index["assets"]:
        key, uri = asset["asset_id"], asset["uri"]
        require(key not in assets and uri not in uris, "duplicate asset id or URI")
        path = safe_file(root, uri)
        require(
            path.stat().st_size == asset["byte_length"] and digest(path) == asset["sha256"],
            f"asset integrity failure: {uri}",
        )
        require(set(asset["required_extensions"]) <= {"KHR_materials_unlit"}, "unsupported required extension")
        if asset["type"] in ("render_mesh", "character"):
            document = glb_document(path)
            require(
                set(document.get("extensionsRequired", [])) == set(asset["required_extensions"]),
                "GLB extension declaration missing",
            )
        elif asset["type"] == "collision":
            collision = load_json(path)
            check_schema(checks, "collision-trimesh.schema.json", collision)
            collisions[key] = collision
        else:
            raise ValueError("producer supports embedded GLB and triangle collision assets only")
        assets[key] = asset
        uris.add(uri)
    for asset in assets.values():
        require(all(key in assets for key in asset["dependencies"]), "missing asset dependency")
        require(not asset["dependencies"], "embedded producer assets must not have external dependencies")
    require(
        world["character_asset_id"] in assets and assets[world["character_asset_id"]]["type"] == "character",
        "character reference invalid",
    )
    chunks = set()
    for chunk in world["chunks"]:
        require(chunk["id"] not in chunks, "duplicate chunk")
        chunks.add(chunk["id"])
        require(
            chunk["transform"] == {"position": [0, 0, 0], "rotation_xyzw": [0, 0, 0, 1], "scale": 1},
            "producer geometry must have a single baked world transform",
        )
        require(
            all(a < b for a, b in zip(chunk["bounds_world"]["min"], chunk["bounds_world"]["max"], strict=True)),
            "inverted chunk bounds",
        )
        require(
            inside(bounds, chunk["bounds_world"]["min"]) and inside(bounds, chunk["bounds_world"]["max"]),
            "chunk outside roam bounds",
        )
        require(chunk["collision_asset_id"] in collisions, "missing collision reference")
        check_collision(collisions[chunk["collision_asset_id"]], chunk["bounds_world"])
        require(set(chunk["render_variants"]) == set(world["profiles"]), "profile coverage mismatch")
        for lods in chunk["render_variants"].values():
            require(
                lods[0]["min_distance_m"] == 0 and lods[0]["hysteresis_m"] == 0,
                "first LOD distance and hysteresis must be zero",
            )
            previous = -1
            for lod in lods:
                require(lod["min_distance_m"] > previous, "LOD thresholds must increase")
                if previous >= 0:
                    require(
                        0 < lod["hysteresis_m"] < lod["min_distance_m"] - previous,
                        "LOD hysteresis must be positive and below the distance gap",
                    )
                previous = lod["min_distance_m"]
                require(
                    lod["asset_id"] in assets and assets[lod["asset_id"]]["type"] == "render_mesh",
                    "missing render mesh",
                )
        require(all(key in assets for key in chunk["shared_dependencies"]), "missing shared dependency")
    for chunk in world["chunks"]:
        require(all(key in chunks for key in chunk["neighbors"]), "missing neighbor chunk")
    check_spawn_support(collisions[world["chunks"][0]["collision_asset_id"]], world["spawn"]["position"])
    source = world["build"]["master_source"]
    source_path = safe_file(root, source["uri"])
    require(
        digest(source_path) == source["sha256"] and source_path.stat().st_size == source["bytes"],
        "master source hash mismatch",
    )
    master = load_json(source_path)
    require(
        master["master_revision"] == world["provenance"]["master_revision"]
        and master["active_reconstruction"] == world["provenance"]["reconstruction_revision"]
        and master["world_frame_id"] == world["world_frame"]["id"]
        and master["source"]["input_batch_ids"] == world["provenance"]["input_batch_ids"],
        "master provenance mismatch",
    )
    require(
        content_hash({key: value for key, value in master.items() if key != "master_revision"})
        == master["master_revision"],
        "master revision hash mismatch",
    )
    require(
        content_hash(master["export_config"]) == world["build"]["export_config_sha256"],
        "export configuration provenance mismatch",
    )
    for uri, expected_hash in master["exported_asset_sha256"].items():
        require(uri in uris and digest(safe_file(root, uri)) == expected_hash, "derived asset provenance mismatch")
    for path in root.rglob("*"):
        workspace_path(path)
    expected = uris | {"world.json", world["asset_index_uri"], source["uri"]}
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    require(actual == expected, "unindexed or missing release files")
    return world
