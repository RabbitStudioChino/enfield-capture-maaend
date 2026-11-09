# Blender masters and export

This document describes the offline authoring CLI, master revisions and coordinate constraints. Updated 2026-11-09.

## Environment

Use an existing Blender installation. The locally verified version is Blender 5.3.0 Release Candidate `f78a5e153839`. The outer Python environment uses `tools/capture/requirements.lock`, while Blender runs authoring scripts with its bundled Python. Run these commands from the capture repository root.

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.blender --help
```

The CLI stores Blender user configuration, extensions and scripts under workspace `local/cache/blender/`, and temporary files and process logs under `local/tmp/blender/`. All inputs, masters and outputs must remain inside this workspace. Background execution disables embedded master scripts. A run lasts at most one hour, and timeout or interruption terminates its process tree.

## Import reconstruction

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.blender import \
  --reconstruction ../local/reconstruction/region/revisions/REV/reconstruction.json \
  --output ../local/masters/region-r1.blend \
  --blender /Applications/Blender.app/Contents/MacOS/Blender
```

Formal `reconstruction.json` uses `reconstruction.bundle/1.0` and is checked against the capture repository schema. Minimal author-supplied OBJ/PLY/glTF manifests use `blender.source/1.0` and provide the consumed fields below. The importer consumes `revision`, `world_frame_id`, `scale_status`, `mesh_file`, `files`, `source.input_batches` (formal bundles) or `source.input_batch_ids` (manual manifests) and `transform_world_from_sfm`. Each `files` entry supplies `uri`, `sha256` and `bytes`. Meshes and referenced textures must be indexed. Supported inputs are OBJ, PLY, glTF and embedded GLB. Each OBJ `mtllib` line names one file. MTL texture references contain a single relative path without options. PLY external `TextureFile` references are rejected.

Transforms use column vectors `X_world = s R X_sfm + t`. `R` is a row-serialized orthonormal 3×3 rotation matrix, `s > 0`, and `t` is in meters. Explicit `coordinate_revision`, `scale_evidence` and `world_frame_id` are required. Use `--transform ../local/alignment.json` to override the transform explicitly. Unaligned arbitrary input can enter an independent SfM master. Its identity-axis transform preserves arbitrary coordinates and publication is rejected. To align it, explicitly pass `--transform`, `--world-frame` and `--scale-status estimated` together while importing into a new master. Navigation coordinates cannot supply this transform.

```json
{
    "s": 1,
    "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
    "t": [0, 0, 0],
    "coordinate_revision": "alignment-1",
    "scale_evidence": "Author-supplied scale and alignment evidence"
}
```

Blender authoring is Z-up. Import applies `(x_b,y_b,z_b)=(x_w,-z_w,y_w)`. glTF input first recovers its original geometry coordinates before applying the explicit SfM-to-world transform. Final glTF export uses Blender's built-in Y-up conversion, and the release chunk transform is identity.

## Author the master

| Collection | Rule |
| --- | --- |
| `Source_<revision>` | Frozen reconstruction source, hidden and unselectable, with geometry and transform fingerprints verified before reimport |
| `Working` | Authoring copy of the current reconstruction, with new revisions replacing only objects tagged `generated_source` |
| `ManualPatches` | Manual repairs preserved across revisions, with geometry fingerprints and coordinate revision recorded at publication |
| `Collision` | Author-supplied Mesh proxies merged into one static triangle mesh at export |
| `Anchors` | Required `Spawn` feet-position Empty, with other objects exported as named anchors |
| `Export_mobile` | Generated area derived directly from the master for each export profile |

Edit the `Working` copy in Blender and put repairs that must survive reconstruction into `ManualPatches`. Create collision proxies and `Spawn`, then save a new local `.blend`. Export uses evaluated meshes including authoring modifiers. Collision triangles must face outward, with upward-facing ground beneath the spawn.

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.blender import \
  --reconstruction ../local/reconstruction/region/revisions/NEXT/reconstruction.json \
  --master ../local/masters/region-r1.blend \
  --output ../local/masters/region-r2.blend
```

`--output` must be a new path. Reimporting the same revision verifies the manifest hash and preserves idempotent collections. New revisions preserve old Source collections, manual patches, collision and anchors. A changed world frame, coordinate revision or explicit transform requires manual realignment in a new master.

The master contains a `MasterAssetSet.json` text recording source manifest and file hashes, coordinate revision, Blender version and exporter version. Publication additionally records the actual `.blend` hash, each profile's output asset hashes, manual patch fingerprints and export configuration hash.

## Publish

See the [publishing workflow](./release-publishing.en.md) for configuration, pinned contracts and atomic publication. Complete coordinate roundtrips and browser loading with owned fixtures first, then inspect views, textures, holes, doorways, steps and collision on the actual reconstructed master.
