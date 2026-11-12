"""Loopback-only control page for real map-region capture tasks."""

import argparse
import hmac
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .planner import build_plan
from .regions import SOURCE_ROOT, region_catalog, resource_hashes
from .task import check_path, read_json, task_path, valid_id, write_json
from .validate import load_json, require

WEB_ROOT = Path(__file__).with_name("web")
SHARED_ROOT = SOURCE_ROOT / "tools/MapNavigator/web/static/js"
STATIC_FILES = {"/": WEB_ROOT / "index.html", "/app.js": WEB_ROOT / "app.js", "/style.css": WEB_ROOT / "style.css"}
STATIC_FILES.update(
    {f"/shared/{name}": SHARED_ROOT / name for name in ("camera.js", "map_view.js", "navmesh_3d_data.js")}
)
MAX_BODY = 128 * 1024


class CaptureApplication:
    def __init__(self, workspace):
        self.workspace = check_path(workspace)
        require(self.workspace.is_dir(), "工作区必须是已有真实目录")
        self.local = self.workspace / "local"
        check_path(self.local).mkdir(exist_ok=True)
        self.directory = check_path(self.local / "capture-ui")
        self.directory.mkdir(exist_ok=True)
        for name in ("plans", "logs", "mesh", "tmp"):
            check_path(self.directory / name).mkdir(exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.children = {}
        self.lock = threading.Lock()
        self.mesh_lock = threading.Lock()

    def cli_arguments(self, command, arguments):
        return [sys.executable, "-B", "-m", "tools.capture", command, "--workspace", str(self.workspace), *arguments]

    def environment(self):
        env = os.environ.copy()
        env.update(
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONUTF8="1",
            TMPDIR=str(self.directory / "tmp"),
            TEMP=str(self.directory / "tmp"),
            TMP=str(self.directory / "tmp"),
        )
        return env

    def spawn(self, command, task_id, arguments):
        valid_id(task_id)
        with self.lock:
            previous = self.children.get(task_id, [])
            require(
                command != "auto" or all(child.poll() is not None for child in previous),
                "该任务已有控制进程，请先查看当前状态",
            )
            log = check_path(self.directory / "logs" / f"{task_id}-{uuid.uuid4().hex}.log")
            with log.open("xb") as output:
                process = subprocess.Popen(
                    self.cli_arguments(command, arguments),
                    cwd=SOURCE_ROOT,
                    env=self.environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                )
            self.children[task_id] = [child for child in previous if child.poll() is None] + [process]
            return {"task_id": task_id, "pid": process.pid, "launch_log": str(log)}

    def run(self, command, task_id, arguments):
        valid_id(task_id)
        result = subprocess.run(
            self.cli_arguments(command, ["--task", task_id, *arguments]),
            cwd=SOURCE_ROOT,
            env=self.environment(),
            capture_output=True,
            timeout=40,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        error = result.stderr.decode("utf-8", errors="replace")
        require(result.returncode == 0, (error or output or f"命令失败 {result.returncode}")[-8000:])
        return load_json(result.stdout, command) if output.strip() else {"ok": True}

    def start(self, request):
        require(isinstance(request, dict), "请求必须是 JSON 对象")
        plan = build_plan(self.workspace, request)
        task_id = request.get("task") or "auto-" + uuid.uuid4().hex[:20]
        valid_id(task_id)
        require(not task_path(self.workspace, task_id).exists(), "任务 ID 已存在，请恢复旧任务或使用新 ID")
        path = self.directory / "plans" / f"{plan['plan_id']}.json"
        write_json(path, plan)
        result = self.spawn("auto", task_id, ["--task", task_id, "--plan", str(path), "--start-delay", "5"])
        return {**result, "plan": plan, "message": "任务已启动，请在五秒内切回游戏并保持前台；状态以任务检查点为准"}

    def status(self, task_id):
        result = self.run("status", task_id, [])
        with self.lock:
            processes = self.children.get(task_id, [])
            if processes:
                result["launcher_exit_code"] = (
                    None if any(child.poll() is None for child in processes) else processes[-1].returncode
                )
        return result

    def events(self, task_id, after):
        root = task_path(self.workspace, task_id)
        paths = sorted(check_path(root / "events").glob("*.json"))
        selected = [path for path in paths if path.name > after][:80] if after else paths[-80:]
        return {"events": [read_json(path) for path in selected], "cursor": selected[-1].name if selected else after}

    def mesh(self, map_id):
        from .navigation_worker import query_navigation, verify_resource_mirror

        frame = next((item for item in region_catalog()["maps"] if item["id"] == map_id), None)
        require(frame is not None, "未知底图")
        pack = SOURCE_ROOT / "assets/resource/model/map/navmesh/base.nav.gz"
        if not pack.is_file():
            pack = pack.with_suffix("")
        resources = resource_hashes([pack.relative_to(SOURCE_ROOT).as_posix()])
        verify_resource_mirror(self.workspace, resources)
        fingerprint = resources[0]["sha256"]
        path = check_path(self.directory / "mesh" / f"{map_id}-{fingerprint}.nmsh")
        with self.mesh_lock:
            if not path.is_file():
                temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
                result = query_navigation(self.workspace, "mesh", zone_id=frame["zone_id"], out_file=str(temporary))
                require(result.get("ok"), result.get("error", "网格读取失败"))
                require(
                    temporary.is_file() and 16 <= temporary.stat().st_size <= 128 * 1024 * 1024,
                    "网格缺失或超出显示预算",
                )
                with temporary.open("rb") as stream:
                    require(stream.read(4) == b"NMSH", "网格格式不受支持")
                require(
                    resource_hashes([pack.relative_to(SOURCE_ROOT).as_posix()]) == resources,
                    "网格资源已改变，请重新载入",
                )
                verify_resource_mirror(self.workspace, resources)
                temporary.replace(path)
            require(path.is_file() and path.stat().st_size <= 128 * 1024 * 1024, "网格缺失或超出显示预算")
        return path

    def close(self):
        with self.lock:
            children = [(task_id, list(processes)) for task_id, processes in self.children.items()]
        for task_id, processes in children:
            if any(process.poll() is None for process in processes):
                try:
                    self.run("stop", task_id, ["--command-id", "server-close-" + uuid.uuid4().hex])
                    for process in processes:
                        process.wait(timeout=5)
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    print(f"任务 {task_id} 的停止结果需查看本地检查点和日志 {error}", file=sys.stderr)


class CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        return

    def check_origin(self, mutation=False):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        require(self.headers.get_all("Host", []) == [expected_host], "不允许的 Host")
        origin = self.headers.get("Origin")
        require(
            origin == "http://" + expected_host if mutation else origin in (None, "http://" + expected_host),
            "不允许的 Origin",
        )
        if mutation:
            require(
                hmac.compare_digest(self.headers.get("X-Capture-Token", ""), self.server.application.token),
                "控制令牌无效，请刷新页面",
            )

    def send_data(self, data, media, status=200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def send_json(self, record, status=200):
        self.send_data(
            json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def send_file(self, path, media=None):
        path = check_path(path)
        require(path.is_file(), "文件不存在")
        media = media or {
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
        }.get(path.suffix)
        self.send_data(path.read_bytes(), media or mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def do_GET(self):
        try:
            self.check_origin()
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            app = self.server.application
            if route.path in STATIC_FILES:
                self.send_file(STATIC_FILES[route.path])
            elif route.path == "/api/config":
                self.send_json(
                    {
                        "token": app.token,
                        "workspace": str(app.workspace),
                        "defaults": {"radius": 60, "spacing": 20, "views": 8},
                    }
                )
            elif route.path == "/api/catalog":
                self.send_json(region_catalog())
            elif route.path == "/api/zones":
                from .navigation_worker import query_navigation

                self.send_json(query_navigation(app.workspace, "zones"))
            elif route.path == "/api/tasks":
                parent = check_path(app.local / "capture")
                records = [read_json(path) for path in sorted(parent.glob("*/checkpoint.json"))[-100:]]
                self.send_json({"tasks": records})
            elif route.path == "/api/status":
                self.send_json(app.status(query.get("task", [""])[0]))
            elif route.path == "/api/events":
                self.send_json(app.events(query.get("task", [""])[0], query.get("after", [""])[0]))
            elif route.path == "/api/map":
                frame = next(
                    (item for item in region_catalog()["maps"] if item["id"] == query.get("map", [""])[0]), None
                )
                require(frame is not None, "未知底图")
                self.send_file(SOURCE_ROOT / frame["image_uri"], "image/png")
            elif route.path == "/api/mesh":
                self.send_file(app.mesh(query.get("map", [""])[0]), "application/octet-stream")
            elif route.path == "/favicon.ico":
                self.send_data(b"", "image/x-icon", 204)
            else:
                self.send_json({"error": "未知端点"}, 404)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
            self.send_json({"error": str(error)}, 400)

    def do_POST(self):
        try:
            self.check_origin(mutation=True)
            require(self.headers.get_content_type() == "application/json", "只接受 application/json")
            require(self.headers.get("Transfer-Encoding") is None, "不接受分块请求体")
            lengths = self.headers.get_all("Content-Length", [])
            require(len(lengths) == 1 and lengths[0].isascii() and lengths[0].isdigit(), "需要唯一 Content-Length")
            size = int(lengths[0])
            require(0 < size <= MAX_BODY, "请求体超出 128 KiB")
            body = load_json(self.rfile.read(size), "request")
            require(isinstance(body, dict), "请求必须是 JSON 对象")
            app = self.server.application
            route = urlsplit(self.path).path
            if route == "/api/preview":
                self.send_json(build_plan(app.workspace, body))
            elif route == "/api/start":
                self.send_json(app.start(body), 202)
            elif route == "/api/control":
                action = body.get("action")
                require(action in ("pause", "stop", "resume"), "未知控制动作")
                task_id = valid_id(body.get("task"))
                arguments = ["--command-id", valid_id(body.get("command_id"))]
                if body.get("generation"):
                    require(isinstance(body["generation"], str), "generation 必须为字符串")
                    arguments.extend(["--generation", body["generation"]])
                if action == "resume":
                    self.send_json(app.spawn(action, task_id, ["--task", task_id, *arguments]), 202)
                else:
                    self.send_json(app.run(action, task_id, arguments))
            elif route == "/api/export":
                arguments = ["--session", body["session"]] if body.get("session") else []
                require(all(isinstance(value, str) for value in arguments), "session 必须为字符串")
                self.send_json(app.run("export", valid_id(body.get("task")), arguments))
            else:
                self.send_json({"error": "未知端点"}, 404)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def serve(workspace, port=8780, open_browser=True):
    require(type(port) is int and 1024 <= port <= 65535, "端口必须为 1024 至 65535")
    app = CaptureApplication(workspace)
    server = ThreadingHTTPServer(("127.0.0.1", port), CaptureHandler)
    server.daemon_threads = True
    server.application = app
    server.timeout = 1
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"本地采集控制页 {url}\n工作区 {app.workspace}\n关闭时请求停止本页启动的任务。", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    return serve(args.workspace, args.port, not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
