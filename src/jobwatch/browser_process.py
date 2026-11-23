"""Enforce wall-clock deadlines outside the browser and its driver."""

import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from .common import read_json, write_json


class BrowserInterrupted(ValueError):
    """The renderer stopped responding and the current session is unusable."""


def windows_job(pid):
    """Return a callable that closes a kill-on-close job containing the renderer."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD), ("minimum_memory", ctypes.c_size_t),
            ("maximum_memory", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
            ("process_limit", ctypes.c_size_t), ("job_limit", ctypes.c_size_t),
            ("process_peak", ctypes.c_size_t), ("job_peak", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create, configure, open_process, assign, close = (
        kernel.CreateJobObjectW, kernel.SetInformationJobObject, kernel.OpenProcess,
        kernel.AssignProcessToJobObject, kernel.CloseHandle,
    )
    create.argtypes, create.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
    configure.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    close.argtypes = [wintypes.HANDLE]
    job = create(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not configure(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = open_process(0x100 | 0x001, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not assign(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close(process)
    except OSError:
        close(job)
        raise

    def release():
        if not close(job):
            raise ctypes.WinError(ctypes.get_last_error())

    return release


def launch_worker(folder):
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return subprocess.Popen([sys.executable, "-m", "jobwatch.browser_process", str(folder)], **options)


def unix_descendants(pid):
    listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True,
                             check=True, timeout=2).stdout
    children = {}
    for line in listing.splitlines():
        child, parent = map(int, line.split())
        children.setdefault(parent, []).append(child)
    descendants = []
    pending = children.get(pid, [])
    while pending:
        child = pending.pop()
        descendants.append(child)
        pending.extend(children.get(child, []))
    return descendants

def retry_io(operation, attempts=8, delay=0.03):
    for attempt in range(attempts):
        try:
            return operation()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


class IsolatedBrowser:
    def __init__(self, source, output):
        self.source = source
        self.output = output.resolve()
        options = source["browser"]
        self.timeout = options.get("operation_timeout_seconds", options.get("timeout_seconds", 18) * 2 + 15)
        self.process = None
        self.release_job = None
        self.broken = False

    def start(self):
        self.spool = TemporaryDirectory(prefix="jobwatch-browser-", ignore_cleanup_errors=True)
        self.directory = Path(self.spool.name)
        write_json(self.directory / "source.json", self.source)
        self.process = launch_worker(self.directory)
        if os.name == "nt":
            try:
                self.release_job = windows_job(self.process.pid)
            except OSError:
                pass

    def stop(self, interrupted=False):
        process, self.process = self.process, None
        if process is None:
            return ""
        problem = ""
        if not interrupted:
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            if self.release_job:
                release, self.release_job = self.release_job, None
                release()
            elif os.name != "nt":
                descendants = []
                if interrupted:
                    try:
                        descendants = unix_descendants(process.pid)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        problem = "浏览器子进程树清理未确认"
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for pid in reversed(descendants):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            elif process.poll() is None:
                kill = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], timeout=4,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
                if kill.returncode != 0 and process.poll() is None:
                    try:
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        problem = "浏览器子进程树清理未确认"
        except ProcessLookupError:
            pass
        except (OSError, subprocess.SubprocessError):
            problem = "浏览器子进程树清理未确认"
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            problem = "浏览器工作进程退出未确认"
        return problem

    def interrupt(self, request, reason):
        self.broken = True
        cleanup = self.stop(interrupted=True)
        try:
            progress = read_json(self.directory / "phase.json")
        except (OSError, ValueError):
            progress = {"phase": "启动浏览器工作进程"}
        message = f"{reason}\uff08{progress['phase']}\uff0c{request.get('path', '关闭会话')}\uff09"
        if cleanup:
            message += "\uff1b" + cleanup
        write_json(self.output / "browser-watchdog.json", {"operation": request, **progress, "message": message})
        return BrowserInterrupted(message)

    def exchange(self, command, timeout, **fields):
        request = dict(operation=command, **fields)
        deadline = time.monotonic() + timeout
        if self.broken:
            raise BrowserInterrupted("浏览器工作进程已中止")
        if self.process is None:
            self.start()
        phase_path = self.directory / "phase.json"
        retry_io(lambda: phase_path.unlink(missing_ok=True))
        retry_io(lambda: write_json(self.directory / "request.json", request))
        reply_path = self.directory / "reply.json"
        while time.monotonic() < deadline:
            if reply_path.is_file():
                try:
                    reply = retry_io(lambda: read_json(reply_path))
                except PermissionError:
                    if time.monotonic() < deadline:
                        time.sleep(0.05)
                        continue
                    raise
                retry_io(lambda: reply_path.unlink(missing_ok=True))
                if "error" in reply:
                    raise ValueError(reply["error"])
                return reply.get("value")
            if self.process.poll() is not None and not reply_path.is_file():
                raise self.interrupt(request, "浏览器工作进程意外退出")
            time.sleep(0.05)
        raise self.interrupt(request, f"浏览器操作超过 {timeout} 秒硬超时")

    def get(self, _site, url, path, detail=False):
        self.exchange("get", self.timeout, url=url, path=str(path.resolve()), detail=detail)
        return path.read_bytes()

    def advance(self, path, next_page, expected_rows):
        self.exchange("advance", self.timeout, path=str(path.resolve()), page=next_page, rows=expected_rows)
        return path.read_bytes()

    def close(self):
        error = ""
        closed = False
        try:
            if self.process is not None:
                self.exchange("close", 5)
                closed = True
        except (ValueError, OSError) as exc:
            error = str(exc)
        finally:
            cleanup = self.stop(interrupted=not closed)
            if hasattr(self, "spool"):
                self.spool.cleanup()
        return error or cleanup


def worker(directory):
    from .browser import BrowserPages
    from .sources import WebSource

    source = read_json(directory / "source.json")
    browser = BrowserPages(source)
    browser.progress = lambda phase: retry_io(lambda: write_json(directory / "phase.json", {"phase": phase}))
    site = WebSource(source)
    next_request = directory / "request.json"
    while True:
        if not next_request.is_file():
            time.sleep(0.05)
            continue
        request = retry_io(lambda: read_json(next_request))
        retry_io(lambda: next_request.unlink(missing_ok=True))
        action = request["operation"]
        try:
            if action == "get":
                browser.get(site, request["url"], Path(request["path"]), detail=request["detail"])
            elif action == "advance":
                browser.advance(Path(request["path"]), request["page"], request["rows"])
            elif action == "close":
                browser.progress("关闭浏览器会话")
                browser.stack.close()
            else:
                raise ValueError("未知浏览器操作")
            reply = {}
        except (OSError, ValueError) as error:
            reply = {"error": str(error)}
        retry_io(lambda: write_json(directory / "reply.json", reply))
        if action == "close":
            return


if __name__ == "__main__":
    worker(Path(sys.argv[1]))
from tempfile import TemporaryDirectory
import time

from .common import read_json, write_json


class BrowserInterrupted(ValueError):
    """The renderer stopped responding and the current session is unusable."""


def windows_job(pid):
    """Return a callable that closes a kill-on-close job containing the renderer."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD), ("minimum_memory", ctypes.c_size_t),
            ("maximum_memory", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", ctypes.c_DWORD if False else wintypes.DWORD),
        ]
