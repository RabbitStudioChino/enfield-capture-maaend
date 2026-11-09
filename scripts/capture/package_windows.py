"""Package built Windows capture agents, exact source resources and local launchers."""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
PYTHON_SHA256 = "0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8"
PYTHON_URL = "https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg"
FORBIDDEN = {".git", "__pycache__", "tests", "test", "samples", "fixtures", "debug", "node_modules"}
MAP_MODULES = ("__init__.py", "model.py", "runtime.py", "agent_session.py", "navmesh_backend.py", "json_import.py")
VC_RUNTIME_DLLS = {
    "concrt140.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
}


def real_path(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError(f"Parent traversal is forbidden: {path}")
    for component in (*reversed(path.parents), path):
        if not component.exists() and not component.is_symlink():
            continue
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Linked package path is forbidden: {component}")
    return path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def allowed(uri):
    path = Path(uri)
    return (
        not any(part.lower() in FORBIDDEN for part in path.parts)
        and path.suffix.lower() not in (".log", ".pyc", ".pdb", ".dmp")
        and not path.name.startswith("test_")
    )


def package(args):
    workspace = real_path(args.workspace)
    install = real_path(SOURCE_ROOT / args.install)
    sdk = real_path(SOURCE_ROOT / args.sdk)
    python = real_path(SOURCE_ROOT / args.python_package)
    output = real_path(SOURCE_ROOT / args.output)
    if not all(path.is_relative_to(workspace) for path in (SOURCE_ROOT, install, sdk, python, output)):
        raise ValueError("All package inputs and outputs must stay inside the workspace")
    if output.exists():
        raise ValueError("Refusing to overwrite an existing delivery")
    if sha256(python) != PYTHON_SHA256:
        raise ValueError("Official Python archive SHA-256 mismatch")
    for relative in (
        "agent/cpp-algo.exe",
        "agent/go-service.exe",
        "agent/MaaUtils.dll",
        "agent/WebView2Loader.dll",
        "maafw/MaaFramework.dll",
        "maafw/MaaAgentServer.dll",
        "resource/model/map/navmesh/base.nav.gz",
    ):
        if not real_path(install / relative).is_file():
            raise ValueError(f"Missing production runtime: {relative}")
    vc_notice = real_path(install / "licenses/VisualCpp")
    vc_runtime = json.loads((vc_notice / "provenance.json").read_text(encoding="utf-8-sig"))
    vc_names = {entry["name"].lower() for entry in vc_runtime["files"]}
    if not VC_RUNTIME_DLLS.issubset(vc_names):
        raise ValueError("The installed Visual C++ Redist does not cover the production PE import dependencies")
    for entry in vc_runtime["files"]:
        for directory in ("agent", "maafw"):
            path = real_path(install / directory / entry["name"])
            if sha256(path) != entry["sha256"]:
                raise ValueError(f"Application-local Visual C++ runtime differs from its installed source: {path}")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE_ROOT, text=True).strip()
    submodules = {
        uri: subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE_ROOT / uri, text=True).strip()
        for uri in ("agent/cpp-algo/MaaUtils", "assets/resource/model")
    }
    files = {}

    def add(uri, path):
        if not allowed(uri):
            return
        path = real_path(path)
        if not path.is_file() or uri in files:
            raise ValueError(f"Invalid or duplicate package file: {uri}")
        if path.suffix.lower() in (".onnx", ".png", ".gz"):
            with path.open("rb") as stream:
                if stream.read(100).startswith(b"version https://git-lfs.github.com/spec/"):
                    raise ValueError(f"Unresolved LFS pointer: {path}")
        files[uri] = path

    def tree(directory, prefix):
        directory = real_path(directory)
        if not directory.is_dir():
            raise ValueError(f"Missing package directory: {directory}")
        for base, directories, names in os.walk(directory, followlinks=False):
            directories[:] = [name for name in directories if name.lower() not in FORBIDDEN]
            for name in directories:
                real_path(Path(base) / name)
            for name in sorted(names):
                path = Path(base) / name
                add(prefix + "/" + path.relative_to(directory).as_posix(), path)

    for prefix in ("tools/capture", "contracts/capture", "docs/capture"):
        tree(SOURCE_ROOT / prefix, prefix)
    for uri in (
        "tools/capture/server.py",
        "tools/capture/regions.py",
        "tools/capture/planner.py",
        "tools/capture/auto_task.py",
        "tools/capture/navigation_worker.py",
        "tools/capture/web/app.js",
        "tools/capture/web/index.html",
    ):
        if uri not in files:
            raise ValueError(f"Missing automatic capture source: {uri}")
    for name in ("bootstrap.py", "capture.cmd", "capture.ps1"):
        add("scripts/capture/" + name, SOURCE_ROOT / "scripts/capture" / name)
    for name in MAP_MODULES:
        add("tools/MapNavigator/" + name, SOURCE_ROOT / "tools/MapNavigator" / name)
    for name in ("camera.js", "map_view.js", "navmesh_3d_data.js"):
        add("tools/MapNavigator/web/static/js/" + name, SOURCE_ROOT / "tools/MapNavigator/web/static/js" / name)
    for uri in (
        "capture.cmd",
        "tools/zmdmap/data/teleport_anchors.json",
        "tools/pipeline-generate/SceneTeleport/anchors.json",
        "assets/interface.json",
        "LICENSE",
    ):
        add(uri, SOURCE_ROOT / uri)
    for name in ("resource", "data"):
        tree(SOURCE_ROOT / "assets" / name, "assets/" + name)
        tree(install / name, "install/" + name)
    tree(install / "agent", "install/agent")
    tree(install / "maafw", "install/maafw")
    tree(vc_notice, "licenses/VisualCpp")
    for uri, path in list(files.items()):
        if uri.startswith(("assets/resource/", "assets/data/")):
            native_uri = "install/" + uri.removeprefix("assets/")
            if native_uri not in files or sha256(files[native_uri]) != sha256(path):
                raise ValueError(f"Source and runtime resource differ: {uri}")
    add("licenses/MaaFramework-LICENSE.md", sdk / "LICENSE.md")
    add("licenses/MaaUtils-LICENSE.md", SOURCE_ROOT / "agent/cpp-algo/MaaUtils/LICENSE.md")
    add("licenses/Model-LICENSE", SOURCE_ROOT / "assets/resource/model/LICENSE")
    deps_share = SOURCE_ROOT / "agent/cpp-algo/MaaUtils/MaaDeps/vcpkg/installed/maa-x64-windows/share"
    for copyright_file in sorted(deps_share.glob("*/copyright")):
        add("licenses/MaaDeps/" + copyright_file.parent.name + ".txt", copyright_file)
    webview = SOURCE_ROOT / "agent/cpp-algo/3rdparty/webview2"
    for path in webview.rglob("*"):
        if path.is_file() and any(word in path.name.lower() for word in ("license", "notice")):
            add("licenses/WebView2/" + path.relative_to(webview).as_posix(), path)
    add("local/cache/python/python.3.12.10.nupkg", python)
    generated = {
        "Start-Region-Capture.cmd": b'@echo off\r\ncall "%~dp0scripts\\capture\\capture.cmd" serve\r\n',
        "SOURCE-NOTICE.txt": (
            f"Endfield automatic region capture Windows x64 delivery\nSource commit {commit}\n"
            f"Corresponding source https://github.com/RabbitStudioChino/enfield-capture-maaend/tree/{commit}\n"
            f"Source submodules {json.dumps(submodules, ensure_ascii=False)}\n"
            "MaaEnd and MaaUtils use their included licenses. Model resources retain their included license.\n"
            "MaaFramework v5.14.3 uses the included LGPL license. MaaDeps v3.0.0 third-party copyright files are included.\n"
            "WebView2 SDK 1.0.2210.55 is sourced from Microsoft's official NuGet package and retains its notices.\n"
            "Microsoft Visual C++ release x64 DLLs are bundled app-local in install/agent and install/maafw.\n"
            "Their installed Visual Studio source, exact file versions, hashes and official redistribution/license sources are retained in licenses/VisualCpp.\n"
            f"Official PSF Python 3.12.10 x64 archive {PYTHON_URL}\nArchive SHA-256 {PYTHON_SHA256}\n"
            "Python license is included verbatim and also remains in the official archive.\n"
            "Run Start-Region-Capture.cmd and read docs/capture/automatic-capture.md or its English counterpart.\n"
            "SDK Python dependencies are installed from PyPI with the locked Windows wheel hashes.\n"
            "Tests, simulations and device validation were explicitly skipped for this feature. Build completion is not acceptance.\n"
            "Project test files, samples, logs, credentials and other application GUIs are excluded.\n"
            f"Unbundled documentation links resolve in the fixed source tree https://github.com/RabbitStudioChino/enfield-capture-maaend/tree/{commit}/docs/capture\n"
        ).encode(),
    }
    with zipfile.ZipFile(python) as archive:
        generated["licenses/Python-LICENSE.txt"] = archive.read("tools/LICENSE.txt")
    entries = [
        {"uri": uri, "sha256": sha256(path), "byte_length": path.stat().st_size} for uri, path in sorted(files.items())
    ]
    entries += [
        {"uri": uri, "sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data)}
        for uri, data in sorted(generated.items())
    ]
    manifest = {
        "format": "capture.windows-delivery/1.0",
        "source_commit": commit,
        "submodules": submodules,
        "sdk_version": "5.14.3",
        "maadeps_version": "3.0.0",
        "python_version": "3.12.10",
        "visual_cpp_runtime": vc_runtime,
        "tests_executed": False,
        "device_validation": "not_performed",
        "files": sorted(entries, key=lambda item: item["uri"]),
    }
    generated["DELIVERY-MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".partial")
    with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for uri, path in sorted(files.items()):
            archive.write(path, uri)
        for uri, data in sorted(generated.items()):
            archive.writestr(uri, data)
    with zipfile.ZipFile(partial) as archive:
        expected = set(files) | set(generated)
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected:
            raise ValueError("Delivery archive file inventory mismatch")
        for entry in entries:
            digest = hashlib.sha256()
            with archive.open(entry["uri"]) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != entry["sha256"]:
                raise ValueError(f"Delivery archive SHA-256 mismatch: {entry['uri']}")
    partial.rename(output)
    checksum = sha256(output)
    output.with_suffix(output.suffix + ".sha256").write_text(f"{checksum}  {output.name}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "path": str(output),
                "files": len(files) + len(generated),
                "bytes": output.stat().st_size,
                "sha256": checksum,
                "source_commit": commit,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--install", type=Path, default=Path("install"))
    parser.add_argument("--sdk", type=Path, default=Path("deps"))
    parser.add_argument("--python-package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    package(parser.parse_args())


if __name__ == "__main__":
    main()
