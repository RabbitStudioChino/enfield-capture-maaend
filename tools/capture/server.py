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
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .planner import build_plan, selection_parameters, validate_polygon
from .probe import utc_now
from .regions import SOURCE_ROOT, region_catalog, resource_hashes
from .task import check_path, encoded, exclusive, read_json, task_path, valid_id, write_json
from .validate import load_json, read_verified, require

WEB_ROOT = Path(__file__).with_name("web")
SHARED_ROOT = SOURCE_ROOT / "tools/MapNavigator/web/static/js"
STATIC_FILES = {"/": WEB_ROOT / "index.html", "/app.js": WEB_ROOT / "app.js", "/style.css": WEB_ROOT / "style.css"}
STATIC_FILES.update(
    {f"/shared/{name}": SHARED_ROOT / name for name in ("camera.js", "map_view.js", "navmesh_3d_data.js")}
)
MAX_BODY = 128 * 1024
MAX_REVIEW_BODY = 4 * 1024 * 1024
REVIEW_IMPORT_ROUTES = {"/api/project/import", "/api/rescan/preview", "/api/rescan/start"}
MAX_SELECTION_BYTES = 1024 * 1024
MAX_SELECTIONS = 100
SELECTION_SCHEMA = "capture.saved-selections/1.0"
SELECTION_FIELDS = {
    "id",
    "name",
    "created_utc",
    "region",
    "map",
    "polygon",
    "radius",
    "spacing",
    "views",
    "max_points",
    "tier",
    "floor_y",
}


def selection_text(value, label, maximum=120):
    require(
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value == value.strip()
        and all(ord(character) >= 32 for character in value),
        f"{label}必须为 1 至 {maximum} 个可显示字符",
    )
    return value


def current_selection(request):
    parameters = selection_parameters(request)
    catalog = region_catalog()
    region = next((item for item in catalog["regions"] if item["id"] == request.get("region")), None)
    require(region is not None, "选区的区域锚点已失效，请重新选择真实区域")
    require(region["map"] == request.get("map"), "选区底图与区域锚点不一致，请重新选择")
    frame = next(item for item in catalog["maps"] if item["id"] == region["map"])
    return {
        "region": region["id"],
        "map": frame["id"],
        "polygon": validate_polygon(request.get("polygon"), frame["size"]),
        **parameters,
    }


class CaptureApplication:
    def __init__(self, workspace):
        self.workspace = check_path(workspace)
        require(self.workspace.is_dir(), "工作区必须是已有真实目录")
        self.local = self.workspace / "local"
        check_path(self.local).mkdir(exist_ok=True)
        self.directory = check_path(self.local / "capture-ui")
        self.directory.mkdir(exist_ok=True)
        for name in ("plans", "logs", "mesh", "tmp", "imports"):
            check_path(self.directory / name).mkdir(exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.children = {}
        self.lock = threading.Lock()
        self.mesh_lock = threading.Lock()
        self.evidence = {}

    def workspace_path(self, value):
        require(isinstance(value, str) and value.strip(), "需要工作区内的路径")
        path = Path(value)
        path = check_path(path if path.is_absolute() else self.workspace / path)
        require(path.is_relative_to(self.workspace), "路径必须位于当前工作区")
        return path

    def projects(self):
        from tools.reconstruction.project import project_status

        parent = check_path(self.local / "reconstruction-projects")
        records = []
        for path in sorted(parent.glob("*/project.json"))[-100:]:
            check_path(path)
            try:
                result = project_status(self.workspace, valid_id(path.parent.name))
                records.append({key: result.get(key) for key in ("project_id", "status", "stop_reason")})
            except (OSError, ValueError, KeyError, TypeError) as error:
                records.append({"project_id": path.parent.name, "status": "unreadable", "stop_reason": str(error)})
        return {"projects": records}

    def project_status(self, identity):
        from tools.reconstruction.project import project_status

        return project_status(self.workspace, valid_id(identity))

    def export_review(self, request):
        from tools.reconstruction.project import export_review

        return export_review(
            self.workspace,
            valid_id(request.get("project")),
            valid_id(request.get("round")),
            "review-" + uuid.uuid4().hex[:20],
        )

    def rescan_document(self, request):
        raw = request.get("document")
        require(isinstance(raw, str) and len(raw.encode("utf-8")) <= MAX_REVIEW_BODY, "请导入不超过 4 MiB 的审阅 JSON")
        document = load_json(raw.encode("utf-8"), "导入的审阅/补采 JSON")
        require(isinstance(document, dict), "导入内容必须是 JSON 对象")
        if document.get("schema_version") == "reconstruction.review-result/1.0":
            require(isinstance(document.get("rescan"), dict), "审阅结果没有补采请求，可仅存档审阅结果")
            document = document["rescan"]
        require(len(encoded(document)) <= MAX_BODY, "补采请求本身超出 128 KiB，请缩小本轮动作和证据清单")
        return document

    def preview_rescan(self, request):
        from .rescan import preview_rescan

        document = self.rescan_document(request)
        review = self.workspace_path(request.get("review"))
        bundle = self.workspace_path(request.get("bundle"))
        result = preview_rescan(self.workspace, document, review, bundle)
        images = {}
        # Only hash-verified images from the review's frozen sources become browser URLs.
        for issue in result.get("issues", []):
            issue["images"] = []
            for evidence in issue.pop("evidence_images", []):
                path = self.workspace_path(evidence["image_uri"])
                require(path.suffix.lower() in (".png", ".jpg", ".jpeg"), "证据图像格式不受支持")
                require(path.stat().st_size <= 32 * 1024 * 1024, "证据图像超出 32 MiB")
                read_verified(path, evidence["sha256"], False)
                identity = uuid.uuid4().hex
                images[identity] = (path, evidence["sha256"])
                issue["images"].append(
                    {
                        "session_id": evidence["session_id"],
                        "frame_id": evidence["frame_id"],
                        "url": "/api/rescan-evidence?id=" + identity,
                    }
                )
        with self.lock:
            self.evidence = images
        return result

    def evidence_image(self, identity):
        with self.lock:
            record = self.evidence.get(identity)
        require(record is not None, "证据预览已过期，请重新校验补采 JSON")
        path, expected_hash = record
        require(path.stat().st_size <= 32 * 1024 * 1024, "证据图像超出 32 MiB")
        return read_verified(check_path(path), expected_hash, True), mimetypes.guess_type(path.name)[0]

    def import_review(self, request):
        from tools.reconstruction.project import import_review

        raw = request.get("document")
        require(isinstance(raw, str), "需要审阅 JSON 原文")
        document = load_json(raw.encode("utf-8"), "审阅结果")
        require(isinstance(document, dict), "审阅结果必须是对象")
        project = valid_id(request.get("project"))
        path = check_path(self.directory / "imports" / (uuid.uuid4().hex + ".json"))
        write_json(path, document)
        return import_review(self.workspace, project, path)

    def start_rescan(self, request):
        from .rescan import resolve_rescan

        plan = resolve_rescan(
            self.workspace,
            self.rescan_document(request),
            self.workspace_path(request.get("review")),
            self.workspace_path(request.get("bundle")),
        )
        return self.launch_plan(plan, request.get("task"))

    def selections(self):
        path = check_path(self.directory / "selections.json")
        if not path.exists():
            return []
        try:
            require(path.is_file(), "列表路径不是普通文件")
            with path.open("rb") as stream:
                data = stream.read(MAX_SELECTION_BYTES + 1)
            require(len(data) <= MAX_SELECTION_BYTES, "列表文件超出 1 MiB")
            record = load_json(data, str(path))
            require(
                isinstance(record, dict)
                and set(record) == {"schema_version", "selections"}
                and record["schema_version"] == SELECTION_SCHEMA,
                "不支持的选区列表格式",
            )
            items = record["selections"]
            require(isinstance(items, list) and len(items) <= MAX_SELECTIONS, "选区列表最多 100 条")
            seen = set()
            for item in items:
                require(isinstance(item, dict) and set(item) == SELECTION_FIELDS, "选区记录字段不完整或未知")
                identity = selection_text(item["id"], "选区 ID", 32)
                require(uuid.UUID(identity).hex == identity and identity not in seen, "选区 ID 无效或重复")
                seen.add(identity)
                selection_text(item["name"], "选区名称", 80)
                timestamp = selection_text(item["created_utc"], "保存时间", 40)
                require(datetime.fromisoformat(timestamp).tzinfo == UTC, "保存时间必须为 UTC")
                selection_text(item["region"], "区域锚点")
                selection_text(item["map"], "底图")
                selection_parameters(item)
                validate_polygon(item["polygon"])
            return items
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f"本地选区列表读取失败，原文件保留，请检查 {path}：{error}") from error

    def save_selection(self, request):
        name = selection_text(request.get("name"), "选区名称", 80)
        selection = current_selection(request)
        with exclusive(self.directory / ".selections.lock"):
            items = self.selections()
            require(len(items) < MAX_SELECTIONS, "已保存 100 个选区，请先删除不需要的选区")
            item = {"id": uuid.uuid4().hex, "name": name, "created_utc": utc_now(), **selection}
            items.insert(0, item)
            record = {"schema_version": SELECTION_SCHEMA, "selections": items}
            require(len(encoded(record)) <= MAX_SELECTION_BYTES, "选区列表将超出 1 MiB，请先删除不需要的选区")
            write_json(check_path(self.directory / "selections.json"), record, replace=True)
        return {"selection": item, "selections": items}

    def load_selection(self, identity):
        item = next((item for item in self.selections() if item["id"] == identity), None)
        require(item is not None, "该选区已不存在，请重新打开页面读取列表")
        return {**item, **current_selection(item)}

    def delete_selection(self, identity):
        with exclusive(self.directory / ".selections.lock"):
            items = self.selections()
            remaining = [item for item in items if item["id"] != identity]
            require(len(remaining) != len(items), "该选区已不存在，请重新打开页面读取列表")
            write_json(
                check_path(self.directory / "selections.json"),
                {"schema_version": SELECTION_SCHEMA, "selections": remaining},
                replace=True,
            )
        return {"selections": remaining}

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
        return self.launch_plan(plan, request.get("task"))

    def launch_plan(self, plan, task_id=None):
        task_id = task_id or "auto-" + uuid.uuid4().hex[:20]
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
            elif route.path == "/api/selections":
                self.send_json({"selections": app.selections()})
            elif route.path == "/api/projects":
                self.send_json(app.projects())
            elif route.path == "/api/project/status":
                self.send_json(app.project_status(query.get("project", [""])[0]))
            elif route.path == "/api/rescan-evidence":
                data, media = app.evidence_image(query.get("id", [""])[0])
                self.send_data(data, media)
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
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
            self.send_json({"error": str(error)}, 400)

    def do_POST(self):
        try:
            self.check_origin(mutation=True)
            require(self.headers.get_content_type() == "application/json", "只接受 application/json")
            require(self.headers.get("Transfer-Encoding") is None, "不接受分块请求体")
            lengths = self.headers.get_all("Content-Length", [])
            require(len(lengths) == 1 and lengths[0].isascii() and lengths[0].isdigit(), "需要唯一 Content-Length")
            size = int(lengths[0])
            route = urlsplit(self.path).path
            maximum = MAX_REVIEW_BODY if route in REVIEW_IMPORT_ROUTES else MAX_BODY
            require(0 < size <= maximum, f"请求体必须在 1 至 {maximum} 字节内")
            body = load_json(self.rfile.read(size), "request")
            require(isinstance(body, dict), "请求必须是 JSON 对象")
            app = self.server.application
            if route == "/api/preview":
                self.send_json(build_plan(app.workspace, body))
            elif route == "/api/selections":
                self.send_json(app.save_selection(body), 201)
            elif route == "/api/selections/load":
                self.send_json({"selection": app.load_selection(selection_text(body.get("id"), "选区 ID", 32))})
            elif route == "/api/selections/delete":
                self.send_json(app.delete_selection(selection_text(body.get("id"), "选区 ID", 32)))
            elif route == "/api/start":
                self.send_json(app.start(body), 202)
            elif route == "/api/project/review-export":
                self.send_json(app.export_review(body), 201)
            elif route == "/api/project/import":
                self.send_json(app.import_review(body), 201)
            elif route == "/api/rescan/preview":
                self.send_json(app.preview_rescan(body))
            elif route == "/api/rescan/start":
                self.send_json(app.start_rescan(body), 202)
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
