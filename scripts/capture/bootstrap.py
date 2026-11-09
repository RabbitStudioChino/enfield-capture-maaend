"""Local-only Windows capture dependency setup and launcher."""

import argparse
import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT))

from tools.capture.probe import create_run, utc_now, write_json  # noqa: E402


def local_directory(workspace, relative):
    current = workspace
    for part in relative.split("/"):
        current /= part
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"Linked installation path is forbidden: {current}")
        current.mkdir(exist_ok=True)
        if not current.resolve().is_relative_to(workspace.resolve()):
            raise ValueError(f"Installation path escapes workspace: {current}")
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--command", choices=("doctor", "probe"), default="doctor")
    args = parser.parse_args()
    run = create_run(args.workspace)
    report = {
        "schema_version": "capture.probe.bootstrap/1.0",
        "status": "installing",
        "started_utc": utc_now(),
        "python": sys.version,
        "os": platform.platform(),
        "errors": [],
        "capture_exit_code": None,
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
        for base, directories, files in os.walk(venv, followlinks=False):
            for name in directories + files:
                existing = Path(base) / name
                if existing.is_symlink() or existing.is_junction():
                    raise ValueError(f"Linked existing virtual-environment entry is forbidden: {existing}")
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
            python = venv / "Scripts/python.exe"
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
        report["status"] = "installed"
        write_json(path, report)
        completed = subprocess.run(
            [str(python), "-m", "tools.capture", args.command, "--workspace", str(workspace)],
            cwd=SOURCE_ROOT,
            env=environment,
        )
        report["capture_exit_code"] = completed.returncode
        report["status"] = "complete" if completed.returncode == 0 else "validation_failed"
        exit_code = completed.returncode
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
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
