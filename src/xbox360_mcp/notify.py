"""Background XBDM notification channel: captures OutputDebugString, exceptions,
breakpoints, module loads and execution state changes into a ring buffer.

The channel survives title launches / reboots by reconnecting automatically.
"""

from __future__ import annotations

import collections
import re
import threading
import time
from dataclasses import asdict, dataclass, field

from .client import XbdmConnection
from .protocol import DEDICATED, XbdmConnectionError, parse_kv, parse_status


@dataclass
class Event:
    id: int
    time: float
    type: str
    text: str
    fields: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time"] = time.strftime("%H:%M:%S", time.localtime(self.time)) + f".{int(self.time * 1000) % 1000:03d}"
        if not d["fields"]:
            del d["fields"]
        return d


def parse_notification(line: str) -> tuple[str, str, dict]:
    """Return (type, human_text, fields) for one notification line."""
    kind, _, rest = line.partition(" ")
    kind = kind.lower()
    if kind == "debugstr":
        idx = rest.find("string=")
        head = rest[:idx] if idx != -1 else rest
        text = rest[idx + 7 :] if idx != -1 else ""
        fields = parse_kv(head)
        return "debug", text.rstrip("\r\n"), fields
    if kind == "execution":
        return "execution", rest.strip(), {}
    fields = parse_kv(rest)
    if kind in ("exception", "break", "singlestep", "data", "assert", "rip", "fce"):
        return "stop" if kind != "exception" else "exception", line, fields
    return kind or "other", line, fields


class NotificationListener:
    def __init__(
        self,
        host: str,
        port: int = 730,
        *,
        capacity: int = 20000,
        attach_debugger: bool = True,
        log_file: str | None = None,
    ):
        self.host = host
        self.port = port
        self.attach_debugger = attach_debugger
        self.log_file = log_file
        self._events: collections.deque[Event] = collections.deque(maxlen=capacity)
        self._next_id = 1
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False
        self.last_error: str | None = None
        self._conn: XbdmConnection | None = None
        self._log = None  # file handle, opened lazily and kept open

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xbdm-notify", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._conn:
            self._conn.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        backoff = 1.0
        was_connected = False
        while not self._stop.is_set():
            conn = XbdmConnection(self.host, self.port, timeout=5.0)
            self._conn = conn
            try:
                conn.connect()
                if self.attach_debugger:
                    try:
                        conn.command('debugger connect override name="xbox360-mcp" user="claude"', check=False)
                    except XbdmConnectionError:
                        raise
                # real xbdm answers a bare `notify` with "400- missing reconnectport". reconnectport
                # is where the console would dial back after a reboot; we reconnect ourselves instead.
                resp = conn.command(f"notify reconnectport=0x{self.port:x} reverse", check=False)
                if resp.code >= 400:
                    resp = conn.command("notify", check=False)
                if resp.code != DEDICATED:
                    raise XbdmConnectionError(f"notify refused: {resp.code} {resp.message}")
                assert conn.sock is not None
                conn.sock.settimeout(None)
                self.connected = True
                self.last_error = None
                backoff = 1.0
                self._push("mcp", "notification channel " + ("reconnected" if was_connected else "connected"), {})
                was_connected = True
                while not self._stop.is_set():
                    line = conn.readline()
                    if line:
                        kind, text, fields = parse_notification(line)
                        self._push(kind, text, fields)
            except (XbdmConnectionError, OSError) as e:
                if self.connected:
                    self._push("mcp", f"notification channel lost: {e}", {})
                self.last_error = str(e)
            finally:
                self.connected = False
                conn.close()
            self._stop.wait(backoff)
            backoff = min(backoff * 1.5, 5.0)

    # -- buffer ------------------------------------------------------------
    def _push(self, kind: str, text: str, fields: dict) -> None:
        with self._cond:
            ev = Event(self._next_id, time.time(), kind, text, fields)
            self._next_id += 1
            self._events.append(ev)
            self._cond.notify_all()
        if self.log_file:
            try:
                if self._log is None:
                    self._log = open(self.log_file, "a", encoding="utf-8", errors="replace", buffering=1)
                self._log.write(f"{ev.to_dict()['time']} [{kind}] {text}\n")
            except (OSError, ValueError):
                self._log = None

    @property
    def next_id(self) -> int:
        return self._next_id

    def clear(self) -> None:
        with self._cond:
            self._events.clear()

    def query(
        self,
        since_id: int = 0,
        limit: int = 200,
        contains: str | None = None,
        regex: str | None = None,
        types: list[str] | None = None,
        tail: bool = True,
    ) -> list[Event]:
        rx = re.compile(regex) if regex else None
        with self._cond:
            evs = [e for e in self._events if e.id > since_id]
        if types:
            evs = [e for e in evs if e.type in types]
        if contains:
            low = contains.lower()
            evs = [e for e in evs if low in e.text.lower()]
        if rx:
            evs = [e for e in evs if rx.search(e.text)]
        return evs[-limit:] if tail else evs[:limit]

    def wait_for(self, pattern: str, timeout: float, since_id: int | None = None, types: list[str] | None = None) -> Event | None:
        rx = re.compile(pattern)
        start_id = self._next_id - 1 if since_id is None else since_id
        deadline = time.time() + timeout
        seen = start_id
        with self._cond:
            while True:
                for e in self._events:
                    if e.id <= seen:
                        continue
                    seen = e.id
                    if types and e.type not in types:
                        continue
                    if rx.search(e.text):
                        return e
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
