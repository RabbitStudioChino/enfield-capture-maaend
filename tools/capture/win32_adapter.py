"""MaaFramework 5.14.3 Win32 adapter and bounded disposable SDK worker."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from importlib.metadata import version
from pathlib import Path


class WorkerCancelled(RuntimeError):
    """The coordinator cancelled an SDK request before acknowledging its result."""


class Win32Worker:
    def __init__(self, run):
        self.run = Path(run)
        self.sequence = 0
        self.log = (self.run / "logs/worker.log").open("wb")
        environment = os.environ.copy()
        environment.pop("MAAFW_BINARY_PATH", None)
        environment.update(
            PYTHONPATH=str(Path(__file__).resolve().parents[2]),
            PYTHONDONTWRITEBYTECODE="1",
            TEMP=str(self.run / ".worker"),
            TMP=str(self.run / ".worker"),
            TMPDIR=str(self.run / ".worker"),
        )
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "tools.capture.win32_adapter", str(self.run)],
                stdin=subprocess.PIPE,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                cwd=self.run,
                env=environment,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        except Exception:
            self.log.close()
            raise
        self.pid = self.process.pid

    def request(self, operation, timeout, should_cancel=None, **parameters):
        self.sequence += 1
        response = self.run / ".worker" / f"response-{self.sequence:04d}.json"
        command = {"operation": operation, "sequence": self.sequence, **parameters}
        self.process.stdin.write((json.dumps(command) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while not response.exists():
            if should_cancel is not None and should_cancel():
                raise WorkerCancelled(f"SDK worker {self.pid}: {operation} cancelled")
            if self.process.poll() is not None:
                raise RuntimeError(f"SDK worker {self.pid} exited with {self.process.returncode}; see logs/worker.log")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"SDK worker {self.pid}: {operation} exceeded {timeout:g}s")
            time.sleep(0.025)
        if should_cancel is not None and should_cancel():
            raise WorkerCancelled(f"SDK worker {self.pid}: {operation} cancelled before confirmation")
        return json.loads(response.read_text(encoding="utf-8"))

    @property
    def returncode(self):
        return self.process.returncode

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        finally:
            self.process.stdin.close()
            self.log.close()


class WindowsAdapter:
    def __init__(self, run):
        self.run = run
        self.controller = None
        self.window = None
        self.last_window = None
        self.initial_size = None

    def initialize(self):
        result = {"connected": False, "candidates": [], "sdk_version": None, "raw_size_enabled": False}
        try:
            if sys.platform != "win32":
                raise RuntimeError("Windows x64 is required for the Win32 capture probe")
            if sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
                raise RuntimeError("The locked capture runtime requires Python 3.12 x64")
            import ctypes
            from ctypes import wintypes

            self.ctypes = ctypes
            self.types = wintypes
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.user32.GetForegroundWindow.restype = wintypes.HWND
            self.user32.GetForegroundWindow.argtypes = []
            for name in ("IsWindow", "IsIconic", "IsWindowVisible"):
                function = getattr(self.user32, name)
                function.argtypes = [wintypes.HWND]
                function.restype = wintypes.BOOL
            self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self.user32.GetWindowTextW.restype = ctypes.c_int
            self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self.user32.GetClassNameW.restype = ctypes.c_int
            self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            self.user32.GetClientRect.restype = wintypes.BOOL
            self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            self.user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            self.user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            if not self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                raise RuntimeError(f"Cannot enable per-monitor DPI awareness: Win32 error {ctypes.get_last_error()}")
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            self.user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
            self.user32.EnumWindows.restype = wintypes.BOOL

            @callback_type
            def collect(hwnd, _):
                info = self.window_info(hwnd)
                if info["class_name"] == "UnityWndClass" and "Endfield" in info["title"]:
                    result["candidates"].append(info)
                return True

            if not self.user32.EnumWindows(collect, 0):
                raise RuntimeError("Window enumeration failed")
            result["sdk_version"] = version("maafw")
            if result["sdk_version"].removeprefix("v") != "5.14.3":
                raise RuntimeError("Expected the locked maafw 5.14.3 package; run the local setup script")
            if len(result["candidates"]) != 1:
                raise RuntimeError("Exactly one UnityWndClass window with Endfield in its title is required")
            self.window = result["candidates"][0]
            self.hwnd = int(self.window["hwnd"], 16)
            self.check_window()
            from maa.controller import Win32Controller
            from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
            from maa.library import Library
            from maa.toolkit import Toolkit

            if not Toolkit.init_option(self.run / "sdk", {"logging": True}):
                raise RuntimeError("MaaFramework log initialization failed")
            result["library_version"] = Library.version()
            self.controller = Win32Controller(
                hWnd=self.hwnd,
                screencap_method=MaaWin32ScreencapMethodEnum.ScreenDC,
                mouse_method=MaaWin32InputMethodEnum.Null,
                keyboard_method=MaaWin32InputMethodEnum.Null,
            )
            if not self.controller.set_screenshot_use_raw_size(True):
                raise RuntimeError("MaaFramework rejected the raw screenshot size option")
            result["raw_size_enabled"] = True
            self.wait_job(self.controller.post_connection(), 10)
            self.check_window()
            result["connected"] = True
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
            traceback.print_exc()
        result["window"] = self.last_window
        return result

    def window_info(self, hwnd):
        title = self.ctypes.create_unicode_buffer(2048)
        class_name = self.ctypes.create_unicode_buffer(256)
        self.user32.GetWindowTextW(hwnd, title, len(title))
        self.user32.GetClassNameW(hwnd, class_name, len(class_name))
        pid = self.types.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        rect = self.types.RECT()
        self.user32.GetClientRect(hwnd, self.ctypes.byref(rect))
        return {
            "hwnd": hex(hwnd),
            "pid": pid.value,
            "title": title.value,
            "class_name": class_name.value,
            "visible": bool(self.user32.IsWindowVisible(hwnd)),
            "minimized": bool(self.user32.IsIconic(hwnd)),
            "foreground": self.user32.GetForegroundWindow() == hwnd,
            "client_size": [rect.right - rect.left, rect.bottom - rect.top],
        }

    def check_window(self):
        self.last_window = None
        if not self.user32.IsWindow(self.hwnd):
            raise RuntimeError("Game window was closed")
        info = self.window_info(self.hwnd)
        self.last_window = info
        if (
            info["class_name"] != "UnityWndClass"
            or "Endfield" not in info["title"]
            or info["pid"] != self.window["pid"]
        ):
            raise RuntimeError("Game window identity changed")
        if not info["visible"] or info["minimized"] or not info["foreground"]:
            raise RuntimeError(
                "Game window must be visible, restored, and in the foreground "
                f"(visible={info['visible']}, minimized={info['minimized']}, foreground={info['foreground']})"
            )
        if min(info["client_size"]) <= 0:
            raise RuntimeError("Game window has an empty client area")
        return info

    def wait_job(self, job, timeout):
        deadline = time.monotonic() + timeout
        while not job.done:
            self.check_window()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"MaaFramework job exceeded {timeout:g}s")
            time.sleep(0.025)
        if not job.succeeded:
            raise RuntimeError("MaaFramework job failed")

    def capture(self, index):
        result = {
            "image": {"status": "failed"},
            "fatal": False,
            "timing": {"request_monotonic_ns": None, "complete_monotonic_ns": None, "native_render_timestamp": None},
        }
        temporary = self.run / "images" / f"frame-{index:06d}.png.partial"
        capture_pending = False
        try:
            before = self.check_window()
            result["window"] = before
            result["timing"]["request_monotonic_ns"] = str(time.monotonic_ns())
            capture_pending = True
            job = self.controller.post_screencap()
            self.wait_job(job, 5)
            capture_pending = False
            result["timing"]["complete_monotonic_ns"] = str(time.monotonic_ns())
            after = self.check_window()
            if after["client_size"] != before["client_size"]:
                raise RuntimeError("Window size changed during capture")
            image = job.get(wait=False)
            import numpy as np
            from PIL import Image

            if image is None or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
                raise RuntimeError("MaaFramework returned an empty or unsupported image")
            height, width = image.shape[:2]
            raw_resolution = tuple(self.controller.resolution)
            result["raw_resolution"] = list(raw_resolution)
            if raw_resolution != (width, height):
                raise RuntimeError("Screenshot dimensions differ from the SDK raw resolution")
            if self.initial_size is not None and self.initial_size != (width, height):
                raise RuntimeError("Screenshot dimensions changed; start a new probe run")
            if int(image.max()) <= 2:
                raise RuntimeError("Screenshot is black or near-black")
            self.check_window()
            rgb = np.ascontiguousarray(image[:, :, ::-1])
            Image.fromarray(rgb).save(temporary, format="PNG")
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            target = temporary.with_suffix("")
            if target.exists():
                raise RuntimeError("Refusing to overwrite an existing capture")
            temporary.replace(target)
            self.initial_size = (width, height)
            result["image"] = {
                "status": "captured",
                "uri": target.relative_to(self.run).as_posix(),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "width": width,
                "height": height,
                "pixel_format": "RGB8",
                "source_format": "BGR8",
            }
        except Exception as error:
            result["window"] = self.last_window
            result["error"] = f"{type(error).__name__}: {error}"
            result["image"]["reason"] = result["error"]
            result["fatal"] = capture_pending or isinstance(error, TimeoutError)
            traceback.print_exc()
        finally:
            temporary.unlink(missing_ok=True)
        return result


def worker_main(run):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    adapter = WindowsAdapter(run)
    for line in sys.stdin:
        request = json.loads(line)
        result = adapter.initialize() if request["operation"] == "initialize" else adapter.capture(request["index"])
        response = run / ".worker" / f"response-{request['sequence']:04d}.json"
        temporary = response.with_suffix(".partial")
        temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(response)


if __name__ == "__main__":
    worker_main(Path(sys.argv[1]))
