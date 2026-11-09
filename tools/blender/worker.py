"""Executed by Blender only. Never enable Python embedded in an input master."""

# ruff: noqa: E402

import sys

# Blender isolates Python and ignores PYTHONDONTWRITEBYTECODE.
sys.dont_write_bytecode = True

import hashlib
import json
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.blender.common import (
    VERSION,
    content_hash,
    decode_json,
    digest,
    load_json,
    profile_filename,
    profile_lods,
    require,
    write_json,
)

BASIS = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
METADATA = "MasterAssetSet.json"


def collection(name):
    result = bpy.data.collections.get(name)
    if result is None:
        result = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(result)
    return result


def material_signature(material):
    if material is None:
        return None
    result = {"color": list(material.diffuse_color), "use_nodes": material.use_nodes}
    if material.use_nodes:
        nodes = []
        for node in material.node_tree.nodes:
            properties = {}
            for prop in node.bl_rna.properties:
                if (
                    prop.identifier
                    in (
                        "rna_type",
                        "location",
                        "width",
                        "height",
                        "select",
                        "show_options",
                        "show_preview",
                        "show_texture",
                    )
                    or prop.is_readonly
                ):
                    continue
                value = getattr(node, prop.identifier)
                if isinstance(value, (str, int, float, bool)):
                    properties[prop.identifier] = value
            inputs = {}
            for socket in node.inputs:
                if hasattr(socket, "default_value"):
                    value = socket.default_value
                    inputs[socket.identifier] = value if isinstance(value, (str, int, float, bool)) else list(value)
            item = {"type": node.bl_idname, "name": node.name, "properties": properties, "inputs": inputs}
            if getattr(node, "image", None):
                image = node.image
                require(
                    image.packed_file is not None and not image.is_dirty,
                    "Source textures must remain packed and unedited",
                )
                item["image"] = hashlib.sha256(image.packed_file.data).hexdigest()
                item["colorspace"] = image.colorspace_settings.name
            nodes.append(item)
        result["nodes"] = nodes
        result["links"] = [
            (link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier)
            for link in material.node_tree.links
        ]
    return result


def fingerprint(obj):
    attributes = []
    for attribute in obj.data.attributes:
        if attribute.name.startswith((".select_", ".hide_")):
            continue
        values = []
        for item in attribute.data:
            fields = {}
            for prop in item.bl_rna.properties:
                if prop.identifier == "rna_type":
                    continue
                value = getattr(item, prop.identifier)
                if isinstance(value, (str, int, float, bool)):
                    fields[prop.identifier] = value
                elif prop.is_array:
                    fields[prop.identifier] = list(value)
            values.append(fields)
        attributes.append(
            {"name": attribute.name, "type": attribute.data_type, "domain": attribute.domain, "values": values}
        )
    return content_hash(
        {
            "location": list(obj.location),
            "rotation_mode": obj.rotation_mode,
            "rotation_euler": list(obj.rotation_euler),
            "rotation_quaternion": list(obj.rotation_quaternion),
            "scale": list(obj.scale),
            "vertices": [list(vertex.co) for vertex in obj.data.vertices],
            "polygons": [list(face.vertices) for face in obj.data.polygons],
            "attributes": attributes,
            "materials": [material_signature(slot.material) for slot in obj.material_slots],
        }
    )


def metadata():
    text = bpy.data.texts.get(METADATA)
    require(text is not None, "master has no MasterAssetSet.json")
    return decode_json(text.as_string())


def verify_sources(record):
    for source in record["sources"]:
        group = bpy.data.collections.get(source["collection"])
        require(group is not None, "Source collection was removed")
        require(
            all(obj.parent is None and not obj.modifiers and not obj.constraints for obj in group.all_objects),
            "Source hierarchy or modifiers were edited",
        )
        actual = {obj.name: fingerprint(obj) for obj in group.all_objects if obj.type == "MESH"}
        require(actual == source["objects"], "Source collection was edited; recover the original immutable source")


def import_revision(job, output):
    source = load_json(job)
    reconstruction, transform = source["record"], source["transform"]
    existing = bpy.data.texts.get(METADATA)
    if existing:
        record = metadata()
        verify_sources(record)
        require(
            record["coordinate_revision"] == transform["coordinate_revision"]
            and record["world_frame_id"] == reconstruction["world_frame_id"]
            and record["transform"] == transform,
            "coordinate change requires explicit manual migration to a new master",
        )
        previous = next((item for item in record["sources"] if item["revision"] == reconstruction["revision"]), None)
        if previous:
            require(
                previous["manifest_sha256"] == source["manifest_sha256"],
                "revision reused with different source manifest",
            )
            bpy.ops.wm.save_as_mainfile(filepath=output, check_existing=False)
            return
    else:
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        record = {
            "schema_version": "master.assets/1.0",
            "sources": [],
            "coordinate_revision": transform["coordinate_revision"],
            "world_frame_id": reconstruction["world_frame_id"],
            "transform": transform,
        }
    for name in ("Working", "ManualPatches", "Collision", "Anchors", "Export_mobile"):
        collection(name)
    mesh = Path(source["mesh"])
    before = set(bpy.data.objects)
    if mesh.suffix.lower() == ".obj":
        bpy.ops.wm.obj_import(filepath=str(mesh), forward_axis="Y", up_axis="Z")
    elif mesh.suffix.lower() == ".ply":
        bpy.ops.wm.ply_import(filepath=str(mesh), forward_axis="Y", up_axis="Z")
    else:
        bpy.ops.import_scene.gltf(filepath=str(mesh))
    imported = list(set(bpy.data.objects) - before)
    meshes = [obj for obj in imported if obj.type == "MESH"]
    require(meshes, "source has no mesh objects")
    raw_world = {obj: obj.matrix_world.copy() for obj in meshes}
    group = collection("Source_" + reconstruction["revision"])
    rotation = Matrix(transform["R"]).to_4x4()
    translation = Matrix.Translation(Vector(transform["t"]))
    scale = Matrix.Scale(transform["s"], 4)
    mapping = BASIS @ translation @ rotation @ scale
    if mesh.suffix.lower() in (".gltf", ".glb"):
        mapping = mapping @ BASIS.inverted()
    for obj in meshes:
        obj.parent = None
        obj.matrix_world = mapping @ raw_world[obj]
        for owner in list(obj.users_collection):
            owner.objects.unlink(obj)
        group.objects.link(obj)
        obj.hide_select = True
    for obj in imported:
        if obj.type != "MESH":
            bpy.data.objects.remove(obj, do_unlink=True)
    working = collection("Working")
    for obj in list(working.objects):
        if obj.get("generated_source"):
            bpy.data.objects.remove(obj, do_unlink=True)
    for obj in meshes:
        copied = obj.copy()
        copied.data = obj.data.copy()
        copied.name = "Working_" + obj.name
        copied.hide_select = False
        for slot in copied.material_slots:
            if slot.material:
                slot.material = slot.material.copy()
                if slot.material.use_nodes:
                    for node in slot.material.node_tree.nodes:
                        if getattr(node, "image", None):
                            node.image = node.image.copy()
        copied["generated_source"] = reconstruction["revision"]
        working.objects.link(copied)
    group.hide_render = True
    group.hide_viewport = True
    bpy.ops.file.pack_all()
    entry = {
        "revision": reconstruction["revision"],
        "collection": group.name,
        "manifest_sha256": source["manifest_sha256"],
        "files": reconstruction["files"],
        "objects": {obj.name: fingerprint(obj) for obj in meshes},
    }
    record["sources"].append(entry)
    record.update(
        active_reconstruction=reconstruction["revision"],
        source=reconstruction["source"],
        scale_status=reconstruction["scale_status"],
        blender_version=bpy.app.version_string,
        exporter_version=VERSION,
    )
    text = bpy.data.texts.get(METADATA) or bpy.data.texts.new(METADATA)
    text.clear()
    text.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
    bpy.context.scene.unit_settings.system = "METRIC"
    bpy.context.scene.unit_settings.scale_length = 1
    bpy.context.scene.view_settings.view_transform = "Standard"
    bpy.context.scene.view_settings.exposure = 0
    bpy.context.scene.view_settings.gamma = 1
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=output, check_existing=False)


def world_position(point):
    return list((BASIS.inverted() @ point)[:3])


def anchor(obj):
    world = BASIS.inverted() @ obj.matrix_world @ BASIS
    rotation = world.to_quaternion().normalized()
    return {"position": list(world.translation), "rotation_xyzw": [rotation.x, rotation.y, rotation.z, rotation.w]}


def evaluated_mesh(obj):
    return bpy.data.meshes.new_from_object(obj.evaluated_get(bpy.context.evaluated_depsgraph_get()))


def make_unlit(material):
    copied = material.copy()
    copied.use_nodes = True
    tree = copied.node_tree
    output = next((node for node in tree.nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output), None)
    require(output is not None, "material has no output")
    principled = next((node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    color_node = tree.nodes.new("ShaderNodeRGB")
    color_socket = color_node.outputs[0]
    if principled:
        color = principled.inputs["Base Color"]
        color_node.outputs[0].default_value = color.default_value
        if color.is_linked:
            color_socket = color.links[0].from_socket
    else:
        color_node.outputs[0].default_value = copied.diffuse_color
    alpha = principled.inputs["Alpha"] if principled else None
    if alpha and (alpha.is_linked or alpha.default_value < 1):
        transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
        mixed = tree.nodes.new("ShaderNodeMixShader")
        mixed.inputs[0].default_value = alpha.default_value
        if alpha.is_linked:
            tree.links.new(alpha.links[0].from_socket, mixed.inputs[0])
        tree.links.new(transparent.outputs[0], mixed.inputs[1])
        tree.links.new(color_socket, mixed.inputs[2])
        tree.links.new(mixed.outputs[0], output.inputs["Surface"])
    else:
        tree.links.new(color_socket, output.inputs["Surface"])
    return copied


def export_master(config_file, staging):
    config, staging = load_json(config_file), Path(staging)
    record = metadata()
    verify_sources(record)
    require(
        config["accepted_reconstruction_revision"] == record["active_reconstruction"],
        "export configuration must explicitly accept the active reconstruction revision",
    )
    require(record["scale_status"] in ("estimated", "verified"), "arbitrary scale cannot be released")
    bpy.context.view_layer.update()
    render_objects = [
        obj for name in ("Working", "ManualPatches") for obj in collection(name).all_objects if obj.type == "MESH"
    ]
    require(render_objects, "no Working or ManualPatches mesh")
    vertices, triangles, render_vertices = [], [], []
    for obj in collection("Collision").all_objects:
        require(obj.type == "MESH", "Collision collection accepts mesh proxies only")
        mesh = evaluated_mesh(obj)
        offset = len(vertices)
        vertices.extend(world_position(obj.matrix_world @ vertex.co) for vertex in mesh.vertices)
        mesh.calc_loop_triangles()
        triangles.extend([offset + index for index in face.vertices] for face in mesh.loop_triangles)
        bpy.data.meshes.remove(mesh)
    require(triangles, "Collision collection requires explicit proxy meshes")
    for obj in render_objects:
        mesh = evaluated_mesh(obj)
        render_vertices.extend(world_position(obj.matrix_world @ vertex.co) for vertex in mesh.vertices)
        bpy.data.meshes.remove(mesh)
    all_points = vertices + render_vertices
    bounds = {
        "min": [min(point[i] for point in all_points) - 1e-5 for i in range(3)],
        "max": [max(point[i] for point in all_points) + 1e-5 for i in range(3)],
    }
    anchors = {obj.name: anchor(obj) for obj in collection("Anchors").all_objects}
    require("Spawn" in anchors, "Anchors requires an explicit Spawn object")
    write_json(
        staging / "collision.json",
        {
            "schema_version": "world.collision/2.0",
            "coordinate_space": "chunk_local",
            "shape_type": "trimesh",
            "vertices": vertices,
            "triangles": triangles,
        },
    )
    for profile, settings in config["profiles"].items():
        for lod_index, lod in enumerate(profile_lods(settings)):
            group = collection("Export_" + profile)
            for obj in list(group.objects):
                bpy.data.objects.remove(obj, do_unlink=True)
            generated = []
            for obj in render_objects:
                mesh = evaluated_mesh(obj)
                mesh.transform(obj.matrix_world)
                copied = bpy.data.objects.new("Export_" + obj.name, mesh)
                group.objects.link(copied)
                if lod["decimate_ratio"] < 1:
                    modifier = copied.modifiers.new("Profile reduction", "DECIMATE")
                    modifier.ratio = lod["decimate_ratio"]
                if config["unlit"]:
                    if not copied.data.materials:
                        default = bpy.data.materials.new("Default unlit base")
                        default.use_nodes = True
                        copied.data.materials.append(default)
                    for slot in copied.material_slots:
                        if slot.material:
                            slot.material = make_unlit(slot.material)
                generated.append(copied)
            bpy.ops.object.select_all(action="DESELECT")
            for obj in generated:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = generated[0]
            bpy.ops.export_scene.gltf(
                filepath=str(staging / profile_filename(profile, lod_index)),
                export_format="GLB",
                use_selection=True,
                export_yup=True,
                export_apply=True,
                export_animations=False,
                export_cameras=False,
                export_lights=False,
                export_extras=False,
                export_image_format="AUTO",
                export_materials="EXPORT",
                export_draco_mesh_compression_enable=False,
                export_use_gltfpack=False,
                export_meshopt_compression_enable=False,
                export_use_ktx_compression=False,
            )
            for obj in generated:
                bpy.data.objects.remove(obj, do_unlink=True)
    patches = [
        {"object": obj.name, "sha256": fingerprint(obj), "coordinate_revision": record["coordinate_revision"]}
        for obj in collection("ManualPatches").all_objects
        if obj.type == "MESH"
    ]
    record.update(
        manual_patch_revisions=patches,
        collections=[group.name for group in bpy.data.collections],
        export_config=config,
        blender_version=bpy.app.version_string,
        exported_asset_sha256={
            path.name: digest(path)
            for path in staging.iterdir()
            if path.suffix == ".glb" or path.name == "collision.json"
        },
    )
    record["master_revision"] = content_hash(record)
    write_json(staging / "master-source.json", record)
    write_json(
        staging / "export-result.json",
        {"bounds_world": bounds, "spawn": anchors.pop("Spawn"), "anchors": anchors, "master": record},
    )


if __name__ == "__main__":
    arguments = sys.argv[sys.argv.index("--") + 1 :]
    if arguments[0] == "import":
        import_revision(*arguments[1:])
    elif arguments[0] == "export":
        export_master(*arguments[1:])
    else:
        raise ValueError("unsupported worker command")
