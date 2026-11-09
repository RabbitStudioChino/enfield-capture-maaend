"""Create a new master revision without overwriting an existing .blend file."""

import argparse
import os
import shutil
import sys
import uuid
from pathlib import Path

from .common import WORKSPACE, digest, load_json, reconstruction_input, require, run_blender, workspace_path, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["import"])
    parser.add_argument("--reconstruction", required=True, type=Path)
    parser.add_argument("--transform", type=Path, help="Explicit s/R/t override JSON, never navigation-derived")
    parser.add_argument("--world-frame", help="Explicit aligned world frame with --transform")
    parser.add_argument(
        "--scale-status", choices=["estimated", "verified"], help="Explicit scale status with alignment override"
    )
    parser.add_argument("--master", type=Path, help="Existing master, opened read-only")
    parser.add_argument("--output", required=True, type=Path, help="New master path, must not exist")
    parser.add_argument("--blender", default="/Applications/Blender.app/Contents/MacOS/Blender")
    args = parser.parse_args()
    output = workspace_path(args.output)
    require(not output.exists() and output.suffix == ".blend", "output must be a new .blend path")
    master = workspace_path(args.master) if args.master else None
    if master:
        require(master.is_file(), "missing master")
    transform = load_json(workspace_path(args.transform)) if args.transform else None
    source = reconstruction_input(args.reconstruction, transform, args.world_frame, args.scale_status)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = workspace_path(WORKSPACE / "local/tmp/blender" / ("import-" + uuid.uuid4().hex))
    temporary.mkdir(parents=True)
    job = temporary / "job.json"
    snapshot_root = temporary / "source"
    lock = output.with_suffix(".blend.lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        require(not output.exists(), "output already exists")
        source_root = workspace_path(args.reconstruction).parent
        for entry in source["record"]["files"]:
            copied = snapshot_root / entry["uri"]
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_root / entry["uri"], copied)
            require(
                copied.stat().st_size == entry["bytes"] and digest(copied) == entry["sha256"],
                "source changed during snapshot",
            )
        mesh_uri = source["record"]["mesh_file"]
        source["mesh"] = str(snapshot_root / mesh_uri)
        write_json(job, source)
        master_hash = digest(master) if master else None
        run_blender(args.blender, ["import", str(job), str(output)], master)
        require(master is None or digest(master) == master_hash, "input master changed during import")
        require(output.is_file(), "Blender did not save master")
    finally:
        os.close(descriptor)
        lock.unlink()
        shutil.rmtree(temporary)
    print(f"Master saved: {output}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from None
