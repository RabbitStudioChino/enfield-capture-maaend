"""Probe the installed binaries and bound every external process to a local run."""

import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from tools.capture.validate import BundleValidationError, load_json, require

from .prepare import check_path, encoded, within

COMMANDS = ("feature_extractor", "exhaustive_matcher", "mapper", "image_undistorter", "model_converter")
MVS_TOOLS = ("InterfaceCOLMAP", "DensifyPointCloud", "ReconstructMesh", "RefineMesh", "TextureMesh")


def file_hash(path):
    hasher = hashlib.sha256()
    with check_path(Path(path)).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def atomic_json(path, value):
    check_path(path)
    temporary = check_path(path.with_name(path.name + ".pending"))
    with temporary.open("wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def local_environment(root):
    environment = os.environ.copy()
    for name, folder in (
        ("HOME", "home"),
        ("USERPROFILE", "home"),
        ("TMPDIR", "tmp"),
        ("TMP", "tmp"),
        ("TEMP", "tmp"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_CONFIG_HOME", "config"),
    ):
        target = check_path(root / folder)
        target.mkdir(parents=True, exist_ok=True)
        environment[name] = str(target)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["GLOG_logtostderr"] = "1"
    return environment


def terminate_tree(process):
    if os.name == "nt":
        # taskkill /T also terminates descendants which inherited the process group.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        time.sleep(0.2)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin may report EPERM for a group whose last process already exited.
            require(process.poll() is not None, "cannot terminate the reconstruction process group")
    process.wait(timeout=15)


def execute(argv, cwd, log, timeout, cancel=None):
    require(0 < timeout <= 604800, "timeout must be 1..604800 seconds")
    cwd, log = check_path(Path(cwd)), check_path(Path(log))
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with log.open("ab") as output:
        output.write(("\nCOMMAND " + repr([str(item) for item in argv]) + "\n").encode("utf-8"))
        output.flush()
        process = subprocess.Popen(
            [str(item) for item in argv],
            cwd=cwd,
            env=local_environment(cwd / ".runtime"),
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **flags,
        )
        try:
            started = time.monotonic()
            while process.poll() is None:
                if cancel is not None and cancel.exists():
                    raise InterruptedError("cancellation requested")
                if time.monotonic() - started >= timeout:
                    raise TimeoutError(f"process exceeded {timeout} seconds")
                time.sleep(0.1)
            require(process.returncode == 0, f"process exited {process.returncode}; see {log}")
        except BaseException:
            terminate_tree(process)
            raise


def help_probe(argv, root, name):
    log = root / (name + ".help.txt")
    try:
        execute(argv, root, log, 30)
        exit_status = "zero"
    except BundleValidationError:
        # Several OpenMVS releases return 1 after printing a complete --help.
        exit_status = "nonzero"
    output = log.read_bytes().decode("utf-8", errors="replace")
    return {"uri": log.name, "sha256": file_hash(log), "text": output, "exit_status": exit_status}


def doctor(workspace, output, colmap=None, openmvs_dir=None):
    workspace = check_path(Path(workspace))
    root = within(workspace, Path(output))
    require(root.is_relative_to(workspace / "local"), "doctor output must be under workspace local/")
    root.mkdir(parents=True, exist_ok=False)
    tools = {}
    executables = {"colmap": colmap or shutil.which("colmap")}
    for name in MVS_TOOLS:
        candidate = Path(openmvs_dir) / (name + (".exe" if os.name == "nt" else "")) if openmvs_dir else None
        executables[name] = str(candidate) if candidate else shutil.which(name)
    for name, value in executables.items():
        if not value or not Path(value).is_file():
            tools[name] = {"status": "missing", "path": str(value) if value else None}
            continue
        executable = check_path(Path(value).absolute())
        entry = {"status": "available", "path": str(executable), "sha256": file_hash(executable), "help": {}}
        try:
            for command in COMMANDS if name == "colmap" else (name,):
                argv = [executable, command, "--help"] if name == "colmap" else [executable, "--help"]
                evidence = help_probe(argv, root, name + "." + command)
                require("--" in evidence["text"], "help did not report any CLI options")
                entry["help"][command] = evidence
            entry["version_evidence"] = next(iter(entry["help"].values()))["text"].splitlines()[:12]
        except (OSError, TimeoutError, BundleValidationError) as error:
            entry["status"], entry["error"] = "unusable", str(error)
        tools[name] = entry
    result = {
        "schema_version": "reconstruction.toolchain/1.0",
        "tools": tools,
        "ready": all(entry["status"] == "available" for entry in tools.values()),
        "mask_propagation": "not_validated",
        "native_execution": "help_only",
    }
    atomic_json(root / "toolchain.json", result)
    return result


def load_toolchain(path):
    result = load_json(check_path(Path(path)).read_bytes(), "toolchain")
    require(
        isinstance(result, dict) and result.get("schema_version") == "reconstruction.toolchain/1.0",
        "unsupported toolchain",
    )
    require(set(result.get("tools", {})) == {"colmap", *MVS_TOOLS}, "incomplete toolchain inventory")
    for name, tool in result["tools"].items():
        require(tool.get("status") in ("available", "missing", "unusable"), f"invalid tool status {name}")
        if tool["status"] == "available":
            require(file_hash(Path(tool["path"])) == tool["sha256"], f"tool changed since doctor: {name}")
            for evidence in tool["help"].values():
                require(
                    hashlib.sha256(evidence["text"].encode("utf-8")).hexdigest() == evidence["sha256"],
                    "help evidence changed",
                )
    return result


def fingerprint(toolchain):
    """Exclude wall clocks, machine capacity, logs and installation paths from build keys."""
    result = {}
    for name, tool in toolchain["tools"].items():
        capabilities = {}
        for operation, evidence in tool.get("help", {}).items():
            capabilities[operation] = sorted(set(re.findall(r"--[A-Za-z][\w.-]*", evidence["text"])))
        result[name] = {"status": tool["status"], "sha256": tool.get("sha256"), "capabilities": capabilities}
    return result


def command(toolchain, tool, operation, options):
    entry = toolchain["tools"][tool]
    require(entry["status"] == "available", f"required tool unavailable: {tool}")
    evidence = entry["help"].get(operation)
    require(evidence is not None, f"missing help capability: {tool} {operation}")
    for option in options[::2]:
        require(
            re.search(r"(?<![\w.-])" + re.escape(option) + r"(?![\w.-])", evidence["text"]),
            f"installed {tool} {operation} does not expose {option}; run doctor for a compatible binary",
        )
    return [entry["path"], *([operation] if tool == "colmap" else []), *[str(value) for value in options]]


def cpu_option(toolchain, operation):
    text = toolchain["tools"]["colmap"].get("help", {}).get(operation, {}).get("text", "")
    choices = (
        ("--FeatureExtraction.use_gpu", "--SiftExtraction.use_gpu")
        if operation == "feature_extractor"
        else ("--FeatureMatching.use_gpu", "--SiftMatching.use_gpu")
    )
    return next((choice for choice in choices if choice in text), choices[0])
