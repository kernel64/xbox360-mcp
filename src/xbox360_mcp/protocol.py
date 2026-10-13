"""Helpers for the XBDM text protocol (Xbox 360 debug monitor, TCP 730).

XBDM is a line-based protocol. Every response starts with a 3-digit status:

    200- OK                       single-line success
    201- connected                greeting sent on connect
    202- multiline response follows   lines until a lone "."
    203- binary response follows      raw bytes follow (format depends on command)
    204- send binary data         client must now send the payload
    205- now a notification channel
    4xx- ...                      errors
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

OK = 200
CONNECTED = 201
MULTILINE = 202
BINARY = 203
SEND_BINARY = 204
DEDICATED = 205

ERROR_CODES = {
    400: "unexpected error",
    401: "max number of connections exceeded",
    402: "file not found",
    403: "no such module",
    404: "memory not mapped",
    405: "no such thread",
    406: "failed to set system time",
    407: "unknown command",
    408: "not stopped",
    409: "file must be copied",
    410: "file already exists",
    411: "directory not empty",
    412: "filename is invalid",
    413: "file cannot be created",
    414: "access denied",
    415: "no room on device",
    416: "not debuggable",
    417: "type invalid",
    418: "data not available",
    420: "box not locked",
    421: "key exchange required",
    422: "dedicated connection required",
}


class XbdmError(Exception):
    """Error status returned by the console (4xx)."""

    def __init__(self, code: int, message: str, command: str | None = None):
        self.code = code
        self.message = message
        self.command = command
        hint = ERROR_CODES.get(code)
        text = f"XBDM {code}: {message}"
        if hint and hint.lower() not in message.lower():
            text += f" ({hint})"
        if command:
            text += f" [cmd: {command}]"
        super().__init__(text)


class XbdmConnectionError(Exception):
    """The console could not be reached or the socket broke."""


@dataclass
class Response:
    code: int
    message: str
    lines: list[str] | None = None
    data: bytes | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.code < 300


_STATUS_RE = re.compile(r"^(\d{3})- ?(.*)$")


def parse_status(line: str) -> tuple[int, str]:
    m = _STATUS_RE.match(line)
    if not m:
        raise XbdmConnectionError(f"Malformed XBDM status line: {line!r}")
    return int(m.group(1)), m.group(2)


_SEPARATORS = " ,"


def parse_kv(line: str) -> dict[str, object]:
    """Parse `key=value key2="quoted value", flag` into a dict.

    Spaces and commas separate fields (some replies, e.g. `screenshot`, use commas),
    but commas inside quoted values are kept.
    Values that look like integers (0x.. or decimal) are converted to int.
    Bare flags become True.
    """
    out: dict[str, object] = {}
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i] in _SEPARATORS:
            i += 1
        if i >= n:
            break
        j = i
        while j < n and line[j] not in " =,":
            j += 1
        key = line[i:j]
        if j < n and line[j] == "=":
            j += 1
            if j < n and line[j] == '"':
                k = line.find('"', j + 1)
                if k == -1:
                    k = n
                value: object = line[j + 1 : k]
                j = k + 1
            else:
                k = j
                while k < n and line[k] not in _SEPARATORS:
                    k += 1
                raw = line[j:k]
                value = to_int(raw)
                j = k
            out[key] = value
        else:
            if key:
                out[key] = True
        i = j
    return out


def to_int(raw: str) -> object:
    try:
        if raw.lower().startswith("0x"):
            return int(raw, 16)
        if raw.isdigit():
            return int(raw)
    except ValueError:
        pass
    return raw


def join_hilo(d: dict, base: str) -> int | None:
    hi, lo = d.get(base + "hi"), d.get(base + "lo")
    if isinstance(hi, int) and isinstance(lo, int):
        return (hi << 32) | lo
    return None


_FILETIME_EPOCH = _dt.datetime(1601, 1, 1, tzinfo=_dt.timezone.utc)


def filetime_to_iso(ft: int | None) -> str | None:
    if not ft:
        return None
    try:
        return (_FILETIME_EPOCH + _dt.timedelta(microseconds=ft // 10)).isoformat()
    except OverflowError:
        return None


def quote(value: str) -> str:
    if '"' in value:
        raise ValueError(f"XBDM strings cannot contain double quotes: {value!r}")
    return f'"{value}"'


_DRIVE_RE = re.compile(r"^([A-Za-z0-9]+):[\\/]?(.*)$")


def normalize_path(path: str, *, directory: bool = False) -> str:
    """Normalise a console path to XBDM form: `Hdd:\\Apps\\Foo`.

    Accepts forward slashes, `Hdd:/x`, `hdd:\\x`, `/Hdd/x`.
    Directory paths get a trailing backslash (XBDM requires it for drive roots).
    """
    p = path.strip().replace("/", "\\")
    if p.startswith("\\") and ":" not in p:
        # "\Hdd\Apps" -> "Hdd:\Apps"
        parts = p.strip("\\").split("\\", 1)
        p = parts[0] + ":\\" + (parts[1] if len(parts) > 1 else "")
    m = _DRIVE_RE.match(p)
    if not m:
        raise ValueError(
            f"Console path must start with a drive, e.g. 'Hdd:\\Apps\\Game' (got {path!r})"
        )
    drive, rest = m.group(1), m.group(2)
    while "\\\\" in rest:
        rest = rest.replace("\\\\", "\\")
    rest = rest.rstrip("\\")
    out = f"{drive}:\\{rest}"
    if directory and not out.endswith("\\"):
        out += "\\"
    return out


def join_console(base: str, *parts: str) -> str:
    out = normalize_path(base).rstrip("\\")
    for part in parts:
        part = part.replace("/", "\\").strip("\\")
        if part:
            out += "\\" + part
    if out.endswith(":"):
        out += "\\"
    return out


_PROTECTED_DIRS = ("hdd:\\content", "hdd:\\cache")


def is_protected_path(path: str) -> bool:
    """Drive roots, the whole Flash: drive and everything under Hdd:\\Content / Hdd:\\Cache
    (profiles, DLC, title updates, system cache) must not be deleted or moved by mistake."""
    low = normalize_path(path).lower().rstrip("\\")
    if low.endswith(":") or low.startswith("flash:"):
        return True
    return any(low == d or low.startswith(d + "\\") for d in _PROTECTED_DIRS)


# XBDM commands that only read state; used to enforce read-only mode on raw commands.
READ_ONLY_COMMANDS = frozenset(
    {
        "altaddr", "consoletype", "dbgname", "dirlist", "dmversion", "drivefreespace", "drivelist",
        "getconsoleid", "getcontext", "getextcontext", "getfile", "getfileattributes", "getmem",
        "getmemex", "getpid", "isdebugger", "isstopped", "modlong", "modsections", "modules",
        "screenshot", "systeminfo", "systime", "threadinfo", "threads", "walkmem", "xbeinfo",
    }
)


def is_read_only_command(command: str) -> bool:
    verb = command.strip().split(" ", 1)[0].lower()
    return verb in READ_ONLY_COMMANDS


def console_dirname(path: str) -> str:
    p = normalize_path(path)
    head, _, _ = p.rpartition("\\")
    if head.endswith(":"):
        head += "\\"
    return head
