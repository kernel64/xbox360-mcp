"""Synchronous XBDM client for an Xbox 360 RGH/JTAG (or devkit) running xbdm.xex.

Usage:
    from xbox360_mcp.client import XboxConsole
    xbox = XboxConsole("192.168.1.50")
    print(xbox.info())
    for entry in xbox.list_dir("Hdd:\\"):
        print(entry)
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, TypeVar

from .protocol import (
    BINARY,
    CONNECTED,
    MULTILINE,
    SEND_BINARY,
    Response,
    XbdmConnectionError,
    XbdmError,
    filetime_to_iso,
    is_protected_path,
    join_hilo,
    normalize_path,
    parse_kv,
    parse_status,
    quote,
)

T = TypeVar("T")

MEM_READ_CHUNK = 0x1000
MEM_WRITE_CHUNK = 0x80
SEND_CHUNK = 0x10000


def _add_range(ranges: list[tuple[int, int]], start: int, end: int) -> None:
    """Append [start, end) to a sorted range list, merging with the last range if adjacent."""
    if ranges and ranges[-1][1] == start:
        ranges[-1] = (ranges[-1][0], end)
    else:
        ranges.append((start, end))


class XbdmConnection:
    """One TCP connection to XBDM. Not thread-safe; XboxConsole wraps it with a lock."""

    def __init__(self, host: str, port: int = 730, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = bytearray()

    # -- low level -------------------------------------------------------
    def connect(self) -> str:
        self.close()
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            self.sock = None
            raise XbdmConnectionError(
                f"Cannot reach XBDM at {self.host}:{self.port} ({e}). "
                "Is the console on, on the network, and is xbdm.xex loaded (DashLaunch plugin)?"
            ) from e
        greeting = self.readline()
        code, msg = parse_status(greeting)
        if code != CONNECTED:
            raise XbdmConnectionError(f"Unexpected XBDM greeting: {greeting!r}")
        return msg

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def close(self) -> None:
        sock, self.sock = self.sock, None  # may be called from another thread
        if sock is not None:
            try:
                sock.sendall(b"bye\r\n")
            except OSError:
                pass
            try:  # wakes up a recv() blocked in another thread (notification listener)
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._buf.clear()

    def _recv_more(self) -> None:
        if self.sock is None:
            raise XbdmConnectionError("Not connected")
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout as e:
            raise XbdmConnectionError("Timed out waiting for the console") from e
        except OSError as e:
            raise XbdmConnectionError(f"Socket error: {e}") from e
        if not chunk:
            self.sock = None
            raise XbdmConnectionError("Console closed the connection")
        self._buf += chunk

    def readline(self) -> str:
        while True:
            idx = self._buf.find(b"\r\n")
            if idx != -1:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 2]
                return line.decode("latin-1")
            self._recv_more()

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._recv_more()
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def send_line(self, line: str) -> None:
        if self.sock is None:
            raise XbdmConnectionError("Not connected")
        try:
            self.sock.sendall(line.encode("latin-1") + b"\r\n")
        except OSError as e:
            self.sock = None
            raise XbdmConnectionError(f"Send failed: {e}") from e

    def send_bytes(self, data: bytes) -> None:
        if self.sock is None:
            raise XbdmConnectionError("Not connected")
        try:
            view = memoryview(data)
            for i in range(0, len(view), SEND_CHUNK):
                self.sock.sendall(view[i : i + SEND_CHUNK])
        except OSError as e:
            self.sock = None
            raise XbdmConnectionError(f"Send failed: {e}") from e

    def send_stream(self, f: BinaryIO, length: int) -> None:
        """Send exactly `length` bytes read from a file object, without loading it in memory."""
        remaining = length
        while remaining > 0:
            chunk = f.read(min(SEND_CHUNK, remaining))
            if not chunk:
                self.close()
                raise XbdmConnectionError(f"Local file shorter than announced ({length - remaining}/{length} bytes)")
            self.send_bytes(chunk)
            remaining -= len(chunk)

    # -- command level ---------------------------------------------------
    def command(self, cmd: str, *, check: bool = True) -> Response:
        """Send a command and read the status line (+ multiline body for 202).

        For 203 (binary) the caller must read the payload with read_exact/readline.
        For 204 the caller must send the payload and call read_status().
        """
        self.send_line(cmd)
        return self.read_response(cmd, check=check)

    def read_response(self, cmd: str | None = None, *, check: bool = True) -> Response:
        code, msg = parse_status(self.readline())
        if code == MULTILINE:
            lines = []
            while True:
                line = self.readline()
                if line == ".":
                    break
                lines.append(line)
            return Response(code, msg, lines=lines)
        if check and code >= 400:
            raise XbdmError(code, msg, cmd)
        return Response(code, msg)


class XboxConsole:
    """High level, thread-safe API over XBDM with automatic reconnection."""

    def __init__(self, host: str, port: int = 730, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._conn = XbdmConnection(host, port, timeout)
        self._lock = threading.RLock()

    # -- plumbing --------------------------------------------------------
    def _ensure(self) -> XbdmConnection:
        if not self._conn.connected:
            self._conn.connect()
        return self._conn

    def _run(self, fn: Callable[[XbdmConnection], T], *, retry: bool = True) -> T:
        with self._lock:
            try:
                return fn(self._ensure())
            except XbdmConnectionError:
                self._conn.close()
                if not retry:
                    raise
                time.sleep(0.3)
                return fn(self._ensure())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        try:
            self.cmd("dbgname")
            return True
        except (XbdmConnectionError, XbdmError):
            return False

    def cmd(self, command: str, *, retry: bool = True, check: bool = True) -> Response:
        return self._run(lambda c: c.command(command, check=check), retry=retry)

    def raw(self, command: str, binary_timeout: float = 2.0, max_binary: int = 1 << 20) -> dict:
        """Send any XBDM command. 203 payloads are read until the socket goes quiet."""

        def go(c: XbdmConnection) -> dict:
            resp = c.command(command, check=False)
            out: dict = {"code": resp.code, "message": resp.message}
            if resp.lines is not None:
                out["lines"] = resp.lines
            if resp.code == BINARY:
                assert c.sock is not None
                old = c.sock.gettimeout()
                c.sock.settimeout(binary_timeout)
                data = bytearray(c._buf)
                c._buf.clear()
                try:
                    while len(data) < max_binary:
                        chunk = c.sock.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                except (socket.timeout, OSError):
                    pass
                c.sock.settimeout(old)
                out["binary_length"] = len(data)
                out["binary_hex_preview"] = bytes(data[:256]).hex()
                # binary payload framing is command specific: reset the connection
                c.close()
            return out

        return self._run(go, retry=False)

    def _text(self, command: str) -> str:
        return self.cmd(command).message

    def _lines(self, command: str) -> list[str]:
        resp = self.cmd(command)
        return resp.lines or []

    def _kv_lines(self, command: str) -> list[dict]:
        return [parse_kv(line) for line in self._lines(command)]

    def _kv_merged(self, command: str) -> dict:
        merged: dict = {}
        for d in self._kv_lines(command):
            merged.update(d)
        return merged

    # -- console info ----------------------------------------------------
    def name(self) -> str:
        return self._text("dbgname")

    def console_type(self) -> str | None:
        try:
            return self._text("consoletype")
        except XbdmError:
            return None

    def system_info(self) -> dict:
        try:
            return self._kv_merged("systeminfo")
        except XbdmError:
            return {}

    def running_title(self) -> dict:
        try:
            return self._kv_merged("xbeinfo running")
        except XbdmError:
            return {}

    def info(self) -> dict:
        info: dict = {"host": self.host, "port": self.port, "name": self.name()}
        info["console_type"] = self.console_type()
        info["system"] = self.system_info()
        title = self.running_title()
        info["running_title"] = title.get("name", title or None)
        return info

    # -- filesystem ------------------------------------------------------
    def drives(self) -> list[str]:
        return [str(d.get("drivename")) for d in self._kv_lines("drivelist") if d.get("drivename")]

    def free_space(self, drive: str) -> dict:
        d = self._kv_merged(f"drivefreespace name={quote(normalize_path(drive, directory=True))}")
        return {
            "free_bytes": join_hilo(d, "freetocaller"),
            "total_bytes": join_hilo(d, "totalbytes"),
            "total_free_bytes": join_hilo(d, "totalfreebytes"),
        }

    @staticmethod
    def _entry(d: dict) -> dict:
        return {
            "name": d.get("name"),
            "is_dir": bool(d.get("directory")),
            "size": join_hilo(d, "size") or 0,
            "created": filetime_to_iso(join_hilo(d, "create")),
            "modified": filetime_to_iso(join_hilo(d, "change")),
        }

    def list_dir(self, path: str) -> list[dict]:
        p = normalize_path(path, directory=True)
        entries = [self._entry(d) for d in self._kv_lines(f"dirlist name={quote(p)}")]
        entries.sort(key=lambda e: (not e["is_dir"], str(e["name"]).lower()))
        return entries

    def stat(self, path: str) -> dict | None:
        p = normalize_path(path)
        try:
            d = self._kv_merged(f"getfileattributes name={quote(p)}")
        except XbdmError as e:
            if e.code == 402:
                return None
            raise
        e = self._entry(d)
        e["name"] = p
        return e

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    def download(self, path: str, offset: int | None = None, size: int | None = None) -> bytes:
        p = normalize_path(path)
        command = f"getfile name={quote(p)}"
        if offset is not None or size is not None:
            command += f" offset=0x{offset or 0:x}"
            if size is not None:
                command += f" size=0x{size:x}"

        def go(c: XbdmConnection) -> bytes:
            resp = c.command(command)
            if resp.code != BINARY:
                raise XbdmError(resp.code, f"expected binary response, got {resp.message}", command)
            (length,) = struct.unpack("<I", c.read_exact(4))
            return c.read_exact(length)

        return self._run(go)

    def _sendfile(self, path: str, length: int, send: Callable[[XbdmConnection], None]) -> None:
        p = normalize_path(path)
        command = f"sendfile name={quote(p)} length=0x{length:x}"

        def go(c: XbdmConnection) -> None:
            resp = c.command(command)
            if resp.code != SEND_BINARY:
                raise XbdmError(resp.code, f"expected 204, got {resp.message}", command)
            send(c)
            c.read_response(command)

        self._run(go, retry=False)

    def upload(self, path: str, data: bytes) -> None:
        self._sendfile(path, len(data), lambda c: c.send_bytes(data))

    def upload_file(self, path: str, local_path: str | Path) -> int:
        """Stream a local file to the console (constant memory, fine for multi-GB files)."""
        local = Path(local_path)
        size = local.stat().st_size
        with open(local, "rb") as f:
            self._sendfile(path, size, lambda c: c.send_stream(f, size))
        return size

    def mkdir(self, path: str, *, exist_ok: bool = True) -> None:
        try:
            self.cmd(f"mkdir name={quote(normalize_path(path))}")
        except XbdmError as e:
            if not (exist_ok and e.code == 410):
                raise

    def makedirs(self, path: str) -> None:
        p = normalize_path(path)
        drive, _, rest = p.partition(":\\")
        cur = drive + ":\\"
        for part in [x for x in rest.split("\\") if x]:
            cur = cur.rstrip("\\") + "\\" + part
            self.mkdir(cur, exist_ok=True)

    @staticmethod
    def _check_protected(path: str, force: bool) -> None:
        if not force and is_protected_path(path):
            raise PermissionError(
                f"Refusing to modify protected path {path} (drive root, Flash:, Hdd:\\Content, Hdd:\\Cache); "
                "pass force=True if you really mean it"
            )

    def delete(self, path: str, *, recursive: bool = False, force: bool = False) -> int:
        """Delete a file or directory. Returns number of entries deleted.
        Protected system locations (see is_protected_path) need force=True."""
        p = normalize_path(path)
        self._check_protected(p, force)
        st = self.stat(p)
        if st is None:
            raise XbdmError(402, "file not found", p)
        if not st["is_dir"]:
            self.cmd(f"delete name={quote(p)}")
            return 1
        count = 0
        if recursive:
            for e in self.list_dir(p):
                count += self.delete(p.rstrip("\\") + "\\" + str(e["name"]), recursive=True, force=force)
        self.cmd(f"delete name={quote(p)} dir")
        return count + 1

    def rename(self, src: str, dst: str, *, force: bool = False) -> None:
        s, d = normalize_path(src), normalize_path(dst)
        self._check_protected(s, force)
        self._check_protected(d, force)
        self.cmd(f"rename name={quote(s)} newname={quote(d)}")

    def walk(self, path: str) -> Iterable[tuple[str, dict]]:
        root = normalize_path(path)
        for e in self.list_dir(root):
            full = root.rstrip("\\") + "\\" + str(e["name"])
            yield full, e
            if e["is_dir"]:
                yield from self.walk(full)

    # -- titles / power --------------------------------------------------
    def launch(self, xex_path: str, directory: str | None = None) -> None:
        p = normalize_path(xex_path)
        d = normalize_path(directory, directory=True) if directory else p.rpartition("\\")[0] + "\\"
        command = f"magicboot title={quote(p)} directory={quote(d)}"
        self._fire_and_forget(command)

    def reboot(self, cold: bool = False) -> None:
        self._fire_and_forget("magicboot cold" if cold else "magicboot")

    def _fire_and_forget(self, command: str) -> None:
        """For commands after which the console drops the connection."""
        with self._lock:
            try:
                c = self._ensure()
                c.command(command)
            except XbdmConnectionError:
                pass  # the console often drops the socket immediately
            finally:
                self._conn.close()

    # -- modules / threads / debugging -----------------------------------
    def modules(self) -> list[dict]:
        return self._kv_lines("modules")

    def module_sections(self, name: str) -> list[dict]:
        return self._kv_lines(f"modsections name={quote(name)}")

    def resolve_address(self, address: int, modules: list[dict] | None = None) -> str:
        for m in modules if modules is not None else self.modules():
            base, size = m.get("base"), m.get("size")
            if isinstance(base, int) and isinstance(size, int) and base <= address < base + size:
                return f"{m.get('name')}+0x{address - base:x}"
        return f"0x{address:08x}"

    def threads(self) -> list[int]:
        out = []
        for line in self._lines("threads"):
            v = line.strip()
            try:
                # retail-based xbdm prints ids as signed decimal ("-83886072" == 0xFB000008)
                out.append(int(v, 0) & 0xFFFFFFFF)
            except ValueError:
                continue
        return out

    def thread_info(self, thread: int) -> dict:
        return self._kv_merged(f"threadinfo thread=0x{thread:x}")

    def thread_context(self, thread: int) -> dict:
        return self._kv_merged(f"getcontext thread=0x{thread:x} control int fp")

    def is_stopped(self, thread: int) -> tuple[bool, str]:
        resp = self.cmd(f"isstopped thread=0x{thread:x}", check=False)
        if resp.code >= 400:
            return False, resp.message
        return True, resp.message

    def stop(self) -> None:
        self.cmd("stop")

    def go(self) -> None:
        self.cmd("go")

    def continue_thread(self, thread: int, exception: bool = False) -> None:
        self.cmd(f"continue thread=0x{thread:x}" + (" exception" if exception else ""))

    def set_breakpoint(self, address: int) -> None:
        self.cmd(f"break addr=0x{address:08x}")

    def clear_breakpoint(self, address: int) -> None:
        self.cmd(f"break addr=0x{address:08x} clear")

    def clear_all_breakpoints(self) -> None:
        self.cmd("break clearall")

    # -- memory ----------------------------------------------------------
    def memory_map(self) -> list[dict]:
        return self._kv_lines("walkmem")

    def read_memory(self, address: int, length: int) -> tuple[bytes, list[tuple[int, int]]]:
        """Read memory. Returns (data, unreadable_ranges). Unreadable bytes are 0."""
        out = bytearray()
        bad: list[tuple[int, int]] = []
        pos = address
        end = address + length
        while pos < end:
            n = min(MEM_READ_CHUNK, end - pos)
            lines = self._lines(f"getmem addr=0x{pos:08x} length=0x{n:x}")
            hexstr = "".join(line.strip() for line in lines)
            got = min(len(hexstr) // 2, n)
            hexstr = hexstr[: got * 2]
            if "?" not in hexstr:  # fast path: fully mapped chunk
                out += bytes.fromhex(hexstr)
            else:
                i = 0
                while i < got:
                    j = hexstr.find("??", i * 2)
                    while j != -1 and j % 2:  # only byte-aligned "??"
                        j = hexstr.find("??", j + 1)
                    if j == -1:
                        out += bytes.fromhex(hexstr[i * 2 :])
                        break
                    out += bytes.fromhex(hexstr[i * 2 : j])
                    k = j
                    while k < len(hexstr) and hexstr[k : k + 2] == "??":
                        k += 2
                    _add_range(bad, pos + j // 2, pos + k // 2)
                    out += bytes((k - j) // 2)
                    i = k // 2
            if got < n:  # short read: pad as unreadable
                _add_range(bad, pos + got, pos + n)
                out += bytes(n - got)
            pos += n
        return bytes(out), bad

    def read_u32(self, address: int) -> int | None:
        data, bad = self.read_memory(address, 4)
        return None if bad else struct.unpack(">I", data)[0]

    def write_memory(self, address: int, data: bytes) -> None:
        for i in range(0, len(data), MEM_WRITE_CHUNK):
            chunk = data[i : i + MEM_WRITE_CHUNK]
            self.cmd(f"setmem addr=0x{address + i:08x} data={chunk.hex()}")

    # -- screenshot ------------------------------------------------------
    def screenshot_raw(self) -> tuple[dict, bytes]:
        def go(c: XbdmConnection) -> tuple[dict, bytes]:
            resp = c.command("screenshot")
            if resp.code != BINARY:
                raise XbdmError(resp.code, f"expected binary response, got {resp.message}", "screenshot")
            assert c.sock is not None
            c.sock.settimeout(max(self.timeout, 30.0))
            try:
                header = parse_kv(c.readline())
                size = header.get("framebuffersize")
                if not isinstance(size, int):
                    raise XbdmError(400, f"screenshot header without framebuffersize: {header}")
                return header, c.read_exact(size)
            finally:
                if c.sock is not None:
                    c.sock.settimeout(self.timeout)

        return self._run(go)
