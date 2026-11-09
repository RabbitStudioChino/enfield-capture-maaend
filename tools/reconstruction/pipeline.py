"""Single-region, checkpointed native COLMAP/OpenMVS production jobs."""

import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from jsonschema import Draft202012Validator

from tools.capture.validate import BundleValidationError, load_json, read_jsonl, require, safe_path

from .geometry import validate_obj, validate_ply
from .model import CAMERA_PARAMETERS, apply, camera_records, quality_statistics, read_model, validate_transform
from .prepare import (
    check_path,
    check_tree,
    copy_image,
    digest,
    encoded,
    identifier,
    output_path,
    verify,
    within,
    write_bytes,
)
from .toolchain import atomic_json, command, cpu_option, execute, file_hash, fingerprint, load_toolchain

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts/reconstruction"
STAGES = (
    "verify",
    "preprocess",
    "features",
    "match",
    "map",
    "undistort",
    "import",
    "densify",
    "mesh",
    "refine",
    "texture",
    "quality",
    "bundle",
)
VERSION = "1.0.0"


def schema_check(kind, value):
    schema = load_json((CONTRACTS / (kind + ".schema.json")).read_bytes(), kind)
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    require(error is None, f"invalid {kind}: {error.message if error else ''}")


def inventory(root):
    check_path(root)
    paths = []
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if name != ".runtime"]
        for name in folders + files:
            check_path(Path(directory) / name)
        paths.extend(Path(directory) / name for name in files)
    return [
        {"uri": path.relative_to(root).as_posix(), "sha256": file_hash(path), "bytes": path.stat().st_size}
        for path in sorted(paths)
    ]


def run_root(workspace, run_id, create=False):
    workspace, _ = output_path(workspace, run_id)
    root = within(workspace, workspace / "local/reconstruction-runs" / run_id)
    if create:
        root.parent.mkdir(parents=True, exist_ok=True)
    return workspace, root


@contextmanager
def exclusive(root):
    lock = check_path(root.with_name("." + root.name + ".lock"))
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise BundleValidationError(
            f"run locked at {lock}; an interrupted owner must be confirmed stopped before removing the lock"
        ) from error
    try:
        write_bytes(lock / "owner.json", encoded({"pid": os.getpid()}))
        yield
    finally:
        (lock / "owner.json").unlink(missing_ok=True)
        lock.rmdir()


def read_config(path, preparation, prepared):
    config = load_json(check_path(Path(path)).read_bytes(), "reconstruction config")
    schema_check("config", config)
    validate_transform(config["transform_world_from_sfm"])
    require(
        (config["scale_status"] == "arbitrary") == (config["transform_world_from_sfm"] is None),
        "arbitrary scale requires no canonical-world transform",
    )
    require(
        (config["world_frame_id"] is None) == (config["transform_world_from_sfm"] is None),
        "world frame requires an explicit transform",
    )
    groups = load_json((prepared / "calibration-groups.json").read_bytes(), "prepared groups")["groups"]
    actual = {group["camera_group_id"]: group for group in groups}
    mapping = config["camera_group_mapping"]
    require(set(mapping) == set(actual), "camera_group_mapping must cover every prepared calibration group exactly")
    dimensions = {}
    for group_id, target in mapping.items():
        calibration = actual[group_id]["calibration"]
        geometry = (
            calibration["image_width"],
            calibration["image_height"],
            calibration["camera_mode"],
            calibration["fov_degrees"],
        )
        require(
            target not in dimensions or dimensions[target] == geometry,
            "cannot share intrinsics across incompatible calibrations",
        )
        dimensions[target] = geometry
    if config.get("camera_params"):
        require(len(set(dimensions.values())) == 1, "common explicit camera_params require matching group dimensions")
        params = config["camera_params"]
        require(
            len(params) == CAMERA_PARAMETERS[config["camera_model"]], "camera_params count does not match camera_model"
        )
        require(params[0] > 0, "focal length must be positive")
        if config["camera_model"] in (
            "PINHOLE",
            "OPENCV",
            "OPENCV_FISHEYE",
            "FULL_OPENCV",
            "FOV",
            "THIN_PRISM_FISHEYE",
        ):
            require(params[1] > 0, "vertical focal length must be positive")
    require(preparation["image_count"] >= 2, "reconstruction requires at least two eligible images")
    return config


def plan(workspace, job, run_id, config_path, toolchain_path):
    workspace, root = run_root(workspace, run_id, create=True)
    preparation = verify(workspace, job)
    _, prepared = output_path(workspace, job)
    config = read_config(within(workspace, Path(config_path)), preparation, prepared)
    toolchain = load_toolchain(within(workspace, Path(toolchain_path)))
    contract_hash = identifier(*[(path.name, file_hash(path)) for path in sorted(CONTRACTS.glob("*.schema.json"))])
    code_hash = identifier(*[(path.name, file_hash(path)) for path in sorted(Path(__file__).parent.glob("*.py"))])
    key = identifier(
        VERSION, file_hash(prepared / "preparation.json"), config, fingerprint(toolchain), contract_hash, code_hash
    )
    record = {
        "schema_version": "reconstruction.plan/1.0",
        "run_id": run_id,
        "job": job,
        "preparation_sha256": file_hash(prepared / "preparation.json"),
        "build_key": key,
        "generator_version": VERSION,
        "code_sha256": code_hash,
        "contracts_sha256": contract_hash,
        "config": config,
        "config_sha256": digest(encoded(config)),
        "toolchain": toolchain,
        "toolchain_sha256": digest(encoded(fingerprint(toolchain))),
        "stages": list(STAGES),
        "blockers": [name for name, value in toolchain["tools"].items() if value["status"] != "available"],
    }
    with exclusive(root):
        root.mkdir(exist_ok=False)
        atomic_json(root / "plan.json", record)
        atomic_json(root / "status.json", {"status": "queued", "build_key": key, "stage": None})
    return record


def read_plan(workspace, run_id):
    workspace, root = run_root(workspace, run_id)
    record = load_json(check_path(root / "plan.json").read_bytes(), "run plan")
    require(
        record.get("schema_version") == "reconstruction.plan/1.0"
        and record.get("run_id") == run_id
        and record.get("stages") == list(STAGES),
        "unsupported run plan",
    )
    preparation = verify(workspace, record["job"])
    _, prepared = output_path(workspace, record["job"])
    require(
        file_hash(prepared / "preparation.json") == record["preparation_sha256"],
        "prepared input changed; create a new plan",
    )
    require(
        digest(encoded(record["config"])) == record["config_sha256"]
        and digest(encoded(fingerprint(record["toolchain"]))) == record["toolchain_sha256"],
        "plan config/toolchain hash mismatch",
    )
    code_hash = identifier(*[(path.name, file_hash(path)) for path in sorted(Path(__file__).parent.glob("*.py"))])
    contract_hash = identifier(*[(path.name, file_hash(path)) for path in sorted(CONTRACTS.glob("*.schema.json"))])
    expected = identifier(
        VERSION,
        record["preparation_sha256"],
        record["config"],
        fingerprint(record["toolchain"]),
        contract_hash,
        code_hash,
    )
    require(record["build_key"] == expected, "build inputs or generator changed; create a new plan")
    schema_check("config", record["config"])
    validate_transform(record["config"]["transform_world_from_sfm"])
    return workspace, root, prepared, preparation, record


def status(workspace, run_id):
    _, root = run_root(workspace, run_id)
    result = load_json(check_path(root / "status.json").read_bytes(), "run status")
    result["cancellation_requested"] = check_path(root / "cancel.requested").exists()
    return result


def cancel(workspace, run_id):
    _, root = run_root(workspace, run_id)
    require((root / "plan.json").is_file(), "run does not exist")
    (root / "cancel.requested").touch(exist_ok=True)
    return {"status": "cancellation_requested", "run_id": run_id}


def images_for(prepared, config):
    records = read_jsonl((prepared / "frames.jsonl").read_bytes(), "prepared frames")
    return [
        {
            "name": config["camera_group_mapping"][record["camera_group_id"]] + "/" + Path(record["image_uri"]).name,
            "record": record,
        }
        for record in records
        if record["image_uri"] is not None
    ]


def database_check(path, names, mapping=None, matched=False):
    require(path.is_file(), "missing COLMAP database")
    with sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True) as database:
        require(database.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "corrupt COLMAP database")
        images = database.execute("SELECT image_id,name,camera_id FROM images").fetchall()
        require({row[1] for row in images} == names and len(images) == len(names), "COLMAP image list mismatch")
        cameras = {}
        for image_id, name, camera_id in images:
            camera = database.execute("SELECT width,height FROM cameras WHERE camera_id=?", (camera_id,)).fetchone()
            require(
                camera and (mapping is None or tuple(camera) == mapping[name]),
                "decoded image dimensions differ from prepared calibration",
            )
            row = database.execute(
                "SELECT rows,cols,length(data) FROM keypoints WHERE image_id=?", (image_id,)
            ).fetchone()
            require(
                row and row[0] > 0 and row[1] in (2, 4, 6) and row[2] == row[0] * row[1] * 4,
                "missing or malformed features",
            )
            descriptor = database.execute(
                "SELECT rows,cols,length(data) FROM descriptors WHERE image_id=?", (image_id,)
            ).fetchone()
            require(
                descriptor and descriptor[0] == row[0] and descriptor[2] == descriptor[0] * descriptor[1],
                "missing descriptors",
            )
            cameras.setdefault(name.split("/")[0], set()).add(camera_id)
        require(
            all(len(ids) == 1 for ids in cameras.values())
            and len({next(iter(ids)) for ids in cameras.values()}) == len(cameras),
            "camera groups were not preserved by COLMAP",
        )
        if matched:
            require(
                database.execute("SELECT count(*) FROM two_view_geometries WHERE rows > 0").fetchone()[0] > 0,
                "no geometrically verified image pair",
            )


def choose_model(root):
    candidates = []
    for directory in sorted(root.iterdir()):
        if directory.is_dir() and (directory / "cameras.txt").is_file():
            model = read_model(directory)
            candidates.append((len(model["images"]), directory.name, model))
    require(bool(candidates), "mapper did not produce a nonempty parseable model")
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1], candidates[0][2], [item[0] for item in candidates]


def run(workspace, run_id, resume=False):
    workspace, root = run_root(workspace, run_id)
    with exclusive(root):
        workspace, root, prepared, preparation, record = read_plan(workspace, run_id)
        require(resume or status(workspace, run_id)["status"] == "queued", "run already started; use resume")
        require(not record["blockers"], "missing toolchain capabilities: " + ", ".join(record["blockers"]))
        # Recheck the executable bytes rather than trusting a saved doctor's availability flag.
        for name, tool in record["toolchain"]["tools"].items():
            require(file_hash(Path(tool["path"])) == tool["sha256"], f"installed tool changed: {name}")
        if resume:
            (root / "cancel.requested").unlink(missing_ok=True)
        runner = Runner(workspace, root, prepared, preparation, record)
        return runner.run()


class Runner:
    def __init__(self, workspace, root, prepared, preparation, record):
        self.workspace, self.root, self.prepared, self.preparation, self.record = (
            workspace,
            root,
            prepared,
            preparation,
            record,
        )
        self.config, self.toolchain = record["config"], record["toolchain"]
        self.images = images_for(prepared, self.config)
        self.names = {image["name"] for image in self.images}
        self.dimensions = {
            image["name"]: (image["record"]["frame"]["image"]["width"], image["record"]["frame"]["image"]["height"])
            for image in self.images
        }
        self.cancel = root / "cancel.requested"
        self.active = None

    def directory(self, stage):
        return self.root / "stages" / stage

    def colmap(self, operation, options, directory):
        evidence = self.toolchain["tools"]["colmap"]["help"][operation]["text"]
        seed_option = "--default_random_seed" if "--default_random_seed" in evidence else "--random_seed"
        options = [*options, seed_option, self.config["random_seed"]]
        if operation == "mapper":
            options.extend(["--Mapper.random_seed", self.config["random_seed"]])
        self.process(command(self.toolchain, "colmap", operation, options), directory)

    def process(self, argv, directory):
        self.require_active()
        execute(
            argv,
            directory,
            self.root / "logs" / (self.active + ".log"),
            self.config["stage_timeout_seconds"],
            self.cancel,
        )

    def require_active(self):
        if self.cancel.exists():
            raise InterruptedError("cancellation requested")

    def mvs(self, tool, source, target, directory, extra=()):
        options = [
            "--input-file",
            source,
            "--output-file",
            target,
            "--working-folder",
            directory,
            "--archive-type",
            -1,
            *extra,
        ]
        self.process(command(self.toolchain, tool, tool, options), directory)

    def validate_mvs(self, directory, name):
        source = directory / name
        require(source.is_file() and source.stat().st_size > 12, "missing OpenMVS scene")
        validation = directory / "validation"
        validation.mkdir(exist_ok=True)
        self.mvs("InterfaceCOLMAP", source, validation, directory, ["--binary", 0])
        model = read_model(validation / "sparse")
        expected_root = self.directory("undistort") / "images"
        actual_names = set()
        for image in model["images"].values():
            resolved = (validation / image["name"]).resolve()
            require(
                resolved.is_relative_to(expected_root), "OpenMVS image escapes the validated undistorted image tree"
            )
            image_name = resolved.relative_to(expected_root).as_posix()
            require(
                image_name in self.names and resolved == safe_path(expected_root, image_name),
                "OpenMVS references an unknown image",
            )
            actual_names.add(image_name)
        selected_names = {image["name"] for image in read_model(self.selected_model())["images"].values()}
        require(actual_names == selected_names, "OpenMVS changed the registered image set")
        return model

    def run(self):
        previous = self.record["build_key"]
        reuse = True
        try:
            for stage in STAGES:
                self.active = stage
                self.require_active()
                directory = self.directory(stage)
                checkpoint = self.root / "checkpoints" / (stage + ".json")
                key = identifier(previous, stage)
                if reuse and checkpoint.is_file() and directory.is_dir():
                    try:
                        saved = load_json(checkpoint.read_bytes(), stage + " checkpoint")
                    except BundleValidationError:
                        saved = {}
                    if (
                        isinstance(saved, dict)
                        and saved.get("status") == "succeeded"
                        and saved.get("key") == key
                        and saved.get("files") == inventory(directory)
                    ):
                        previous = identifier(key, saved["files"])
                        continue
                reuse = False
                if directory.exists():
                    history = self.root / "attempts"
                    history.mkdir(exist_ok=True)
                    directory.rename(history / (stage + "-" + uuid.uuid4().hex))
                directory.mkdir(parents=True)
                atomic_json(
                    self.root / "status.json",
                    {"status": "running", "build_key": self.record["build_key"], "stage": stage},
                )
                getattr(self, "stage_" + stage)(directory)
                self.require_active()
                files = inventory(directory)
                require(bool(files), f"stage {stage} produced no evidence")
                checkpoint.parent.mkdir(exist_ok=True)
                atomic_json(checkpoint, {"status": "succeeded", "key": key, "files": files})
                previous = identifier(key, files)
            result = {
                "status": "needs_review",
                "build_key": self.record["build_key"],
                "stage": "bundle",
                "bundle": str(self.directory("bundle")),
                "geometry_acceptance": "not_assessed",
            }
            atomic_json(self.root / "status.json", result)
            return result
        except (Exception, KeyboardInterrupt) as error:
            result = {
                "status": "cancelled" if isinstance(error, (InterruptedError, KeyboardInterrupt)) else "failed",
                "build_key": self.record["build_key"],
                "stage": self.active,
                "error": str(error) or type(error).__name__,
            }
            atomic_json(self.root / "status.json", result)
            raise

    def stage_verify(self, directory):
        verify(self.workspace, self.record["job"])
        write_bytes(
            directory / "verified.json",
            encoded({"preparation_sha256": self.record["preparation_sha256"], "image_count": len(self.images)}),
        )

    def stage_preprocess(self, directory):
        manifest = []
        for image in self.images:
            self.require_active()
            item = image["record"]
            target = directory / "images" / image["name"]
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_image(safe_path(self.prepared, item["image_uri"]), target, item["frame"]["image"]["sha256"])
            manifest.append(
                {
                    "image_name": image["name"],
                    "frame_id": item["frame"]["frame_id"],
                    "session_id": item["frame"]["session_id"],
                    "A_processed_from_raw": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    "processing": "identity",
                    "mask": "none",
                    "image_decode": "deferred_to_colmap_feature_extractor",
                }
            )
        write_bytes(directory / "images.json", encoded(manifest))

    def stage_features(self, directory):
        options = [
            "--database_path",
            directory / "database.db",
            "--image_path",
            self.directory("preprocess") / "images",
            "--ImageReader.single_camera_per_folder",
            1,
            "--ImageReader.camera_model",
            self.config["camera_model"],
            cpu_option(self.toolchain, "feature_extractor"),
            0,
        ]
        if self.config.get("camera_params"):
            options.extend(
                ["--ImageReader.camera_params", ",".join(str(value) for value in self.config["camera_params"])]
            )
        self.colmap("feature_extractor", options, directory)
        database_check(directory / "database.db", self.names, self.dimensions)

    def stage_match(self, directory):
        shutil.copyfile(self.directory("features") / "database.db", directory / "database.db")
        self.colmap(
            "exhaustive_matcher",
            ["--database_path", directory / "database.db", cpu_option(self.toolchain, "exhaustive_matcher"), 0],
            directory,
        )
        database_check(directory / "database.db", self.names, self.dimensions, matched=True)

    def stage_map(self, directory):
        models = directory / "models"
        models.mkdir()
        shutil.copyfile(self.directory("match") / "database.db", directory / "database.db")
        self.colmap(
            "mapper",
            [
                "--database_path",
                directory / "database.db",
                "--image_path",
                self.directory("preprocess") / "images",
                "--output_path",
                models,
            ],
            directory,
        )
        for model in sorted(models.iterdir()):
            if model.is_dir() and (model / "cameras.bin").is_file():
                self.colmap(
                    "model_converter",
                    ["--input_path", model, "--output_path", model, "--output_type", "TXT"],
                    directory,
                )
        name, model, sizes = choose_model(models)
        require(
            {image["name"] for image in model["images"].values()}.issubset(self.names),
            "mapper references an unknown image",
        )
        write_bytes(
            directory / "selection.json",
            encoded(
                {
                    "model": name,
                    "component_registered_images": sizes,
                    "policy": "largest_registered_component_then_name",
                }
            ),
        )

    def selected_model(self):
        selected = load_json((self.directory("map") / "selection.json").read_bytes(), "model selection")
        return self.directory("map") / "models" / selected["model"]

    def stage_undistort(self, directory):
        self.colmap(
            "image_undistorter",
            [
                "--image_path",
                self.directory("preprocess") / "images",
                "--input_path",
                self.selected_model(),
                "--output_path",
                directory,
                "--output_type",
                "COLMAP",
                "--max_image_size",
                self.config["max_image_size"],
            ],
            directory,
        )
        self.colmap(
            "model_converter",
            ["--input_path", directory / "sparse", "--output_path", directory / "sparse", "--output_type", "TXT"],
            directory,
        )
        model = read_model(directory / "sparse")
        for camera in model["cameras"].values():
            require(camera["model"] in ("PINHOLE", "SIMPLE_PINHOLE"), "OpenMVS requires undistorted pinhole cameras")
        for image in model["images"].values():
            safe_path(directory / "images", image["name"])

    def stage_import(self, directory):
        self.mvs("InterfaceCOLMAP", self.directory("undistort"), directory / "scene.mvs", directory)
        self.validate_mvs(directory, "scene.mvs")

    def stage_densify(self, directory):
        self.mvs("DensifyPointCloud", self.directory("import") / "scene.mvs", directory / "dense.mvs", directory)
        self.validate_mvs(directory, "dense.mvs")
        validate_ply(directory / "dense.ply")

    def stage_mesh(self, directory):
        self.mvs("ReconstructMesh", self.directory("densify") / "dense.mvs", directory / "mesh.mvs", directory)
        validate_ply(directory / "mesh.ply", mesh=True)

    def stage_refine(self, directory):
        self.mvs(
            "RefineMesh",
            self.directory("densify") / "dense.mvs",
            directory / "refined.mvs",
            directory,
            ["--mesh-file", self.directory("mesh") / "mesh.ply"],
        )
        validate_ply(directory / "refined.ply", mesh=True)

    def stage_texture(self, directory):
        extra = ["--mesh-file", self.directory("refine") / "refined.ply", "--export-type", "obj"]
        if "--export-texture-lossless" in self.toolchain["tools"]["TextureMesh"]["help"]["TextureMesh"]["text"]:
            extra.extend(["--export-texture-lossless", 1])
        self.mvs("TextureMesh", self.directory("densify") / "dense.mvs", directory / "model.mvs", directory, extra)
        validate_obj(directory / "model.obj")

    def stage_quality(self, directory):
        model = read_model(self.selected_model())
        quality = quality_statistics(model, len(self.images))
        quality["mapper_components"] = load_json((self.directory("map") / "selection.json").read_bytes(), "selection")[
            "component_registered_images"
        ]
        quality["mask_propagation"] = "not_validated"
        quality["scale_status"] = self.config["scale_status"]
        quality["textured_mesh"] = validate_obj(self.directory("texture") / "model.obj")["counts"]
        quality["dense_point_count"] = validate_ply(self.directory("densify") / "dense.ply")["vertex"]
        quality["acceptance"] = dict.fromkeys(
            (
                "held_out_views",
                "geometry_holes",
                "dynamic_content_leakage",
                "walkable_surfaces",
                "game_reachability",
                "metric_scale",
            ),
            "not_assessed",
        )
        write_bytes(directory / "quality.json", encoded(quality))
        cameras = camera_records(model, self.config["transform_world_from_sfm"])
        originals = {image["name"]: image["record"]["frame"] for image in self.images}
        for image in cameras["images"]:
            original = originals[image["image_name"]]
            image.update(frame_id=original["frame_id"], session_id=original["session_id"])
        write_bytes(directory / "cameras.json", encoded(cameras))

    def stage_bundle(self, directory):
        assets = directory / "assets"
        assets.mkdir()
        mesh = self.directory("texture") / "model.obj"
        references = validate_obj(mesh)["references"]
        for source in [mesh, *references]:
            require(source.parent == mesh.parent, "nested texture references are unsupported")
            shutil.copyfile(source, assets / source.name)
        for name in ("quality.json", "cameras.json"):
            shutil.copyfile(self.directory("quality") / name, directory / name)
        write_bytes(
            directory / "provenance.json",
            encoded({"config": self.config, "toolchain": fingerprint(self.toolchain), "preparation": self.preparation}),
        )
        source_batches = []
        for source in self.preparation["sources"]:
            bundle = load_json(safe_path(self.prepared, source["bundle_uri"]).read_bytes(), "source bundle")
            for uri in bundle["batch_uris"]:
                prefix = str(Path(source["bundle_uri"]).parent)
                batch = load_json(safe_path(self.prepared, prefix + "/" + uri).read_bytes(), "source batch")
                source_batches.append(
                    {
                        "source_id": source["source_id"],
                        "batch_id": batch["batch_id"],
                        "sha256": file_hash(safe_path(self.prepared, prefix + "/" + uri)),
                    }
                )
        files = inventory(directory)
        manifest = {
            "schema_version": "reconstruction.bundle/1.0",
            "revision": identifier(self.record["build_key"], files),
            "parent_revision": self.config["parent_revision"],
            "region_id": self.preparation["compatibility"]["region_id"],
            "world_frame_id": self.config["world_frame_id"],
            "coordinate_system": "sfm",
            "scale_status": self.config["scale_status"],
            "transform_world_from_sfm": self.config["transform_world_from_sfm"],
            "camera_file": "cameras.json",
            "mesh_file": "assets/model.obj",
            "quality_report": "quality.json",
            "source": {
                "preparation_sha256": self.record["preparation_sha256"],
                "input_batches": source_batches,
                "source_bundles": self.preparation["sources"],
            },
            "build": {
                "key": self.record["build_key"],
                "toolchain_sha256": self.record["toolchain_sha256"],
                "config_sha256": self.record["config_sha256"],
            },
            "status": "needs_review",
            "files": files,
        }
        schema_check("bundle", manifest)
        write_bytes(directory / "reconstruction.json", encoded(manifest))
        verify_bundle(directory)


def verify_bundle(root):
    root = check_path(Path(root))
    check_tree(root)
    manifest = load_json(safe_path(root, "reconstruction.json").read_bytes(), "reconstruction bundle")
    schema_check("bundle", manifest)
    validate_transform(manifest["transform_world_from_sfm"])
    require(
        (manifest["scale_status"] == "arbitrary") == (manifest["transform_world_from_sfm"] is None),
        "scale/transform mismatch",
    )
    require(
        (manifest["world_frame_id"] is None) == (manifest["transform_world_from_sfm"] is None),
        "world frame/transform mismatch",
    )
    actual = [entry for entry in inventory(root) if entry["uri"] != "reconstruction.json"]
    require(manifest["files"] == actual, "reconstruction bundle inventory/hash mismatch")
    for key in ("camera_file", "mesh_file", "quality_report"):
        safe_path(root, manifest[key])
    validate_obj(safe_path(root, manifest["mesh_file"]))
    quality = load_json(safe_path(root, manifest["quality_report"]).read_bytes(), "quality")
    require(quality.get("status") == "needs_review", "quality requires independent human acceptance")
    cameras = load_json(safe_path(root, manifest["camera_file"]).read_bytes(), "cameras")
    require(
        cameras.get("coordinate_system") == ("world" if manifest["transform_world_from_sfm"] else "sfm"),
        "camera coordinate frame mismatch",
    )
    require(
        isinstance(cameras.get("images"), list) and len(cameras["images"]) == quality.get("registered_images") > 0,
        "camera/quality image count mismatch",
    )
    names = set()
    for image in cameras["images"]:
        require(
            image["image_name"] not in names and image.get("frame_id") and image.get("session_id"),
            "camera source identity is missing or duplicated",
        )
        names.add(image["image_name"])
        validate_transform(
            {
                "s": 1,
                "R": image["R_frame_from_web_camera"],
                "t": image["position"],
                "coordinate_revision": "camera-basis",
                "scale_evidence": "unit-rotation",
            }
        )
    provenance = load_json(safe_path(root, "provenance.json").read_bytes(), "provenance")
    schema_check("config", provenance["config"])
    require(
        digest(encoded(provenance["config"])) == manifest["build"]["config_sha256"]
        and digest(encoded(provenance["toolchain"])) == manifest["build"]["toolchain_sha256"],
        "portable provenance hash mismatch",
    )
    require(
        identifier(manifest["build"]["key"], manifest["files"]) == manifest["revision"], "content revision mismatch"
    )
    require(
        provenance["config"]["transform_world_from_sfm"] == manifest["transform_world_from_sfm"]
        and provenance["config"]["scale_status"] == manifest["scale_status"]
        and provenance["config"]["world_frame_id"] == manifest["world_frame_id"],
        "portable coordinate provenance mismatch",
    )
    require(
        digest(encoded(provenance["preparation"])) == manifest["source"]["preparation_sha256"]
        and provenance["preparation"]["sources"] == manifest["source"]["source_bundles"],
        "portable source preparation mismatch",
    )
    source_ids = {source["source_id"] for source in manifest["source"]["source_bundles"]}
    batches = manifest["source"]["input_batches"]
    require(
        all(batch["source_id"] in source_ids for batch in batches)
        and len({(batch["source_id"], batch["batch_id"]) for batch in batches}) == len(batches),
        "invalid or duplicate source batch identity",
    )
    return manifest


def feedback(workspace, run_id, request_id):
    _, root = run_root(workspace, run_id)
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", request_id), "invalid request ID")
    target = root / "feedback" / request_id
    with exclusive(root):
        read_plan(workspace, run_id)
        require(status(workspace, run_id)["status"] == "needs_review", "feedback requires a completed reconstruction")
        bundle = root / "stages/bundle"
        manifest = verify_bundle(bundle)
        cameras = load_json((bundle / "cameras.json").read_bytes(), "cameras")
        target.mkdir(parents=True, exist_ok=False)
        preview = {
            "schema_version": "reconstruction.preview/1.0",
            "preview_revision": identifier(manifest["revision"], request_id),
            "source_revision": manifest["revision"],
            "region_id": manifest["region_id"],
            "world_frame_id": manifest["world_frame_id"],
            "source_bundle_sha256": file_hash(bundle / "reconstruction.json"),
            "status": "needs_review",
            "coordinate_system": "world" if manifest["transform_world_from_sfm"] else "sfm",
            "input_batches": manifest["source"]["input_batches"],
            "camera_file": "cameras.json",
            "geometry_file": "sparse.ply",
            "diagnostics_file": "quality.json",
        }
        for name in ("cameras.json", "quality.json"):
            shutil.copyfile(bundle / name, target / name)
        model_dir = (
            root
            / "stages/map/models"
            / load_json((root / "stages/map/selection.json").read_bytes(), "selection")["model"]
        )
        model = read_model(model_dir)
        cloud = [
            "ply",
            "format ascii 1.0",
            f"element vertex {len(model['points'])}",
            "property float x",
            "property float y",
            "property float z",
            "end_header",
        ]
        transform = manifest["transform_world_from_sfm"]
        for point in model["points"].values():
            xyz = point["xyz"]
            if transform:
                xyz = [
                    transform["s"] * value + offset for value, offset in zip(apply(transform["R"], xyz), transform["t"])
                ]
            cloud.append(" ".join(str(value) for value in xyz))
        write_bytes(target / "sparse.ply", ("\n".join(cloud) + "\n").encode("ascii"))
        request = {
            "schema_version": "reconstruction.rescan/1.0",
            "request_id": request_id,
            "source_revision": manifest["revision"],
            "region_id": manifest["region_id"],
            "reference_frame_ids": [
                {"session_id": item["session_id"], "frame_id": item["frame_id"]} for item in cameras["images"]
            ],
            "mapping_revision": None,
            "status": "needs_mapping",
            "problem": "Review the quality report and choose deficient views manually",
            "recommended_views": [],
            "automatic_navigation": False,
        }
        schema_check("preview", preview)
        schema_check("rescan", request)
        write_bytes(target / "preview.json", encoded(preview))
        write_bytes(target / "rescan.json", encoded(request))
    return {"status": "needs_mapping", "path": str(target), "source_revision": manifest["revision"]}
