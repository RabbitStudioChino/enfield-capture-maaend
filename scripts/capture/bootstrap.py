"""Local-only Windows capture dependency setup and launcher."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT))

from tools.capture.probe import create_run, utc_now, write_json  # noqa: E402


def local_directory(workspace, relative):
    for component in (*reversed(workspace.parents), workspace):
        if component.is_symlink() or component.is_junction():
            raise ValueError(f"Linked workspace path is forbidden: {component}")
    current = workspace
    for part in relative.split("/"):
        current /= part
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"Linked installation path is forbidden: {current}")
        current.mkdir(exist_ok=True)
        if not current.resolve().is_relative_to(workspace.resolve()):
            raise ValueError(f"Installation path escapes workspace: {current}")
    return current


def check_environment_tree(venv):
    for base, directories, files in os.walk(venv, followlinks=False):
        for name in directories + files:
            existing = Path(base) / name
            if existing.is_symlink() or existing.is_junction():
                raise ValueError(f"Linked existing virtual-environment entry is forbidden: {existing}")


@contextmanager
def installation_guard(workspace):
    directory = local_directory(workspace, "local/capture")
    path = directory / ".controller.lock"
    if path.is_symlink() or path.is_junction():
        raise ValueError("Linked controller lock is forbidden")
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("A capture task is active in this workspace; dependency changes are refused") from error
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def launch_capture(workspace, command, arguments):
    venv = local_directory(workspace, "local/envs/capture-windows-py312")
    check_environment_tree(venv)
    python = venv / "Scripts/python.exe"
    if not python.is_file():
        print("Capture environment is missing. Run capture.cmd doctor or capture.cmd start first.", file=sys.stderr)
        return 1
    environment = os.environ.copy()
    temporary = local_directory(workspace, "local/tmp")
    environment.update(
        TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"
    )
    try:
        return subprocess.run(
            [str(python), "-B", "-m", "tools.capture", command, "--workspace", str(workspace), *arguments],
            cwd=SOURCE_ROOT,
            env=environment,
        ).returncode
    except KeyboardInterrupt:
        return 130


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=SOURCE_ROOT)
    parser.add_argument(
        "--command",
        choices=(
            "doctor",
            "probe",
            "start",
            "auto",
            "serve",
            "regions",
            "list",
            "status",
            "pause",
            "stop",
            "resume",
            "logs",
            "export",
        ),
        default="serve",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup-only", action="store_true")
    mode.add_argument("--launch-only", action="store_true")
    parser.add_argument("capture_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.capture_args[1:] if args.capture_args[:1] == ["--"] else args.capture_args
    if args.launch_only:
        return launch_capture(args.workspace.absolute(), args.command, arguments)
    run = create_run(args.workspace)
    report = {
        "schema_version": "capture.probe.bootstrap/1.1",
        "stage": "dependency_setup",
        "status": "installing",
        "started_utc": utc_now(),
        "python": sys.version,
        "os": platform.platform(),
        "errors": [],
    }
    path = run / "report.json"
    write_json(path, report)
    print(f"Setup report: {path}", flush=True)
    exit_code = 1
    try:
        if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
            raise RuntimeError("Install Python 3.12 x64 for Windows, then run capture.cmd again")
        workspace = args.workspace.absolute()
        environment = os.environ.copy()
        temporary = local_directory(workspace, "local/tmp")
        cache = local_directory(workspace, "local/cache/pip")
        venv = local_directory(workspace, "local/envs/capture-windows-py312")
        check_environment_tree(venv)
        environment.update(
            TMP=str(temporary),
            TEMP=str(temporary),
            TMPDIR=str(temporary),
            PYTHONUTF8="1",
            PYTHONDONTWRITEBYTECODE="1",
            PIP_CONFIG_FILE=os.devnull,
        )
        lock = SOURCE_ROOT / "tools/capture/requirements-win.lock"
        report["requirements_sha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
        report["venv"] = str(venv)
        python = venv / "Scripts/python.exe"
        marker = venv / ".capture-ready.json"
        identity = {"requirements_sha256": report["requirements_sha256"], "python": platform.python_version()}
        ready = python.is_file() and marker.is_file() and json.loads(marker.read_bytes()) == identity
        if ready:
            report["reused_environment"] = True
            report["status"] = "installed"
            exit_code = 0
        else:
            install_dependencies(workspace, run, venv, python, environment, lock, cache, marker, identity)
            report["status"] = "installed"
            exit_code = 0
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["errors"].append("Interrupted by user")
        exit_code = 130
    except Exception as error:
        report["status"] = "failed"
        report["errors"].append(f"{type(error).__name__}: {error}")
        print(f"Setup failed. Check {path} and {run / 'logs/install.log'}. {error}", file=sys.stderr)
    finally:
        report["finished_utc"] = utc_now()
        try:
            write_json(path, report)
        except OSError as error:
            print(
                f"{report['status']}: cannot finalize setup report: {error}; errors={report['errors']}", file=sys.stderr
            )
            exit_code = 1
    if exit_code != 0:
        return exit_code
    try:
        return 0 if args.setup_only else launch_capture(workspace, args.command, arguments)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(
            f"Capture launch failed after dependency setup completed: {type(error).__name__}: {error}", file=sys.stderr
        )
        return 1


def install_dependencies(workspace, run, venv, python, environment, lock, cache, marker, identity):
    with installation_guard(workspace):
        with (run / "logs/install.log").open("wb") as log:
            print("Creating the local Python environment (up to 120 seconds).", flush=True)
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)],
                cwd=SOURCE_ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=120,
            )
            print(f"Installing locked dependencies (up to 300 seconds). Log: {run / 'logs/install.log'}", flush=True)
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--retries",
                    "1",
                    "--timeout",
                    "30",
                    "--index-url",
                    "https://pypi.org/simple",
                    "--cache-dir",
                    str(cache),
                    "-r",
                    str(lock),
                ],
                cwd=SOURCE_ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=300,
            )
        write_json(marker, identity)


if __name__ == "__main__":
    raise SystemExit(main())
