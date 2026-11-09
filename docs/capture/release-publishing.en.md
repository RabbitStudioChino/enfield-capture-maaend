# WorldRelease publishing workflow

This document describes building an independently distributable WorldRelease from a Blender master. Updated 2026-11-09.

## Pin contracts

`contracts.lock` pins the web repository's full commit and SHA-256 of four schemas. `vendor/world-contracts/` contains only authoritative schemas. Execution checks the local lock and hashes. Review and commit contract changes in the web repository before running this maintenance command.

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.world_export vendor \
  --repository ../endfield-world-web \
  --commit 6a1c4e8a6be8959736b00b09ab964c8f323f4d5d
```

The web repository uses `world.collision/2.0` for triangle meshes. Web consumers retain support for existing `world.collision/1.0` boxes. This producer publishes triangle meshes baked into world coordinates with an identity chunk transform.

## Export configuration

Place configuration and character GLB files inside workspace `local/`. The character must be an owned or licensed GLB with embedded resources. Authors supply coordinates, scale evidence and budgets. Values below only illustrate the format.

```json
{
    "scene_id": "region",
    "release_id": "region-r1",
    "accepted_reconstruction_revision": "REPLACE_WITH_REVIEWED_REVISION",
    "profiles": {
        "mobile": {
            "lods": [
                {"decimate_ratio": 0.5, "min_distance_m": 0, "hysteresis_m": 0},
                {"decimate_ratio": 0.25, "min_distance_m": 20, "hysteresis_m": 2}
            ]
        },
        "high": {"decimate_ratio": 1}
    },
    "unlit": true,
    "source_kind": "reconstruction",
    "description": "Manually inspected regional master",
    "character_glb": "character.glb",
    "scale_reference": {
        "method": "manual-estimate",
        "description": "Author-supplied scale evidence"
    },
    "roam_bounds_world": {"min": [-5, -1, -5], "max": [5, 5, 5]},
    "camera": {
        "mode": "third_person",
        "target_height_m": 1.1,
        "distance_m": 4,
        "yaw_degrees": 0,
        "pitch_degrees": -15,
        "vertical_fov_degrees": 55,
        "near_m": 0.1,
        "far_m": 100
    }
}
```

`mobile` is required. `balanced` and `high` are optional. Every profile derives directly from the same evaluated master mesh, applying Decimate with `decimate_ratio`. Profiles support explicit `lods` with strictly increasing distances and zero distance and hysteresis for LOD0. Every LOD derives directly from the master, preserving UVs and materials in embedded GLB. A single LOD may use `decimate_ratio` directly. `unlit=true` builds unlit materials from Principled Base Color links and uses `KHR_materials_unlit` to preserve screenshot appearance. Review transparent materials, complex custom shaders, color and reference views manually. Missing physical material parameters are not inferred from screenshots. Textures retain their original images, without compression or automatic device-budget decisions.

## Build and validate

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.world_export build \
  --master ../local/masters/region-r2.blend \
  --config ../local/export/region.json \
  --releases ../local/releases

PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python -m tools.world_export validate \
  ../local/releases/region-r1
```

`accepted_reconstruction_revision` must match the current reconstruction revision that the author has reviewed. Formal source batches receive distinct IDs from a canonical hash of `(source_id,batch_id)`, retaining the full original triples to distinguish repeated batch names across sessions.

Build reads the master, generates assets in a unique sibling staging directory, runs independent producer schema and semantic checks, then atomically renames that directory to `release_id`. Existing release IDs and master outputs are rejected. An exclusive lock protects each release ID. Failed staging and logs remain available for diagnosis, while old releases stay usable. After an interrupted machine shutdown, confirm the original process has ended before removing its stale lock manually.

Output includes `world.json`, `asset-index.json`, profile GLBs, character GLB, `collision.json` and `master-source.json`. Successful publication prints the world hash. The world binds the asset-index hash, the index binds asset hashes and byte lengths, and `build.master_source` binds master provenance, which references reconstruction manifests, input batches and original file hashes. Published packages omit masters and raw images.

Checks cover pinned schemas, strict JSON, safe relative paths, duplicate IDs, reference closure, embedded GLB resources, extension declarations, file bytes and hashes, LOD ordering, world coordinates, triangle indices, nondegenerate geometry after float32 conversion, spawn ground support and consistent provenance. Feet support requires an upward triangle within 0.15 meters and at most 45 degrees of slope. Consumers still need capsule overlap, step, doorway and real-device interaction acceptance.

## Cross-repository integration

The web repository can independently inspect the same package with its read-only validator.

```bash
PYTHONDONTWRITEBYTECODE=1 ../local/envs/capture-312/bin/python \
  ../endfield-world-web/tools/asset-validation/validate_world_release.py \
  ../local/releases/region-r1
```

After validation, place the complete release directory under the static site's asset root and load it through the web application's restricted release path. Verify the character, spawn, collision, all profiles and anchor coordinates in a browser. Owned fixtures establish local toolchain behavior. Actual game assets still require texture, reconstruction-quality, scale and device-experience acceptance.
