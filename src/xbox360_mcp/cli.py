"""`xbox360` command line: the same features as the MCP server, for scripts and humans.

    xbox360 --host 192.168.1.50 info
    xbox360 ls Hdd:\\Apps
    xbox360 deploy .\\build\\Release Hdd:\\Devkit\\Harissa64 --run --wait "init ok"
    xbox360 log -f
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import analysis, deploy as deploy_mod, jrpc
from .client import XboxConsole
from .framebuffer import to_png
from .lock import ConsoleBusy, ConsoleLock, describe as describe_lock
from .notify import NotificationListener
from .protocol import XbdmConnectionError, XbdmError, is_read_only_command, normalize_path

WRITE_COMMANDS = {"put", "rm", "mkdir", "deploy", "run", "reboot", "notify"}


def _hex(s: str) -> int:
    s = s.lower()
    return int(s[2:] if s.startswith("0x") else s, 16)


def _print(obj) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    else:
        print(obj)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xbox360", description="Xbox 360 RGH (xbdm) toolbox")
    p.add_argument("--host", default=os.environ.get("XBOX360_HOST") or os.environ.get("XBOX_IP"))
    p.add_argument("--port", type=int, default=int(os.environ.get("XBOX360_PORT", "730")))
    p.add_argument(
        "--wait-lock", type=float, default=0, metavar="SECONDS",
        help="if another session has the console reserved, wait up to SECONDS for it (default: fail at once)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lock-status", help="who has the console reserved")
    s = sub.add_parser("unlock", help="clear a stuck reservation held by another session")
    s.add_argument("--force", action="store_true", required=True)

    sub.add_parser("info", help="console info")
    sub.add_parser("drives", help="drives and free space")
    s = sub.add_parser("ls", help="list directory")
    s.add_argument("path", nargs="?", default="Hdd:\\")
    s.add_argument("-r", "--recursive", action="store_true")
    s = sub.add_parser("get", help="download file")
    s.add_argument("remote")
    s.add_argument("local")
    s = sub.add_parser("put", help="upload file")
    s.add_argument("local")
    s.add_argument("remote")
    s = sub.add_parser("rm", help="delete")
    s.add_argument("path")
    s.add_argument("-r", "--recursive", action="store_true")
    s.add_argument("--force", action="store_true", help="allow protected paths (Flash:, Hdd:\\Content, Hdd:\\Cache, drive roots)")
    s = sub.add_parser("mkdir")
    s.add_argument("path")
    s = sub.add_parser("deploy", help="incremental sync of a build folder (+ optional launch)")
    s.add_argument("local")
    s.add_argument("remote")
    s.add_argument("--run", action="store_true", help="launch <remote>\\default.xex afterwards")
    s.add_argument("--xex", default="default.xex")
    s.add_argument("--wait", help="regex to wait for in the debug log after launch")
    s.add_argument("--timeout", type=float, default=30)
    s.add_argument("--force", action="store_true", help="upload every file even if unchanged")
    s.add_argument("--prune", action="store_true", help="delete console files missing locally (also saves written by the title!)")
    s = sub.add_parser("run", help="launch a xex")
    s.add_argument("xex")
    s = sub.add_parser("reboot")
    s.add_argument("--cold", action="store_true")
    s = sub.add_parser("log", help="print debug output")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-t", "--seconds", type=float, default=5)
    s = sub.add_parser("screenshot")
    s.add_argument("out", nargs="?", default="screenshot.png")
    s.add_argument("--order", default="BGRA", help="byte order of a pixel (BGRA on a real console)")
    s.add_argument("--untiled", action="store_true")
    s.add_argument("--raw", help="also dump the raw framebuffer + header here")
    s = sub.add_parser("mem", help="read memory (address and length are hex)")
    s.add_argument("address", help="hex, e.g. 82000000")
    s.add_argument("length", nargs="?", default="100", help="hex byte count (default 100 = 256 bytes)")
    s.add_argument("--format", default="hexdump")
    s = sub.add_parser("dis", help="disassemble")
    s.add_argument("address")
    s.add_argument("count", nargs="?", type=int, default=32)
    sub.add_parser("modules")
    sub.add_parser("threads")
    sub.add_parser("crash", help="crash report of stopped threads")
    s = sub.add_parser("notify", help="XNotify toast (JRPC2)")
    s.add_argument("message")
    s = sub.add_parser("raw", help="raw XBDM command")
    s.add_argument("command", nargs=argparse.REMAINDER)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.host:
        print("Set --host or XBOX360_HOST", file=sys.stderr)
        return 2
    lock = ConsoleLock(args.host, owner=os.environ.get("XBOX360_OWNER") or f"xbox360 CLI in {Path.cwd().name}")
    if args.cmd == "lock-status":
        st = lock.status()
        print(describe_lock(st) if st else "free")
        return 0
    if args.cmd == "unlock":
        print("reservation cleared" if lock.release(force=True) else "nothing to clear")
        return 0
    # commands that change the console reserve it for their duration, like the MCP tools do
    needs_lock = args.cmd in WRITE_COMMANDS or (args.cmd == "raw" and not is_read_only_command(" ".join(args.command)))
    x = XboxConsole(args.host, args.port)
    try:
        if needs_lock:
            lock.acquire(f"xbox360 {args.cmd}", wait_seconds=args.wait_lock)
        return _dispatch(x, args)
    except ConsoleBusy as e:
        print(f"error: {e} Retry later or pass --wait-lock SECONDS.", file=sys.stderr)
        return 4
    except (XbdmError, XbdmConnectionError, FileNotFoundError, PermissionError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if needs_lock:
            lock.release()
        x.close()


def _follow(listener: NotificationListener, seconds: float | None, start_id: int = 0) -> None:
    last = start_id
    deadline = None if seconds is None else time.time() + seconds
    while deadline is None or time.time() < deadline:
        for e in listener.query(since_id=last, limit=10000, tail=False):
            print(f"{e.to_dict()['time']} [{e.type}] {e.text}", flush=True)
            last = e.id
        time.sleep(0.1)


def _dispatch(x: XboxConsole, a) -> int:
    c = a.cmd
    if c == "info":
        info = x.info()
        kv = jrpc.kernel_version(x)
        if kv:
            info["jrpc2"] = {"kernel": kv, "title_id": jrpc.title_id(x), "temps": jrpc.temperatures(x)}
        _print(info)
    elif c == "drives":
        _print([{"drive": d, **x.free_space(d + ":\\")} for d in x.drives()])
    elif c == "ls":
        if a.recursive:
            for full, e in x.walk(a.path):
                print(f"{'<DIR>' if e['is_dir'] else e['size']:>12}  {full}")
        else:
            for e in x.list_dir(a.path):
                print(f"{'<DIR>' if e['is_dir'] else e['size']:>12}  {e['modified'] or '':<32} {e['name']}")
    elif c == "get":
        data = x.download(a.remote)
        Path(a.local).write_bytes(data)
        print(f"{len(data)} bytes -> {a.local}")
    elif c == "put":
        size = x.upload_file(a.remote, a.local)
        print(f"{size} bytes -> {normalize_path(a.remote)}")
    elif c == "rm":
        print(f"deleted {x.delete(a.path, recursive=a.recursive, force=a.force)}")
    elif c == "mkdir":
        x.makedirs(a.path)
    elif c == "deploy":
        res = deploy_mod.deploy(
            x, a.local, a.remote, force=a.force, prune=a.prune, progress=lambda r, s: print(f"  -> {r} ({s} B)")
        )
        for d in res.get("deleted", []):
            print(f"  x  {d}")
        print(f"{res['uploaded_count']} sent, {res['skipped_unchanged']} unchanged, {len(res.get('deleted', []))} deleted, {res['seconds']} s")
        if a.run:
            listener = NotificationListener(x.host, x.port)
            listener.start()
            time.sleep(0.5)
            start = listener.next_id - 1
            x.launch(normalize_path(res["remote_dir"]).rstrip("\\") + "\\" + a.xex)
            if a.wait:
                ev = listener.wait_for(a.wait, a.timeout, since_id=start)
                for e in listener.query(since_id=start, limit=10000, tail=False):
                    print(f"{e.to_dict()['time']} [{e.type}] {e.text}")
                print("MATCHED" if ev else "TIMEOUT")
                return 0 if ev else 3
            _follow(listener, 5, start)
    elif c == "run":
        x.launch(a.xex)
    elif c == "reboot":
        x.reboot(cold=a.cold)
    elif c == "log":
        listener = NotificationListener(x.host, x.port)
        listener.start()
        try:
            _follow(listener, None if a.follow else a.seconds)
        except KeyboardInterrupt:
            pass
    elif c == "screenshot":
        header, raw = x.screenshot_raw()
        if a.raw:
            Path(a.raw).write_bytes(raw)
            Path(a.raw + ".json").write_text(json.dumps(header, default=str))
        png, meta = to_png(header, raw, channel_order=a.order, tiled=not a.untiled, max_width=None)
        Path(a.out).write_bytes(png)
        print(f"{a.out} {meta}")
    elif c == "mem":
        addr = _hex(a.address)
        data, bad = x.read_memory(addr, _hex(a.length))
        print(analysis.format_memory(data, addr, a.format, bad))
    elif c == "dis":
        print("\n".join(analysis.disassemble_at(x, _hex(a.address), a.count)))
    elif c == "modules":
        for m in x.modules():
            print(f"0x{m.get('base', 0):08x} 0x{m.get('size', 0):08x}  {m.get('name')}")
    elif c == "threads":
        for t in x.threads():
            stopped, why = x.is_stopped(t)
            print(f"0x{t:08x}  {'STOPPED ' + why if stopped else 'running'}")
    elif c == "crash":
        _print(analysis.crash_report(x))
    elif c == "notify":
        print("ok" if jrpc.xnotify(x, a.message) else "JRPC2 not available")
    elif c == "raw":
        _print(x.raw(" ".join(a.command)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
