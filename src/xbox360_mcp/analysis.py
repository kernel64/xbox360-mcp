"""Higher level debugging helpers shared by the CLI and the MCP server."""

from __future__ import annotations

import struct

from . import ppc
from .client import XboxConsole
from .protocol import XbdmError


def hexdump(data: bytes, address: int, bad: list[tuple[int, int]] | None = None, width: int = 16) -> str:
    bad = bad or []

    def unreadable(a: int) -> bool:
        return any(s <= a < e for s, e in bad)

    out = []
    for i in range(0, len(data), width):
        row = data[i : i + width]
        hexes = []
        for j, b in enumerate(row):
            hexes.append("??" if unreadable(address + i + j) else f"{b:02x}")
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append(f"{address + i:08x}  {' '.join(hexes):<{width * 3}} {asc}")
    return "\n".join(out)


def format_memory(data: bytes, address: int, fmt: str, bad=None) -> str:
    if fmt == "hexdump":
        return hexdump(data, address, bad)
    if fmt in ("u32", "f32"):
        n = len(data) // 4
        code = ">" + ("I" if fmt == "u32" else "f") * n
        vals = struct.unpack(code, data[: n * 4])
        rows = []
        for i in range(0, n, 4):
            chunk = vals[i : i + 4]
            cells = [f"0x{v:08x}" if fmt == "u32" else f"{v:.6g}" for v in chunk]
            rows.append(f"{address + i * 4:08x}  " + "  ".join(cells))
        return "\n".join(rows)
    if fmt == "u16":
        n = len(data) // 2
        vals = struct.unpack(">" + "H" * n, data[: n * 2])
        return "\n".join(
            f"{address + i * 2:08x}  " + " ".join(f"{v:04x}" for v in vals[i : i + 8]) for i in range(0, n, 8)
        )
    if fmt == "string":
        return data.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    if fmt == "utf16":
        return data.decode("utf-16-be", errors="replace").split("\0", 1)[0]
    if fmt == "hex":
        return data.hex()
    raise ValueError("format must be one of hexdump, hex, u16, u32, f32, string, utf16")


def disassemble_at(xbox: XboxConsole, address: int, count: int = 32, before: int = 0) -> list[str]:
    start = (address - before * 4) & ~3
    data, bad = xbox.read_memory(start, (count + before) * 4)
    lines = ppc.disassemble(data, start, mark=address if before else None)
    if bad:
        lines.append(f"(unreadable ranges: {[(hex(s), hex(e)) for s, e in bad]})")
    return lines


def _reg(ctx: dict, *names: str) -> int | None:
    lower = {k.lower(): v for k, v in ctx.items()}
    for n in names:
        v = lower.get(n.lower())
        if isinstance(v, int):
            return v
    return None


def thread_report(xbox: XboxConsole, thread: int, modules: list[dict] | None = None, disasm: int = 12) -> dict:
    modules = modules if modules is not None else xbox.modules()
    rep: dict = {"thread": f"0x{thread:08x}"}
    try:
        rep["info"] = xbox.thread_info(thread)
    except XbdmError as e:
        rep["info_error"] = str(e)
    stopped, reason = xbox.is_stopped(thread)
    rep["stopped"] = stopped
    rep["stop_reason"] = reason
    try:
        ctx = xbox.thread_context(thread)
    except XbdmError as e:
        rep["context_error"] = str(e)
        return rep
    iar = _reg(ctx, "iar", "pc")
    lr = _reg(ctx, "lr")
    sp = _reg(ctx, "gpr1", "r1")
    gprs = {f"r{i}": f"0x{v & 0xFFFFFFFFFFFFFFFF:x}" for i in range(32) if (v := _reg(ctx, f"gpr{i}", f"r{i}")) is not None}
    rep["registers"] = {
        k: (f"0x{v:x}" if isinstance(v, int) else v) for k, v in ctx.items() if not k.lower().startswith(("gpr", "fpr"))
    }
    rep["gpr"] = gprs
    if iar is not None:
        rep["pc"] = xbox.resolve_address(iar, modules)
        try:
            rep["disassembly"] = disassemble_at(xbox, iar, count=disasm, before=disasm // 2)
        except XbdmError as e:
            rep["disassembly_error"] = str(e)
    if lr is not None:
        rep["lr"] = xbox.resolve_address(lr & 0xFFFFFFFF, modules)
    if sp:
        frames = ppc.backtrace(lambda a: xbox.read_u32(a), sp & 0xFFFFFFFF, lr & 0xFFFFFFFF if lr else None)
        rep["backtrace"] = [xbox.resolve_address(f, modules) for f in frames]
    return rep


def crash_report(xbox: XboxConsole, thread: int | None = None, max_threads: int = 4) -> dict:
    modules = xbox.modules()
    threads = [thread] if thread is not None else xbox.threads()
    reports = []
    stopped_any = False
    for t in threads:
        stopped, reason = xbox.is_stopped(t)
        if thread is None and not stopped:
            continue
        stopped_any = stopped_any or stopped
        reports.append(thread_report(xbox, t, modules))
        if len(reports) >= max_threads:
            break
    return {
        "running_title": xbox.running_title().get("name"),
        "stopped_threads_found": stopped_any,
        "threads": reports,
        "modules": [
            f"{m.get('name')} base=0x{m.get('base', 0):08x} size=0x{m.get('size', 0):x}" for m in modules
        ],
    }


def find_bytes(xbox: XboxConsole, start: int, length: int, pattern: str, max_hits: int = 64) -> list[int]:
    """Search for a hex pattern with ?? wildcards, e.g. '80 00 ?? 37'.
    Unmapped memory is skipped (a match may not overlap an unreadable byte)."""
    toks = pattern.replace(",", " ").split()
    if len(toks) == 1 and len(toks[0]) > 2:
        t = toks[0]
        toks = [t[i : i + 2] for i in range(0, len(t), 2)]
    pat = [None if t in ("??", "?") else int(t, 16) for t in toks]
    if not pat:
        raise ValueError("empty pattern")
    import re

    # lookahead so overlapping matches are found too (e.g. "00 00" in a run of zeros)
    body = b"".join(b"." if p is None else re.escape(bytes([p])) for p in pat)
    rx = re.compile(b"(?=" + body + b")", re.DOTALL)
    hits: list[int] = []
    chunk = 0x10000
    overlap = len(pat) - 1
    pos = start
    end = start + length
    while pos < end and len(hits) < max_hits:
        n = min(chunk + overlap, end - pos)
        data, bad = xbox.read_memory(pos, n)
        for m in rx.finditer(data):
            a = pos + m.start()
            # unreadable bytes come back as 0: never report a match that touches them
            if any(s < a + len(pat) and a < e for s, e in bad):
                continue
            if a not in hits:
                hits.append(a)
                if len(hits) >= max_hits:
                    break
        pos += chunk
    return hits
