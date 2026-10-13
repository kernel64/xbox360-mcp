"""PowerPC (Xenon) helpers: disassembly via capstone (optional) and stack walking."""

from __future__ import annotations

import struct

try:  # optional dependency
    import capstone  # type: ignore

    _CS = capstone.Cs(capstone.CS_ARCH_PPC, capstone.CS_MODE_64 | capstone.CS_MODE_BIG_ENDIAN)
except Exception:  # pragma: no cover - capstone missing
    _CS = None


def has_disassembler() -> bool:
    return _CS is not None


def disassemble(data: bytes, address: int, mark: int | None = None) -> list[str]:
    """Disassemble big-endian PPC code. VMX128 opcodes capstone doesn't know show as `.long`."""
    lines = []
    for i in range(0, len(data) - 3, 4):
        word = data[i : i + 4]
        addr = address + i
        text = None
        if _CS is not None:
            for insn in _CS.disasm(word, addr, 1):
                text = f"{insn.mnemonic:<10} {insn.op_str}".rstrip()
        if text is None:
            text = f".long      0x{struct.unpack('>I', word)[0]:08x}"
        prefix = "=>" if mark is not None and addr == mark else "  "
        lines.append(f"{prefix} {addr:08x}:  {word.hex()}  {text}")
    return lines


def backtrace(read_u32, sp: int, lr: int | None = None, max_frames: int = 32) -> list[int]:
    """Walk the Xbox 360 back-chain.

    Xbox 360 titles run 32-bit pointers on a 64-bit PPC: each frame's first word is the
    back-chain pointer and the caller's LR is saved at (back_chain - 8).
    """
    frames: list[int] = []
    if lr:
        frames.append(lr)
    seen = set()
    for _ in range(max_frames):
        if not sp or sp in seen or sp & 3:
            break
        seen.add(sp)
        back = read_u32(sp)
        if not back or back <= sp or back - sp > 0x100000:
            break
        saved_lr = read_u32(back - 8)
        if saved_lr and (not frames or saved_lr != frames[-1]):
            frames.append(saved_lr)
        sp = back
    return frames
