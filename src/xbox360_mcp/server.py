"""MCP server exposing an Xbox 360 RGH (xbdm) to Claude Code.

Run:  XBOX360_HOST=192.168.1.50 xbox360-mcp
"""

from __future__ import annotations

import functools
import os

import time
from pathlib import Path
from typing import Literal

# mcp >= 2 runs sync tools in a worker thread, so blocking tools (wait_for_log, reboot)
# don't freeze the server
from mcp.server.mcpserver import Image, MCPServer as _Server
from mcp.types import ToolAnnotations

from . import analysis, deploy as deploy_mod, jrpc, ppc
from .client import XboxConsole
from .config import Config
from .framebuffer import to_png
from .notify import NotificationListener
from .protocol import XbdmConnectionError, XbdmError, is_read_only_command, normalize_path

INSTRUCTIONS = """\
Tools to drive an Xbox 360 RGH/JTAG running xbdm (DashLaunch plugin) over the LAN.

Typical homebrew / emulator loop:
1. Build on the PC, then `xbox_deploy` the build folder (incremental, only changed files are sent)
   and launch the .xex in the same call (launch=true, wait_for="<log line printed at init>").
2. Read what the title prints with OutputDebugString/DbgPrint via `xbox_debug_log` (use since_id
   from the previous call to only get new lines) or block on `xbox_wait_for_log`.
3. If the title hangs or crashes: `xbox_crash_report` (stopped threads, registers, PC symbolised
   as module+offset, back-trace, disassembly). Then `xbox_execution go` to resume, or reboot.
4. `xbox_screenshot` to see the TV output.

Console paths look like `Hdd:\\Apps\\MyGame\\default.xex` (forward slashes accepted).
Local paths are on the PC running this server; prefer absolute paths.
Memory addresses are hex strings (`0x82000000`) or ints. Titles usually load at 0x82000000.
`xbox_raw_command` sends any XBDM command when no dedicated tool exists.
"""

cfg = Config.from_env()
xbox = XboxConsole(cfg.host, cfg.port, cfg.timeout)
notifier = NotificationListener(cfg.host, cfg.port, attach_debugger=cfg.attach_debugger, log_file=cfg.log_file)

mcp = _Server("xbox360", instructions=INSTRUCTIONS)

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _addr(v: str | int) -> int:
    if isinstance(v, int):
        return v
    # Strings are always hexadecimal (with or without 0x): "82000000" == 0x82000000.
    s = v.strip().lower().replace("_", "").replace("`", "")
    return int(s[2:] if s.startswith("0x") else s, 16)


def _guard_write() -> None:
    if cfg.read_only:
        raise PermissionError("Server is in read-only mode (XBOX360_READ_ONLY=1).")


def _local(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser().resolve()


def friendly(fn):
    """Turn transport errors into actionable messages."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except XbdmConnectionError as e:
            raise RuntimeError(
                f"{e}\nCheck: console powered on, same LAN, IP {cfg.host} correct, xbdm.xex loaded by DashLaunch."
            ) from e

    return wrapper


def _ensure_notifier() -> None:
    if not notifier.running:
        notifier.start()


# ---------------------------------------------------------------------------
# Console status
# ---------------------------------------------------------------------------
@mcp.tool(annotations=RO)
@friendly
def xbox_status(include_temperatures: bool = True) -> dict:
    """Console name, type, kernel/system info, running title, debug-log channel state,
    and (if the JRPC2 plugin is loaded) temperatures, title ID and kernel version."""
    _ensure_notifier()
    info = xbox.info()
    info["debug_channel"] = {
        "connected": notifier.connected,
        "events_buffered_until_id": notifier.next_id - 1,
        "last_error": notifier.last_error,
        "log_file": cfg.log_file,
        "debugger_attached": cfg.attach_debugger,
    }
    kv = jrpc.kernel_version(xbox)
    if kv is not None:
        info["jrpc2"] = {"kernel_version": kv, "title_id": jrpc.title_id(xbox), "motherboard": jrpc.motherboard(xbox)}
        if include_temperatures:
            info["jrpc2"]["temperatures_c"] = jrpc.temperatures(xbox)
    else:
        info["jrpc2"] = "not loaded (optional)"
    info["read_only_mode"] = cfg.read_only
    return info


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------
@mcp.tool(annotations=RO)
@friendly
def xbox_drives() -> list[dict]:
    """List mounted drives (Hdd, Usb0, Flash, Game, ...) with free/total space."""
    out = []
    for d in xbox.drives():
        entry: dict = {"drive": d + ":\\"}
        try:
            entry.update(xbox.free_space(d + ":\\"))
        except XbdmError as e:
            entry["error"] = str(e)
        out.append(entry)
    return out


@mcp.tool(annotations=RO)
@friendly
def xbox_list_dir(path: str = "Hdd:\\", recursive: bool = False, max_entries: int = 500) -> dict:
    """List a console directory. With recursive=true walks sub-folders (capped by max_entries)."""
    if not recursive:
        entries = xbox.list_dir(path)
        return {"path": normalize_path(path, directory=True), "entries": entries[:max_entries], "total": len(entries)}
    out = []
    for full, e in xbox.walk(path):
        out.append({"path": full, "is_dir": e["is_dir"], "size": e["size"], "modified": e["modified"]})
        if len(out) >= max_entries:
            break
    return {"path": normalize_path(path), "entries": out, "truncated": len(out) >= max_entries}


@mcp.tool(annotations=RO)
@friendly
def xbox_download(remote_path: str, local_path: str) -> dict:
    """Copy a file (or a whole folder) from the console to the PC."""
    dst = _local(local_path)
    st = xbox.stat(remote_path)
    if st is None:
        raise FileNotFoundError(remote_path)
    files = []
    if st["is_dir"]:
        root = normalize_path(remote_path)
        for full, e in xbox.walk(root):
            rel = full[len(root) :].lstrip("\\").replace("\\", os.sep)
            target = dst / rel
            if e["is_dir"]:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(xbox.download(full))
                files.append(str(target))
    else:
        if dst.is_dir():
            dst = dst / normalize_path(remote_path).rpartition("\\")[2]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(xbox.download(remote_path))
        files.append(str(dst))
    return {"downloaded": len(files), "files": files[:100]}


@mcp.tool(annotations=RO)
@friendly
def xbox_read_text_file(remote_path: str, max_bytes: int = 65536, tail: bool = False) -> str:
    """Read a text file on the console (e.g. a log your homebrew writes to Hdd:\\ or Usb0:\\).
    tail=true returns the last max_bytes."""
    st = xbox.stat(remote_path)
    if st is None:
        raise FileNotFoundError(remote_path)
    size = st["size"]
    if size <= max_bytes:
        data = xbox.download(remote_path)
    elif tail:
        data = xbox.download(remote_path, offset=size - max_bytes, size=max_bytes)
    else:
        data = xbox.download(remote_path, offset=0, size=max_bytes)
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = f"[{size} bytes, showing {'last' if tail else 'first'} {max_bytes}]\n" + text
    return text


@mcp.tool(annotations=WRITE)
@friendly
def xbox_upload(local_path: str, remote_path: str) -> dict:
    """Upload a PC file to an exact console path, or a PC folder into a console folder
    (always sends every file; use xbox_deploy for incremental builds)."""
    _guard_write()
    src = _local(local_path)
    if src.is_dir():
        return deploy_mod.deploy(xbox, str(src), remote_path, excludes=[], force=True)
    target = normalize_path(remote_path)
    st = xbox.stat(target)
    if st and st["is_dir"]:
        target = target.rstrip("\\") + "\\" + src.name
    xbox.makedirs(target.rpartition("\\")[0])
    return {"uploaded": target, "bytes": xbox.upload_file(target, src)}


@mcp.tool(annotations=WRITE)
@friendly
def xbox_mkdir(path: str) -> str:
    """Create a console directory (parents included)."""
    _guard_write()
    xbox.makedirs(path)
    return f"created {normalize_path(path)}"


@mcp.tool(annotations=DESTRUCTIVE)
@friendly
def xbox_rename(src: str, dst: str) -> str:
    """Rename / move a file or folder on the same console drive (system locations refused)."""
    _guard_write()
    xbox.rename(src, dst)
    return f"{normalize_path(src)} -> {normalize_path(dst)}"


@mcp.tool(annotations=DESTRUCTIVE)
@friendly
def xbox_delete(path: str, recursive: bool = False) -> str:
    """Delete a console file or folder. Folders need recursive=true unless empty.
    Refuses drive roots, Flash: and anything under Hdd:\\Content or Hdd:\\Cache
    (the user can still do it from the CLI with `xbox360 rm --force`)."""
    _guard_write()
    p = normalize_path(path)
    n = xbox.delete(p, recursive=recursive)
    return f"deleted {n} entr{'y' if n == 1 else 'ies'} at {p}"


# ---------------------------------------------------------------------------
# Deploy / launch / power
# ---------------------------------------------------------------------------
def _launch_and_watch(xex: str, wait_for: str | None, timeout: float, capture_seconds: float) -> dict:
    _ensure_notifier()
    start_id = notifier.next_id - 1
    xbox.launch(xex)
    out: dict = {"launched": normalize_path(xex), "log_since_id": start_id}
    if wait_for:
        ev = notifier.wait_for(wait_for, timeout, since_id=start_id)
        out["wait_for"] = {"pattern": wait_for, "matched": ev.to_dict() if ev else None, "timed_out": ev is None}
        crash = notifier.query(since_id=start_id, types=["exception"], limit=5)
        if crash:
            out["exceptions"] = [e.to_dict() for e in crash]
    elif capture_seconds > 0:
        time.sleep(capture_seconds)
    if wait_for or capture_seconds > 0:
        evs = notifier.query(since_id=start_id, limit=150)
        out["log"] = [f"#{e.id} [{e.type}] {e.text}" for e in evs]
        out["next_since_id"] = notifier.next_id - 1
    return out


@mcp.tool(annotations=WRITE)
@friendly
def xbox_launch(
    xex_path: str,
    wait_for: str | None = None,
    timeout_seconds: float = 30,
    capture_seconds: float = 0,
) -> dict:
    """Launch a .xex on the console (magicboot). Optionally block until a debug-log line matches
    the regex `wait_for` (returns the log captured since launch), or just capture logs for N seconds."""
    _guard_write()
    return _launch_and_watch(xex_path, wait_for, timeout_seconds, capture_seconds)


@mcp.tool(annotations=WRITE)
@friendly
def xbox_deploy(
    local_dir: str,
    remote_dir: str | None = None,
    launch: bool = False,
    xex_name: str = "default.xex",
    wait_for: str | None = None,
    timeout_seconds: float = 30,
    capture_seconds: float = 3,
    exclude: list[str] | None = None,
    force: bool = False,
    prune: bool = False,
) -> dict:
    """Incrementally sync a PC build folder to the console (only changed files are sent), then
    optionally launch `xex_name` from it and return the first log lines.
    remote_dir defaults to <XBOX360_DEPLOY_ROOT>\\<local folder name>.
    exclude: glob patterns (default skips *.pdb, *.obj, .git, ...).
    prune=true deletes console files missing locally (including files the title wrote, e.g. saves)."""
    _guard_write()
    src = _local(local_dir)
    remote = remote_dir or (cfg.default_deploy_root.rstrip("\\") + "\\" + src.name)
    result = deploy_mod.deploy(xbox, str(src), remote, excludes=exclude, force=force, prune=prune)
    for k in ("uploaded", "deleted"):
        if len(result.get(k) or []) > 50:
            result[k] = result[k][:50] + [f"... +{len(result[k]) - 50} more"]
    if launch:
        xex = normalize_path(result["remote_dir"]).rstrip("\\") + "\\" + xex_name
        result["run"] = _launch_and_watch(xex, wait_for, timeout_seconds, capture_seconds)
    return result


@mcp.tool(annotations=DESTRUCTIVE)
@friendly
def xbox_reboot(cold: bool = False, wait_until_back: bool = True, timeout_seconds: float = 90) -> dict:
    """Reboot the console back to the dashboard (warm) or full power cycle (cold=true).
    Waits until xbdm answers again."""
    _guard_write()
    xbox.reboot(cold=cold)
    out: dict = {"rebooting": "cold" if cold else "warm"}
    if wait_until_back:
        time.sleep(3)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if xbox.ping():
                out["back_online_after_s"] = round(timeout_seconds - (deadline - time.time()) + 3, 1)
                break
            time.sleep(2)
        else:
            out["warning"] = "console did not answer before timeout"
    return out


# ---------------------------------------------------------------------------
# Debug output
# ---------------------------------------------------------------------------
@mcp.tool(annotations=RO)
def xbox_debug_log(
    since_id: int = 0,
    limit: int = 200,
    contains: str | None = None,
    regex: str | None = None,
    types: list[str] | None = None,
) -> dict:
    """Debug output captured from the console in the background (OutputDebugString / DbgPrint,
    exceptions, breakpoints, module loads, execution state). Pass the returned `next_since_id`
    back as since_id to only get new lines. types filter: debug, exception, stop, execution,
    modload, mcp, ..."""
    _ensure_notifier()
    evs = notifier.query(since_id=since_id, limit=limit, contains=contains, regex=regex, types=types)
    return {
        "channel_connected": notifier.connected,
        "lines": [f"#{e.id} {e.to_dict()['time']} [{e.type}] {e.text}" for e in evs],
        "next_since_id": notifier.next_id - 1,
    }


@mcp.tool(annotations=RO)
def xbox_wait_for_log(pattern: str, timeout_seconds: float = 30, since_id: int | None = None) -> dict:
    """Block until a debug line matching the regex appears (or timeout). since_id=None waits for
    new lines only."""
    _ensure_notifier()
    ev = notifier.wait_for(pattern, timeout_seconds, since_id=since_id)
    return {"matched": ev.to_dict() if ev else None, "timed_out": ev is None, "next_since_id": notifier.next_id - 1}


@mcp.tool(annotations=WRITE)
def xbox_clear_log() -> str:
    """Clear the captured debug-log buffer (ids keep increasing)."""
    notifier.clear()
    return "cleared"


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------
@mcp.tool(annotations=RO)
@friendly
def xbox_screenshot(
    save_path: str | None = None,
    channel_order: str = "BGRA",
    tiled: bool = True,
    max_width: int = 1280,
):
    """Capture what the console displays. Returns the image (and saves a PNG on the PC if
    save_path is given). BGRA is right for a real console; if colours look wrong try ARGB/RGBA/ABGR; if the picture
    looks scrambled into blocks toggle tiled."""
    header, raw = xbox.screenshot_raw()
    png, meta = to_png(header, raw, channel_order=channel_order, tiled=tiled, max_width=max_width)
    if save_path:
        p = _local(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(png)
        meta["saved"] = str(p)
    meta["header"] = {k: (hex(v) if isinstance(v, int) else v) for k, v in header.items()}
    return [Image(data=png, format="png"), str(meta)]


# ---------------------------------------------------------------------------
# Memory / debugging
# ---------------------------------------------------------------------------
@mcp.tool(annotations=RO)
@friendly
def xbox_read_memory(
    address: str,
    length: int = 256,
    format: Literal["hexdump", "hex", "u16", "u32", "f32", "string", "utf16"] = "hexdump",
) -> str:
    """Read console memory (big-endian). Max 64 KiB per call. Unmapped bytes show as ??."""
    length = max(1, min(length, 0x10000))
    a = _addr(address)
    data, bad = xbox.read_memory(a, length)
    text = analysis.format_memory(data, a, format, bad)
    if bad and format != "hexdump":
        text += f"\n(unreadable: {[(hex(s), hex(e)) for s, e in bad][:10]})"
    return text


@mcp.tool(annotations=DESTRUCTIVE)
@friendly
def xbox_write_memory(address: str, hex_data: str) -> str:
    """Write bytes into console memory, e.g. hex_data='60000000' (a PPC nop)."""
    _guard_write()
    data = bytes.fromhex(hex_data.replace(" ", "").replace("0x", ""))
    a = _addr(address)
    xbox.write_memory(a, data)
    return f"wrote {len(data)} bytes at 0x{a:08x}"


@mcp.tool(annotations=RO)
@friendly
def xbox_find_bytes(start: str, length: int, pattern: str, max_hits: int = 32) -> list[str]:
    """Search a memory range (max 64 MiB) for a hex pattern with ?? wildcards,
    e.g. pattern='7C 08 02 A6 ?? ?? ?? ??'."""
    length = min(length, 64 << 20)
    hits = analysis.find_bytes(xbox, _addr(start), length, pattern, max_hits)
    mods = xbox.modules()
    return [f"0x{h:08x}  {xbox.resolve_address(h, mods)}" for h in hits]


@mcp.tool(annotations=RO)
@friendly
def xbox_disassemble(address: str, count: int = 32, context_before: int = 0) -> str:
    """Disassemble PowerPC code from console memory (capstone; VMX128 shows as .long).
    context_before = number of instructions to show before `address` (marked =>)."""
    if not ppc.has_disassembler():
        return "capstone not installed: pip install capstone"
    return "\n".join(analysis.disassemble_at(xbox, _addr(address), min(count, 512), context_before))


@mcp.tool(annotations=RO)
@friendly
def xbox_modules(include_sections: str | None = None) -> dict:
    """Loaded modules (name, base, size). include_sections=<module name> also lists its sections."""
    mods = xbox.modules()
    out: dict = {
        "modules": [
            {k: (f"0x{v:08x}" if isinstance(v, int) and k in ("base", "size", "check", "timestamp") else v) for k, v in m.items()}
            for m in mods
        ]
    }
    if include_sections:
        out["sections"] = xbox.module_sections(include_sections)
    return out


@mcp.tool(annotations=RO)
@friendly
def xbox_memory_map() -> list[str]:
    """Committed virtual memory regions (walkmem): base, size, protection."""
    out = []
    for r in xbox.memory_map():
        base, size, prot = r.get("base"), r.get("size"), r.get("protect")
        out.append(
            f"0x{base:08x}-0x{base + size:08x} size=0x{size:x} protect=0x{prot:x}" if isinstance(base, int) and isinstance(size, int) and isinstance(prot, int) else str(r)
        )
    return out


@mcp.tool(annotations=RO)
@friendly
def xbox_threads() -> list[dict]:
    """Threads of the running title with start address, priority, stopped state."""
    mods = xbox.modules()
    out = []
    for t in xbox.threads():
        entry: dict = {"thread": f"0x{t:08x}"}
        try:
            info = xbox.thread_info(t)
            for k in ("start", "priority", "suspend", "tlsbase", "stackbase", "stacklimit"):
                if k in info:
                    v = info[k]
                    entry[k] = xbox.resolve_address(v, mods) if k == "start" and isinstance(v, int) else v
        except XbdmError as e:
            entry["error"] = str(e)
        stopped, reason = xbox.is_stopped(t)
        entry["stopped"] = stopped
        if stopped:
            entry["stop_reason"] = reason
        out.append(entry)
    return out


@mcp.tool(annotations=RO)
@friendly
def xbox_thread_state(thread: str) -> dict:
    """Registers, PC/LR as module+offset, back-trace and disassembly around PC for one thread.
    Most useful when the thread is stopped (crash / breakpoint / after xbox_execution stop)."""
    return analysis.thread_report(xbox, _addr(thread))


@mcp.tool(annotations=RO)
@friendly
def xbox_crash_report(thread: str | None = None) -> dict:
    """One-shot crash analysis: finds stopped threads (exception / breakpoint), dumps registers,
    symbolised PC/LR, back-trace, disassembly around PC, loaded modules and the last log lines."""
    rep = analysis.crash_report(xbox, _addr(thread) if thread else None)
    evs = notifier.query(limit=40, types=["debug", "exception", "stop", "execution"])
    rep["recent_log"] = [f"#{e.id} [{e.type}] {e.text}" for e in evs]
    return rep


@mcp.tool(annotations=DESTRUCTIVE)
@friendly
def xbox_execution(action: Literal["stop", "go", "continue"], thread: str | None = None, pass_exception: bool = False) -> str:
    """stop = break all threads, go = resume all, continue = resume one stopped thread
    (pass_exception=true hands the exception to the title's handler)."""
    _guard_write()
    if action == "stop":
        xbox.stop()
    elif action == "go":
        xbox.go()
    else:
        if not thread:
            raise ValueError("thread required for continue")
        xbox.continue_thread(_addr(thread), exception=pass_exception)
        xbox.go()
    return f"{action} ok"


@mcp.tool(annotations=DESTRUCTIVE)
@friendly
def xbox_breakpoint(action: Literal["set", "clear", "clear_all"], address: str | None = None) -> str:
    """Set/clear an execution breakpoint. When hit, the thread stops and a [stop] event appears
    in xbox_debug_log; inspect it with xbox_thread_state, resume with xbox_execution."""
    _guard_write()
    if action == "clear_all":
        xbox.clear_all_breakpoints()
        return "all breakpoints cleared"
    if not address:
        raise ValueError("address required")
    a = _addr(address)
    (xbox.set_breakpoint if action == "set" else xbox.clear_breakpoint)(a)
    return f"breakpoint {action} at 0x{a:08x}"


@mcp.tool(annotations=WRITE)
@friendly
def xbox_notify(message: str) -> str:
    """Show a notification toast on the TV (needs the JRPC2 plugin)."""
    _guard_write()
    ok = jrpc.xnotify(xbox, message)
    return "shown" if ok else "JRPC2 plugin not loaded: XNotify unavailable"


@mcp.tool(annotations=DESTRUCTIVE)
@friendly
def xbox_raw_command(command: str) -> dict:
    """Send any raw XBDM command line (e.g. 'systime', 'getconsoleid', 'xbeinfo running',
    'modsections name="default.xex"'). Returns status code, message and multiline body.
    In read-only mode only known read commands are accepted."""
    if cfg.read_only and not is_read_only_command(command):
        raise PermissionError(f"read-only mode: '{command.split(' ', 1)[0]}' is not a known read-only XBDM command")
    return xbox.raw(command)


def main() -> None:
    notifier.start()
    mcp.run()


if __name__ == "__main__":
    main()
