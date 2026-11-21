"""MCP server exposing an Xbox 360 RGH (xbdm) to Claude Code.

Run:  XBOX360_HOST=192.168.1.50 xbox360-mcp
"""

from __future__ import annotations

import atexit
import functools
import os
import re
import sys
import time
from pathlib import Path
from typing import Literal

# mcp >= 2 runs sync tools in a worker thread, so blocking tools (wait_for_log, reboot)
# don't freeze the server
from mcp.server.mcpserver import Image, MCPServer as _Server
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import analysis, capture as capture_mod, deploy as deploy_mod, jrpc, ppc
from .client import XboxConsole
from .config import Config
from .framebuffer import to_png
from .lock import ConsoleBusy, ConsoleLock, describe as describe_lock
from .notify import NotificationListener
from .protocol import XbdmConnectionError, XbdmError, is_read_only_command, normalize_path

INSTRUCTIONS = """\
Tools to drive an Xbox 360 RGH/JTAG running xbdm (DashLaunch plugin) over the LAN.

Typical homebrew / emulator loop:
1. Build on the PC, then `xbox_deploy` the build folder (incremental, only changed files are sent)
   and launch the .xex in the same call (launch=true, wait_for="<log line printed at init>").
2. Read what the title prints with OutputDebugString/DbgPrint via `xbox_debug_log` (use since_id
   from the previous call to only get new lines) or block on `xbox_wait_for_log`.
3. If the title crashes: `xbox_debugger(action="attach")`, reproduce the crash (the title now stops
   instead of going back to the dashboard), then `xbox_crash_report` (stopped threads, registers, PC
   symbolised as module+offset, back-trace, disassembly), then `xbox_debugger(action="detach")`.
   Keep the debugger detached otherwise: attached, titles relying on SEH (fastmem, JIT) hang.
4. `xbox_screenshot` to see the TV output (xbdm framebuffer).

If an HDMI capture card is plugged into the PC, the `xbox_capture_*` tools see exactly what the TV
shows: `xbox_capture_photo`, `xbox_capture_burst` (frames a few ms apart, for flicker or animations),
`xbox_capture_video` (a clip with sound) and `xbox_capture_record` (start before launching a title,
stop after, to review the whole run). They return contact sheets, not every frame; files are kept.

Console paths look like `Hdd:\\Apps\\MyGame\\default.xex` (forward slashes accepted).
Local paths are on the PC running this server; prefer absolute paths.
Memory addresses are hex strings (`0x82000000`) or ints. Titles usually load at 0x82000000.
`xbox_raw_command` sends any XBDM command when no dedicated tool exists.

The console may be shared with other Claude Code sessions. Tools that change it (deploy, launch,
reboot, upload, write memory, breakpoints...) reserve it automatically when it is free. Before a
multi-step test, call `xbox_lock(reason=...)` so nobody launches something else in the middle;
call `xbox_unlock` when you are done. If another session holds it you get a "Console reserved by"
error: either wait with `xbox_lock(wait_seconds=...)` or do other work and retry later.
Never work around a reservation (e.g. with xbox_raw_command).
"""

cfg = Config.from_env()
xbox = XboxConsole(cfg.host, cfg.port, cfg.timeout)
notifier = NotificationListener(cfg.host, cfg.port, attach_debugger=cfg.attach_debugger, log_file=cfg.log_file)
console_lock = ConsoleLock(cfg.host, lease_seconds=cfg.lock_lease)
atexit.register(lambda: console_lock.release())
card = capture_mod.CaptureCard.from_env()


def _stop_recording_at_exit() -> None:
    if card.recording and card.recording.running:
        card.stop_recording()  # finalise the MP4 instead of leaving ffmpeg running


atexit.register(_stop_recording_at_exit)

LOCK_HINT = (
    "Don't start anything on the console. Wait with xbox_lock(wait_seconds=100) (call it again to keep "
    "waiting) or do other work and retry later."
)
MAX_LOCK_WAIT = 110.0  # Claude Code moves a tool call to the background after ~120 s

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
    """Call at the start of every tool that changes the console: read-only check, then reserve
    the console for this session (fails if another session holds it)."""
    if cfg.read_only:
        raise PermissionError("Server is in read-only mode (XBOX360_READ_ONLY=1).")
    tool = sys._getframe(1).f_code.co_name
    try:
        console_lock.claim(f"auto: {tool}")
    except ConsoleBusy as e:
        raise ConsoleBusy(e.info, LOCK_HINT) from None


def _lock_info() -> dict | str:
    st = console_lock.status()
    return st if st else "free"


MAX_TEXT = 30_000  # characters; Claude Code rejects tool results much past ~80k


def _fit(text: str, limit: int = MAX_TEXT, *, keep_end: bool = False) -> str:
    """Cut text that would overflow Claude's tool-result limit, at a line boundary, saying so."""
    if len(text) <= limit:
        return text
    if keep_end:
        cut = text[-limit:]
        cut = cut[cut.find("\n") + 1 :] if "\n" in cut else cut
        return f"[... {len(text) - len(cut)} earlier characters cut]\n" + cut
    cut = text[:limit]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    return cut + f"\n[... {len(text) - len(cut)} more characters cut]"


def _fit_lines(lines: list[str], limit: int = MAX_TEXT) -> list[str]:
    """Keep the most recent lines that fit in `limit` characters."""
    out, used = [], 0
    for line in reversed(lines):
        used += len(line) + 4
        if used > limit:
            out.append(f"[... {len(lines) - len(out)} earlier lines cut: pass since_id / a filter]")
            break
        out.append(line)
    return out[::-1]


def _local(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser().resolve()


def _error_text(e: Exception) -> str:
    if isinstance(e, XbdmConnectionError):
        return (
            f"Console unreachable: {e}\nCheck: console powered on, same LAN, IP {cfg.host} correct, "
            "xbdm.xex loaded by DashLaunch. If it froze, the user may have to power-cycle it."
        )
    if isinstance(e, (ConsoleBusy, XbdmError, capture_mod.CaptureError)):
        return str(e)
    if isinstance(e, PermissionError):
        return f"Refused: {e}"
    if isinstance(e, FileNotFoundError):
        return f"Not found: {e}"
    return f"{type(e).__name__}: {e}"


def friendly(fn):
    """Renew our console reservation, and turn every failure into a ToolError so the agent reads
    the reason: mcp >= 2 replaces any other exception with a bare "Error executing tool X"."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            console_lock.renew()  # any call from the holder keeps its reservation alive
        except Exception:
            pass
        try:
            return fn(*a, **kw)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(_error_text(e)) from e

    return wrapper


def tool(annotations: ToolAnnotations):
    """Register an MCP tool. Always use this rather than @mcp.tool: it adds `friendly`."""

    def deco(fn):
        return mcp.tool(annotations=annotations)(friendly(fn))

    return deco


def _ensure_notifier() -> None:
    if not notifier.running:
        notifier.start()


# ---------------------------------------------------------------------------
# Console status
# ---------------------------------------------------------------------------
@tool(RO)
def xbox_status(include_temperatures: bool = True) -> dict:
    """Console name, type, kernel/system info, running title, who has the console reserved,
    debug-log channel state, and (if the JRPC2 plugin is loaded) temperatures, title ID and
    kernel version."""
    info = xbox.info()
    info["reservation"] = _lock_info()
    info["debug_channel"] = {
        "running": notifier.running,
        "connected": notifier.connected,
        "events_buffered_until_id": notifier.next_id - 1,
        "last_error": notifier.last_error,
        "log_file": cfg.log_file,
        "debugger_attached": notifier.attach_debugger,
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
@tool(RO)
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


@tool(RO)
def xbox_list_dir(path: str = "Hdd:\\", recursive: bool = False, max_entries: int = 200, contains: str | None = None) -> dict:
    """List a console directory, one compact line per entry: "<DIR>" or the size in bytes, the
    modification time, the name. recursive=true walks sub-folders and shows paths. contains filters
    names (case-insensitive). max_entries is capped at 1000: list a sub-folder or filter instead."""
    max_entries = max(1, min(max_entries, 1000))
    needle = contains.lower() if contains else None

    def line(name: str, e: dict) -> str:
        size = "<DIR>" if e["is_dir"] else str(e["size"])
        when = (e.get("modified") or "")[:16].replace("T", " ")
        return f"{size:>11}  {when:16}  {name}"

    if recursive:
        root = normalize_path(path)
        items = ((full[len(root):].lstrip("\\") or full, e) for full, e in xbox.walk(root))
    else:
        root = normalize_path(path, directory=True)
        items = ((str(e["name"]), e) for e in xbox.list_dir(path))
    out, total = [], 0
    for name, e in items:
        if needle and needle not in name.lower():
            continue
        total += 1
        if len(out) < max_entries:
            out.append(line(name, e))
        elif recursive:
            break  # don't walk a huge tree just to count it
    res: dict = {"path": root, "entries": out}
    if total > len(out):
        res["truncated"] = f"showing {len(out)} of {total if not recursive else 'more than ' + str(len(out))} entries"
    return res


@tool(RO)
def xbox_download(remote_path: str, local_path: str) -> dict:
    """Copy a file (or a whole folder) from the console to the PC. Files are streamed to disk, so
    size doesn't matter for memory, but xbdm runs at a few MB/s: a 1 GB file takes minutes. In a
    folder, a file that fails (e.g. held open by the running title) is reported and the others are
    still copied."""
    dst = _local(local_path)
    st = xbox.stat(remote_path)
    if st is None:
        raise FileNotFoundError(remote_path)
    files, failed, total = [], [], 0
    started = time.time()
    if st["is_dir"]:
        root = normalize_path(remote_path)
        for full, e in xbox.walk(root):
            rel = full[len(root) :].lstrip("\\").replace("\\", os.sep)
            target = dst / rel
            if e["is_dir"]:
                target.mkdir(parents=True, exist_ok=True)
                continue
            try:
                total += xbox.download_to(full, target)
                files.append(rel)
            except (XbdmError, OSError) as err:
                failed.append(f"{rel}: {_error_text(err)}")
    else:
        if dst.is_dir():
            dst = dst / normalize_path(remote_path).rpartition("\\")[2]
        total = xbox.download_to(remote_path, dst)
        files.append(str(dst))
    out: dict = {"downloaded": len(files), "bytes": total, "seconds": round(time.time() - started, 1), "to": str(dst)}
    out["files"] = files[:50] + ([f"... +{len(files) - 50} more"] if len(files) > 50 else [])
    if failed:
        out["failed"] = failed[:20] + ([f"... +{len(failed) - 20} more"] if len(failed) > 20 else [])
    return out


@tool(RO)
def xbox_read_text_file(remote_path: str, max_bytes: int = 16384, tail: bool = False, grep: str | None = None) -> str:
    """Read a text file on the console (e.g. a log your homebrew writes to Hdd:\\ or Usb0:\\).
    tail=true returns the last max_bytes. grep=<regex> returns only the matching lines (searching
    up to the last/first 4 MB). The answer is capped at ~30,000 characters: use grep or tail with a
    smaller max_bytes for big logs, or xbox_download to get the whole file.
    A log the running title holds open can't be read until the title exits."""
    st = xbox.stat(remote_path)
    if st is None:
        raise FileNotFoundError(remote_path)
    size = st["size"]
    want = min(max(1, max_bytes), 4 << 20 if grep else MAX_TEXT)
    if size <= want:
        data = xbox.download(remote_path)
    elif tail:
        data = xbox.download(remote_path, offset=size - want, size=want)
    else:
        data = xbox.download(remote_path, offset=0, size=want)
    text = data.decode("utf-8", errors="replace")
    head = f"[{size} bytes, read the {'last' if tail else 'first'} {len(data)}]\n" if size > len(data) else ""
    if grep:
        rx = re.compile(grep)
        lines = [l for l in text.splitlines() if rx.search(l)]
        head += f"[{len(lines)} lines match {grep!r}]\n"
        text = "\n".join(lines)
    return head + _fit(text, MAX_TEXT, keep_end=tail)


@tool(WRITE)
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


@tool(WRITE)
def xbox_mkdir(path: str) -> str:
    """Create a console directory (parents included)."""
    _guard_write()
    xbox.makedirs(path)
    return f"created {normalize_path(path)}"


@tool(DESTRUCTIVE)
def xbox_rename(src: str, dst: str, overwrite: bool = False) -> str:
    """Rename / move a file or folder on the same console drive (system locations refused).
    An existing destination makes it fail ("file already exists") unless overwrite=true, which
    deletes an existing destination *file* first (e.g. default.xex -> default.xex.bak)."""
    _guard_write()
    replaced = xbox.rename(src, dst, overwrite=overwrite)
    return f"{normalize_path(src)} -> {normalize_path(dst)}" + (" (replaced the old file)" if replaced else "")


@tool(DESTRUCTIVE)
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
def _launch_and_watch(xex: str, wait_for: str | None, timeout: float, capture_seconds: float, wait_exit: float = 0) -> dict:
    _ensure_notifier()
    deadline = time.time() + 3  # don't miss the first lines if the channel was just opened
    while not notifier.connected and time.time() < deadline:
        time.sleep(0.05)
    start_id = notifier.next_id - 1
    started = time.time()
    xbox.launch(xex)  # raises if the console can't be reached
    out: dict = {"launched": normalize_path(xex), "log_since_id": start_id}
    if not notifier.connected:
        out["warning"] = f"debug-log channel not connected ({notifier.last_error}): the log below may be empty"
    if not (wait_for or capture_seconds > 0 or wait_exit > 0):
        return out
    # The previous title's teardown (dozens of "terminate thread" lines, Aurora's modunloads) comes
    # first: start everything at the new title's own modload so it neither fills the log nor
    # matches wait_for.
    name = re.escape(normalize_path(xex).rpartition("\\")[2])
    loaded = notifier.wait_for(rf'^modload name="{name}"', min(timeout, 30), since_id=start_id, types=["modload"])
    from_id = loaded.id - 1 if loaded else start_id
    if loaded:
        skipped = len(notifier.query(since_id=start_id, limit=100000, tail=False)) - len(notifier.query(since_id=from_id, limit=100000, tail=False))
        out["title_loaded_after_s"] = round(loaded.time - started, 1)
        if skipped:
            out["skipped_teardown_lines"] = skipped
    else:
        out["note"] = "the title's modload wasn't seen: the log starts at the launch"
    if wait_for:
        ev = notifier.wait_for(wait_for, max(0.1, timeout - (time.time() - started)), since_id=from_id)
        out["wait_for"] = {"pattern": wait_for, "matched": ev.to_dict() if ev else None, "timed_out": ev is None}
    elif capture_seconds > 0:
        time.sleep(max(0.0, capture_seconds - (time.time() - started)))
    if wait_exit > 0:
        out["exit"] = xbox.wait_title_exit(xex, wait_exit)
    crash = notifier.query(since_id=from_id, types=["exception"], limit=5)
    if crash:
        out["exceptions"] = [e.to_dict() for e in crash]
    evs = notifier.query(since_id=from_id, limit=400)
    out["log"] = _fit_lines([f"#{e.id} [{e.type}] {e.text}" for e in evs], MAX_TEXT // 2)
    out["next_since_id"] = notifier.next_id - 1
    return out


@tool(WRITE)
def xbox_launch(
    xex_path: str,
    wait_for: str | None = None,
    timeout_seconds: float = 30,
    capture_seconds: float = 0,
    wait_exit_seconds: float = 0,
) -> dict:
    """Launch a .xex on the console (magicboot). Optionally block until a debug-log line matches
    the regex `wait_for`, or just capture logs for N seconds. The returned log starts at the new
    title's own modload (the previous title's teardown is skipped), and wait_for only matches from
    there. wait_exit_seconds=N also waits until the title has exited (back to the dashboard), up to
    N seconds: then its log file can be read."""
    _guard_write()
    return _launch_and_watch(xex_path, wait_for, timeout_seconds, capture_seconds, wait_exit_seconds)


@tool(WRITE)
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
    wait_exit_seconds: float = 0,
) -> dict:
    """Incrementally sync a PC build folder to the console (only changed files are sent), then
    optionally launch `xex_name` from it and return the first log lines (see xbox_launch for
    wait_for / capture_seconds / wait_exit_seconds).
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
        result["run"] = _launch_and_watch(xex, wait_for, timeout_seconds, capture_seconds, wait_exit_seconds)
    return result


@tool(DESTRUCTIVE)
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
@tool(RO)
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
    out = {
        "channel_connected": notifier.connected,
        "lines": _fit_lines([f"#{e.id} {e.to_dict()['time']} [{e.type}] {e.text}" for e in evs]),
        "next_since_id": notifier.next_id - 1,
    }
    st = console_lock.status()
    if st and not st["held_by_me"]:  # these lines may come from someone else's test
        out["note"] = describe_lock(st)
    return out


@tool(RO)
def xbox_wait_for_log(pattern: str, timeout_seconds: float = 30, since_id: int | None = None) -> dict:
    """Block until a debug line matching the regex appears (or timeout). since_id=None waits for
    new lines only. To know when a title has exited, use xbox_launch(wait_exit_seconds=...) rather
    than a log pattern: right after a launch, Aurora's own modunload lines come first and match
    patterns like "modunload" or "Aurora"."""
    _ensure_notifier()
    ev = notifier.wait_for(pattern, timeout_seconds, since_id=since_id)
    return {"matched": ev.to_dict() if ev else None, "timed_out": ev is None, "next_since_id": notifier.next_id - 1}


@tool(WRITE)
def xbox_clear_log() -> str:
    """Clear the captured debug-log buffer (ids keep increasing)."""
    notifier.clear()
    return "cleared"


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------
@tool(RO)
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
    # Right after a launch or an exit the framebuffer can still hold the previous title's frame or
    # garbage: say which title is running so a stale picture can be recognised.
    try:
        meta["running_title"] = xbox.running_title().get("name")
    except (XbdmError, XbdmConnectionError):
        pass
    return [Image(data=png, format="png"), str(meta)]


# ---------------------------------------------------------------------------
# HDMI capture card (what the TV really shows; doesn't touch the console)
# ---------------------------------------------------------------------------
def _capture_meta(meta: dict) -> str:
    st = console_lock.status()
    if st and not st["held_by_me"]:  # the picture may be someone else's test
        meta["note"] = describe_lock(st)
    return str(meta)


def _sheet(frames: list, labels: list[str], thumb_width: int) -> Image:
    return Image(data=capture_mod.contact_sheet(frames, labels, thumb_width=thumb_width), format="jpeg")


@tool(RO)
def xbox_capture_photo(save_path: str | None = None, max_width: int = 1280):
    """Photo of the console's HDMI output through the USB capture card: exactly what the TV shows
    (any title, dashboard, scaler output), unlike xbox_screenshot which reads xbdm's framebuffer.
    Saves a full-size PNG (save_path, or XBOX360_CAPTURE_DIR) and returns it scaled to max_width.
    During an xbox_capture_record, returns the recording's latest frame."""
    shot = card.photo(_local(save_path) if save_path else None)
    jpg, size = capture_mod.preview_jpeg(shot["path"], max_width)
    return [Image(data=jpg, format="jpeg"), _capture_meta({**shot, "size": f"{size[0]}x{size[1]}"})]


@tool(RO)
def xbox_capture_burst(count: int = 8, interval_ms: int = 250, save_dir: str | None = None, thumb_width: int = 480):
    """Burst of `count` HDMI photos (max 120) taken `interval_ms` apart (0 = every frame the card
    delivers, ~30/s), e.g. to catch flicker, an animation or a short glitch. Full-size JPEGs are saved
    in save_dir; returns one contact sheet with each frame's index and time offset."""
    b = card.burst(count, max(0, interval_ms) / 1000, _local(save_dir) if save_dir else None)
    labels = [f"#{i} +{t:.2f}s" for i, t in enumerate(b["times"])]
    frames = b["frames"]
    meta = {"dir": b["dir"], "count": len(frames), "files": [Path(f).name for f in frames[:3]] + (["..."] if len(frames) > 3 else [])}
    return [_sheet(frames[:24], labels, thumb_width), _capture_meta(meta)]


@tool(RO)
def xbox_capture_video(seconds: float = 10, save_path: str | None = None, audio: bool = True, preview_frames: int = 6):
    """Record an HDMI clip (MP4, H.264 + sound from the capture card) and wait for it, max 300 s.
    Returns a contact sheet of `preview_frames` frames spread over the clip plus the file path.
    For a recording that spans other actions (launch, logs...), use xbox_capture_record instead."""
    r = card.record(max(1.0, min(seconds, 300.0)), _local(save_path) if save_path else None, audio=audio)
    return _video_result(r, preview_frames)


def _video_result(r: dict, preview_frames: int) -> list:
    out: list = []
    if preview_frames > 0:
        frames, times = capture_mod.sample_video(r["path"], max(1, min(preview_frames, 24)))
        out.append(_sheet(frames, [f"{t:.1f}s" for t in times], 320))
    out.append(_capture_meta(r))
    return out


@tool(RO)
def xbox_capture_record(
    action: Literal["start", "stop", "status"],
    save_path: str | None = None,
    audio: bool = True,
    max_seconds: float = 600,
    preview_frames: int = 6,
):
    """Background HDMI recording: start, do anything (deploy, launch, wait for logs, play...), then
    stop, which returns the MP4 path and a contact sheet of `preview_frames` frames. The recording
    ends by itself after max_seconds (max 3600). While it runs, xbox_capture_photo still works
    (latest frame) but bursts and clips can't open the card. status also shows the capture devices."""
    if action == "start":
        return card.start_recording(_local(save_path) if save_path else None, audio=audio, max_seconds=max(5.0, min(max_seconds, 3600.0)))
    if action == "stop":
        return _video_result(card.stop_recording(), preview_frames)
    return {**card.status(), "devices": card.devices(), "output_dir": str(card.out_dir)}


# ---------------------------------------------------------------------------
# Memory / debugging
# ---------------------------------------------------------------------------
@tool(RO)
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


@tool(DESTRUCTIVE)
def xbox_write_memory(address: str, hex_data: str) -> str:
    """Write bytes into console memory, e.g. hex_data='60000000' (a PPC nop)."""
    _guard_write()
    data = bytes.fromhex(hex_data.replace(" ", "").replace("0x", ""))
    a = _addr(address)
    xbox.write_memory(a, data)
    return f"wrote {len(data)} bytes at 0x{a:08x}"


@tool(RO)
def xbox_find_bytes(start: str, length: int, pattern: str, max_hits: int = 32) -> list[str]:
    """Search a memory range (max 64 MiB) for a hex pattern with ?? wildcards,
    e.g. pattern='7C 08 02 A6 ?? ?? ?? ??'."""
    length = min(length, 64 << 20)
    hits = analysis.find_bytes(xbox, _addr(start), length, pattern, max_hits)
    mods = xbox.modules()
    return [f"0x{h:08x}  {xbox.resolve_address(h, mods)}" for h in hits]


@tool(RO)
def xbox_disassemble(address: str, count: int = 32, context_before: int = 0) -> str:
    """Disassemble PowerPC code from console memory (capstone; VMX128 shows as .long).
    context_before = number of instructions to show before `address` (marked =>)."""
    if not ppc.has_disassembler():
        return "capstone not installed: pip install capstone"
    return "\n".join(analysis.disassemble_at(xbox, _addr(address), min(count, 512), context_before))


@tool(RO)
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


@tool(RO)
def xbox_memory_map() -> list[str]:
    """Committed virtual memory regions (walkmem): base, size, protection."""
    out = []
    for r in xbox.memory_map():
        base, size, prot = r.get("base"), r.get("size"), r.get("protect")
        out.append(
            f"0x{base:08x}-0x{base + size:08x} size=0x{size:x} protect=0x{prot:x}" if isinstance(base, int) and isinstance(size, int) and isinstance(prot, int) else str(r)
        )
    return out


@tool(RO)
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


@tool(RO)
def xbox_thread_state(thread: str) -> dict:
    """Registers, PC/LR as module+offset, back-trace and disassembly around PC for one thread.
    Most useful when the thread is stopped (crash / breakpoint / after xbox_execution stop)."""
    return analysis.thread_report(xbox, _addr(thread))


@tool(RO)
def xbox_crash_report(thread: str | None = None) -> dict:
    """One-shot crash analysis: finds stopped threads (exception / breakpoint), dumps registers,
    symbolised PC/LR, back-trace, disassembly around PC, loaded modules and the last log lines.
    A crashing title only stays stopped while the debugger is attached (xbox_debugger attach,
    then reproduce the crash); detached, it returns to the dashboard."""
    rep = analysis.crash_report(xbox, _addr(thread) if thread else None)
    evs = notifier.query(limit=40, types=["debug", "exception", "stop", "execution"])
    rep["recent_log"] = _fit_lines([f"#{e.id} [{e.type}] {e.text}" for e in evs], MAX_TEXT // 3)
    return rep


@tool(DESTRUCTIVE)
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


@tool(DESTRUCTIVE)
def xbox_breakpoint(action: Literal["set", "clear", "clear_all"], address: str | None = None) -> str:
    """Set/clear an execution breakpoint (needs xbox_debugger attach first). When hit, the thread
    stops and a [stop] event appears in xbox_debug_log; inspect it with xbox_thread_state, resume
    with xbox_execution."""
    _guard_write()
    if action == "clear_all":
        xbox.clear_all_breakpoints()
        return "all breakpoints cleared"
    if not address:
        raise ValueError("address required")
    a = _addr(address)
    (xbox.set_breakpoint if action == "set" else xbox.clear_breakpoint)(a)
    return f"breakpoint {action} at 0x{a:08x}"


@tool(DESTRUCTIVE)
def xbox_debugger(action: Literal["attach", "detach", "status"]) -> dict:
    """Attach or detach this server as xbdm's debugger. Detached is the default and the safe state.
    attach: a crashing title then STOPS instead of returning to the dashboard, so xbox_crash_report /
    xbox_thread_state can inspect it, and breakpoints work. But every first-chance fault is replayed
    forever: titles relying on SEH (fastmem, JIT) hang, and Aurora's Nova plugin floods xbdm. Attach
    only to chase a crash, then detach (xbox_unlock also detaches). The state is global to the
    console: detach also clears a debugger attached by another program."""
    if action == "status":
        return {"attached": notifier.attach_debugger, "note": "as set by this server; xbdm doesn't report it"}
    _guard_write()
    if action == "attach":
        xbox.debugger_attach()
        notifier.attach_debugger = True  # re-attached by the log channel after each launch
    else:
        notifier.attach_debugger = False
        xbox.debugger_detach()
    _ensure_notifier()
    return {"attached": notifier.attach_debugger}


@tool(WRITE)
def xbox_notify(message: str) -> str:
    """Show a notification toast on the TV (needs the JRPC2 plugin)."""
    _guard_write()
    ok = jrpc.xnotify(xbox, message)
    return "shown" if ok else "JRPC2 plugin not loaded: XNotify unavailable"


@tool(DESTRUCTIVE)
def xbox_raw_command(command: str) -> dict:
    """Send any raw XBDM command line (e.g. 'systime', 'getconsoleid', 'xbeinfo running',
    'modsections name="default.xex"'). Returns status code, message and multiline body.
    In read-only mode only known read commands are accepted; any other command reserves the console."""
    if not is_read_only_command(command):
        if cfg.read_only:
            raise PermissionError(f"read-only mode: '{command.split(' ', 1)[0]}' is not a known read-only XBDM command")
        _guard_write()
    return xbox.raw(command)


# ---------------------------------------------------------------------------
# Sharing the console between sessions
# ---------------------------------------------------------------------------
@tool(WRITE)
def xbox_lock(reason: str, wait_seconds: float = 0, owner: str | None = None, lease_seconds: float | None = None) -> dict:
    """Reserve the console for a test session so other Claude Code sessions (and build scripts
    using the CLI) can't deploy / launch / reboot in the middle of it.
    - Free (or already yours): returns at once with held_by_me: true. Only then start uploads,
      scripts, FTP... never in the same batch as this call: parallel calls run even if it fails.
    - Held by another session: waits up to wait_seconds (max 110, so the call is never moved to the
      background), then fails with who holds it, why, and when their lease ends. Call again to keep
      waiting.
    - The reservation lasts lease_seconds (default XBOX360_LOCK_LEASE, 600; max 3600) after your
      last xbox_* call of any kind (xbox_lock_status included) and is renewed by each. Out-of-band
      tools (FTP, xbreboot, scripts) don't renew it: pass a longer lease_seconds for long scripted
      runs, or call xbox_lock_status every few minutes. Calling xbox_lock again also renews it.
    - It ends when this session's MCP server stops, and it is shared by this session and its
      subagents (same server): they can't use it to coordinate with each other.
    owner = name shown to the others (e.g. your project name). Also opens the debug-log channel
    (without attaching the debugger, and detaching one left by another program: see xbox_debugger).
    Release it with xbox_unlock right after the test."""
    if owner:
        console_lock.owner = owner
    if lease_seconds:
        console_lock.lease_seconds = max(60.0, min(float(lease_seconds), 3600.0))
    was_mine = console_lock.held_by_me()
    try:
        info = console_lock.acquire(reason, wait_seconds=max(0.0, min(wait_seconds, MAX_LOCK_WAIT)))
    except ConsoleBusy as e:
        hint = f"Still busy after waiting {min(wait_seconds, MAX_LOCK_WAIT):.0f} s. " if wait_seconds > 0 else ""
        raise ConsoleBusy(e.info, hint + LOCK_HINT) from None
    if not was_mine and not notifier.attach_debugger:
        info["debugger"] = _detach_quietly()
    _ensure_notifier()
    return info


def _detach_quietly() -> str:
    """A debugger left attached (older server version, XDK tool, earlier session) makes titles that
    rely on SEH hang: clear it when we take the console. Never fails the calling tool."""
    try:
        xbox.debugger_detach()
        return "detached"
    except Exception as e:
        return f"could not detach: {_error_text(e)}"


@tool(WRITE)
def xbox_unlock() -> str:
    """Release your reservation of the console so other sessions can use it. Also detaches the
    debugger if you attached it with xbox_debugger."""
    if not console_lock.release():
        st = console_lock.status()
        return "you did not hold the console (lease expired or already released)" + (f"; {describe_lock(st)}" if st else "; it is free")
    console_lock.lease_seconds = cfg.lock_lease  # a longer lease was for that reservation only
    out = "console released"
    if notifier.attach_debugger:
        notifier.attach_debugger = False
        out += f", debugger {_detach_quietly()}"
    notifier.stop()  # frees an xbdm connection (xbdm accepts only ~4)
    return out


@tool(RO)
def xbox_lock_status() -> dict | str:
    """Who has the console reserved (owner, reason, for how long, lease left), or "free".
    If you hold it, this call renews your lease (like every xbox_* call)."""
    return _lock_info()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
