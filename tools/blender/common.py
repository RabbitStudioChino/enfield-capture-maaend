"""Pure-Python input and subprocess boundary shared with Blender's Python."""

import hashlib
import json
import math
import os
import re
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKSPACE = REPO.parent
VERSION = "world-export/1.0"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def decode_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"duplicate JSON key {key}")
            result[key] = value
        return result

    def number(value):
        parsed = float(value)
        require(math.isfinite(parsed), "non-finite JSON number")
        return parsed

    return json.loads(
        data,
        object_pairs_hook=pairs,
        parse_float=number,
        parse_int=lambda value: int(value) if math.isfinite(float(value)) else number(value),
        parse_constant=number,
    )


def load_json(path):
    return decode_json(Path(path).read_bytes())


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def content_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def workspace_path(path):
    original = Path(path).absolute()
    for component in (*reversed(original.parents), original):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        require(
            not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
            f"symlink or reparse point forbidden: {component}",
        )
    resolved = original.resolve()
    require(resolved.is_relative_to(WORKSPACE), f"path must stay inside workspace: {path}")
    return resolved


def safe_file(root, uri):
    require(
        isinstance(uri, str) and re.fullmatch(r"[A-Za-z0-9_. -]+(?:/[A-Za-z0-9_. -]+)*", uri), f"unsafe path {uri!r}"
    )
    require(all(part not in (".", "..") for part in uri.split("/")), f"unsafe path {uri!r}")
    path = workspace_path(root)
    for part in uri.split("/"):
        path /= part
        workspace_path(path)
    require(path.is_file(), f"missing file {uri}")
    return path


def identifier(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value), f"invalid identifier {value!r}"
    )
    return value


def validate_transform(transform):
    require(isinstance(transform, dict), "explicit transform_world_from_sfm is required")
    scale, rotation, translation = transform["s"], transform["R"], transform["t"]
    require(type(scale) in (int, float) and math.isfinite(scale) and scale > 0, "scale must be positive and finite")
    require(
        len(rotation) == 3 and all(len(row) == 3 for row in rotation) and len(translation) == 3, "transform dimensions"
    )
    require(
        all(type(value) in (int, float) and math.isfinite(value) for row in rotation for value in row)
        and all(type(value) in (int, float) and math.isfinite(value) for value in translation),
        "non-finite transform",
    )
    for i in range(3):
        for j in range(3):
            require(
                abs(sum(rotation[k][i] * rotation[k][j] for k in range(3)) - (i == j)) < 1e-5, "R must be orthonormal"
            )
    a, b, c = rotation
    determinant = (
        a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
    )
    require(abs(determinant - 1) < 1e-5, "R must be a proper rotation")
    identifier(transform["coordinate_revision"])
    require(bool(transform.get("scale_evidence")), "scale_evidence is required")


def reconstruction_input(path, transform_override=None, frame_override=None, scale_override=None):
    path = workspace_path(path)
    record = load_json(path)
    require(
        record["schema_version"] in ("reconstruction.bundle/1.0", "blender.source/1.0"), "unsupported source version"
    )
    if record["schema_version"] == "reconstruction.bundle/1.0":
        from tools.reconstruction.pipeline import schema_check

        schema_check("bundle", record)
    source = record["source"]
    if "input_batches" in source:
        batches = source["input_batches"]
        keys = [(entry["source_id"], entry["batch_id"]) for entry in batches]
        require(len(keys) == len(set(keys)), "duplicate source/batch identity")
        source["input_batch_ids"] = [
            "batch-" + content_hash([entry["source_id"], entry["batch_id"]]) for entry in batches
        ]
    require(
        source.get("input_batch_ids") and len(source["input_batch_ids"]) == len(set(source["input_batch_ids"])),
        "source batch identities required",
    )
    for batch in source["input_batch_ids"]:
        identifier(batch)
    identifier(record["revision"])
    require(record.get("scale_status") in ("arbitrary", "estimated", "verified"), "invalid scale_status")
    files = {}
    for entry in record["files"]:
        uri = entry["uri"]
        require(uri not in files, f"duplicate file {uri}")
        source = safe_file(path.parent, uri)
        require(
            source.stat().st_size == entry["bytes"] and digest(source) == entry["sha256"],
            f"source integrity failure: {uri}",
        )
        files[uri] = entry
    mesh_uri = record["mesh_file"]
    require(mesh_uri in files, "mesh_file must be indexed")
    mesh = safe_file(path.parent, mesh_uri)
    require(mesh.suffix.lower() in (".obj", ".ply", ".gltf", ".glb"), "supported mesh formats: OBJ, PLY, glTF, GLB")

    def dependency(base, uri):
        source = safe_file(base, uri)
        require(source.relative_to(path.parent).as_posix() in files, f"unindexed source dependency: {uri}")
        return source

    if mesh.suffix.lower() == ".obj":
        for line in mesh.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if parts and parts[0] == "mtllib":
                require(len(parts) == 2, "OBJ mtllib must name one relative file")
                material = dependency(mesh.parent, parts[1])
                for material_line in material.read_text(encoding="utf-8").splitlines():
                    tokens = material_line.split()
                    if tokens and (
                        tokens[0].lower().startswith("map_")
                        or tokens[0].lower() in ("bump", "disp", "decal", "refl", "norm")
                    ):
                        require(len(tokens) == 2, "MTL texture must name one relative file without options")
                        dependency(material.parent, tokens[1])
    elif mesh.suffix.lower() in (".gltf", ".glb"):
        if mesh.suffix.lower() == ".glb":
            from tools.world_export.validate import glb_document

            document = glb_document(mesh)
        else:
            document = load_json(mesh)
        for entry in document.get("buffers", []) + document.get("images", []):
            if "uri" in entry and not entry["uri"].startswith("data:"):
                dependency(mesh.parent, entry["uri"])
    elif mesh.suffix.lower() == ".ply":
        with mesh.open("rb") as stream:
            for line in stream:
                require(len(line) < 65536, "oversized PLY header line")
                if line.strip() == b"end_header":
                    break
                require(
                    b"texturefile" not in line.lower(),
                    "PLY external TextureFile is unsupported; use indexed OBJ or embedded glTF",
                )
    transform = transform_override or record.get("transform_world_from_sfm")
    require(
        frame_override is None or transform_override is not None, "world frame override requires an explicit transform"
    )
    if frame_override is not None:
        record["world_frame_id"] = identifier(frame_override)
    if scale_override is not None:
        require(
            transform_override is not None and frame_override is not None,
            "scale override requires explicit transform and world frame",
        )
        record["scale_status"] = scale_override
    if record["scale_status"] == "arbitrary" and transform is None:
        frame = "sfm-" + record["revision"]
        record["world_frame_id"] = frame
        transform = {
            "s": 1,
            "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "t": [0, 0, 0],
            "coordinate_revision": frame,
            "scale_evidence": "Unaligned arbitrary SfM coordinates; release prohibited",
        }
    validate_transform(transform)
    require(record.get("world_frame_id"), "world_frame_id must be explicitly established")
    identifier(record["world_frame_id"])
    return {"record": record, "manifest_sha256": digest(path), "mesh": str(mesh), "transform": transform}


def blender_environment():
    environment = os.environ.copy()
    base = WORKSPACE / "local/cache/blender"
    for key, folder in [
        ("BLENDER_USER_CONFIG", "config"),
        ("BLENDER_USER_SCRIPTS", "scripts"),
        ("BLENDER_USER_DATAFILES", "datafiles"),
        ("BLENDER_USER_EXTENSIONS", "extensions"),
    ]:
        directory = base / folder
        directory.mkdir(parents=True, exist_ok=True)
        environment[key] = str(directory)
    temporary = WORKSPACE / "local/tmp/blender"
    temporary.mkdir(parents=True, exist_ok=True)
    environment.update(TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary), PYTHONDONTWRITEBYTECODE="1")
    return environment


def run_blender(binary, arguments, master=None):
    command = [str(binary), "--background", "--factory-startup", "--disable-autoexec"]
    if master:
        command.append(str(master))
    command += ["--python-exit-code", "1", "--python", str(Path(__file__).with_name("worker.py")), "--", *arguments]
    from tools.reconstruction.toolchain import terminate_tree

    directory = workspace_path(WORKSPACE / "local/tmp/blender")
    log = directory / f"blender-{os.getpid()}.log"
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with log.open("ab") as stream:
        process = subprocess.Popen(
            command,
            env=blender_environment(),
            cwd=directory,
            stdout=stream,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **flags,
        )
        try:
            status = process.wait(timeout=3600)
            require(status == 0, f"Blender exited {status}; see {log}")
        except BaseException:
            if process.poll() is None:
                terminate_tree(process)
            raise
    print(f"Blender log: {log}")


def profile_lods(settings):
    return settings.get(
        "lods", [{"decimate_ratio": settings.get("decimate_ratio", 1), "min_distance_m": 0, "hysteresis_m": 0}]
    )


def profile_filename(profile, index):
    return f"{profile}.glb" if index == 0 else f"{profile}-lod{index}.glb"
