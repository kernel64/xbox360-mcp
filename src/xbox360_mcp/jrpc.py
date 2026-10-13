"""Optional helpers for the JRPC2 xbdm plugin (very common on RGH/JTAG consoles).

JRPC2 extends XBDM with `consolefeatures ver=2 type=N params="..."`.
If the plugin is not loaded, xbdm answers `200- DEBUGGING` (or 407 on some versions) and
these helpers return None instead of raising.
"""

from __future__ import annotations

from .client import XboxConsole
from .protocol import XbdmError

_INT = 1
_STRING = 2

TEMPERATURE_SENSORS = {"cpu": 0, "gpu": 1, "memory": 2, "board": 3}


def _call(xbox: XboxConsole, type_id: int, params: str) -> str | None:
    try:
        resp = xbox.cmd(f'consolefeatures ver=2 type={type_id} params="{params}"')
    except XbdmError:
        return None
    msg = resp.message.strip()
    # without JRPC2, xbdm itself answers every `consolefeatures` with "200- DEBUGGING"
    if msg.upper() == "DEBUGGING":
        return None
    return msg


def available(xbox: XboxConsole) -> bool:
    return kernel_version(xbox) is not None


def xnotify(xbox: XboxConsole, text: str, icon: int = 34) -> bool:
    """Show a toast notification on the TV."""
    hex_text = text.encode("utf-8").hex()
    params = f"A\\0\\A\\2\\{_STRING}/{len(text.encode('utf-8'))}\\{hex_text}\\{_INT}\\{icon}\\"
    return _call(xbox, 12, params) is not None


def kernel_version(xbox: XboxConsole) -> str | None:
    return _call(xbox, 13, "A\\0\\A\\0\\")


def title_id(xbox: XboxConsole) -> str | None:
    v = _call(xbox, 16, "A\\0\\A\\0\\")
    if v is None:
        return None
    try:
        return f"{int(v, 16):08X}"
    except ValueError:
        return v


def motherboard(xbox: XboxConsole) -> str | None:
    return _call(xbox, 17, "A\\0\\A\\0\\")


def temperature(xbox: XboxConsole, sensor: str) -> float | None:
    sid = TEMPERATURE_SENSORS[sensor]
    v = _call(xbox, 15, f"A\\0\\A\\1\\{_INT}\\{sid}\\")
    if v is None:
        return None
    try:
        return float(int(v, 16))
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return None


def temperatures(xbox: XboxConsole) -> dict[str, float | None] | None:
    temps = {name: temperature(xbox, name) for name in TEMPERATURE_SENSORS}
    if all(v is None for v in temps.values()):
        return None
    return temps
