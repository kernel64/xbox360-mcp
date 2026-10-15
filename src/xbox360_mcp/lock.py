"""Cross-process reservation of a console.

Every Claude Code session runs its own xbox360-mcp process, and build scripts use the CLI: without
coordination one of them can deploy / launch / reboot in the middle of another one's test.

The reservation is a JSON file (~/.xbox360-mcp/locks/console-<host>.lock) with a lease: each call
made by the holder renews it, and it is free again once the lease expires or the holder process is
gone. Reading and rewriting that file is serialised with an OS lock on a sibling `.guard` file, which
the OS releases by itself if a process dies.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import time
import uuid
from pathlib import Path

DEFAULT_LEASE = 600.0


class ConsoleBusy(RuntimeError):
    """The console is reserved by another session."""

    def __init__(self, info: dict, hint: str = ""):
        self.info = info
        super().__init__(describe(info) + (f" {hint}" if hint else ""))


def _duration(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def describe(info: dict) -> str:
    reason = f" ({info['reason']})" if info.get("reason") else ""
    return (
        f"Console reserved by {info.get('owner')}{reason} for {_duration(info.get('held_for_s', 0))}, "
        f"lease expires in {_duration(info.get('expires_in_s', 0))} "
        f"(pid {info.get('pid')} on {info.get('machine')})."
    )


def default_owner() -> str:
    """Name shown to other sessions: XBOX360_OWNER, else the Claude Code project folder, else the pid."""
    if os.environ.get("XBOX360_OWNER"):
        return os.environ["XBOX360_OWNER"]
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        return Path(project).name
    return f"pid {os.getpid()}"


def _lock_dir() -> Path:
    base = Path(os.environ.get("XBOX360_CACHE_DIR") or Path.home() / ".xbox360-mcp") / "locks"
    base.mkdir(parents=True, exist_ok=True)
    return base


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _os_lock(path: Path, timeout: float = 10.0):
    """Exclusive OS-level lock on `path` (short critical sections only)."""
    f = open(path, "a+b")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not lock {path}")
                time.sleep(0.02)
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


class ConsoleLock:
    """Lease-based reservation of one console, shared by every process on this PC."""

    def __init__(self, host: str, *, owner: str | None = None, lease_seconds: float = DEFAULT_LEASE):
        self.host = host
        self.owner = owner or default_owner()
        self.lease_seconds = lease_seconds
        self.token = uuid.uuid4().hex
        self.pid = os.getpid()
        self.machine = socket.gethostname()

    # -- file plumbing -------------------------------------------------------
    @property
    def path(self) -> Path:
        return _lock_dir() / f"console-{re.sub(r'[^A-Za-z0-9.-]', '_', self.host)}.lock"

    def _guard(self):
        return _os_lock(self.path.with_suffix(".guard"))

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, info: dict) -> None:
        tmp = self.path.with_suffix(f".{self.token}.tmp")
        tmp.write_text(json.dumps(info), "utf-8")
        os.replace(tmp, self.path)

    def _valid(self, info: dict | None) -> dict | None:
        """The reservation if it still holds: lease not expired and holder process alive."""
        if not info or info.get("expires_at", 0) <= time.time():
            return None
        if info.get("machine") == socket.gethostname() and not pid_alive(int(info.get("pid", 0))):
            return None
        return info

    def _public(self, info: dict) -> dict:
        now = time.time()
        return {
            "owner": info.get("owner"),
            "reason": info.get("reason"),
            "held_by_me": info.get("token") == self.token,
            "held_for_s": round(now - info.get("acquired_at", now)),
            "expires_in_s": round(info.get("expires_at", now) - now),
            "pid": info.get("pid"),
            "machine": info.get("machine"),
        }

    # -- API -----------------------------------------------------------------
    def status(self) -> dict | None:
        """Current holder (None when the console is free)."""
        with self._guard():
            info = self._valid(self._read())
        return self._public(info) if info else None

    def held_by_me(self) -> bool:
        st = self.status()
        return bool(st and st["held_by_me"])

    def acquire(self, reason: str = "", wait_seconds: float = 0.0, *, poll: float = 1.0, keep_reason: bool = False) -> dict:
        """Reserve the console (or renew our reservation). Waits up to wait_seconds for the
        current holder, then raises ConsoleBusy."""
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            with self._guard():
                cur = self._valid(self._read())
                if cur is None or cur.get("token") == self.token:
                    now = time.time()
                    mine = cur is not None
                    info = {
                        "token": self.token,
                        "owner": self.owner,
                        "reason": cur.get("reason", "") if mine and (keep_reason or not reason) else reason,
                        "pid": self.pid,
                        "machine": self.machine,
                        "acquired_at": cur["acquired_at"] if mine else now,
                        "expires_at": now + self.lease_seconds,
                    }
                    self._write(info)
                    return self._public(info)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConsoleBusy(self._public(cur))
            time.sleep(min(poll, remaining))

    def claim(self, reason: str) -> dict:
        """Reserve the console if it is free (keeping our reason if we already hold it); never waits."""
        return self.acquire(reason, keep_reason=True)

    def renew(self) -> bool:
        """Extend our lease. False if we don't hold the console."""
        with self._guard():
            cur = self._read()
            if not cur or cur.get("token") != self.token:
                return False
            cur["expires_at"] = time.time() + self.lease_seconds
            self._write(cur)
            return True

    def release(self, *, force: bool = False) -> bool:
        """Free the console. Only the holder can, unless force=True (to clear a stuck reservation)."""
        with self._guard():
            cur = self._read()
            if not cur or (cur.get("token") != self.token and not force):
                return False
            with contextlib.suppress(OSError):
                self.path.unlink()
            return True
