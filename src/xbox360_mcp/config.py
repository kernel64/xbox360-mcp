"""Configuration via environment variables (set them in .mcp.json / `claude mcp add -e`)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    host: str
    port: int = 730
    timeout: float = 10.0
    read_only: bool = False
    attach_debugger: bool = True
    log_file: str | None = None
    default_deploy_root: str = "Hdd:\\Devkit"

    @classmethod
    def from_env(cls, host: str | None = None) -> "Config":
        h = host or os.environ.get("XBOX360_HOST") or os.environ.get("XBOX_IP")
        if not h:
            raise SystemExit(
                "XBOX360_HOST is not set. Example: XBOX360_HOST=192.168.1.50 (IP of your RGH running xbdm)."
            )
        read_only = _bool("XBOX360_READ_ONLY", False)
        return cls(
            host=h,
            port=int(os.environ.get("XBOX360_PORT", "730")),
            timeout=float(os.environ.get("XBOX360_TIMEOUT", "10")),
            read_only=read_only,
            # attaching changes how a crash behaves (the title freezes): not by default in read-only mode
            attach_debugger=_bool("XBOX360_ATTACH_DEBUGGER", not read_only),
            log_file=os.environ.get("XBOX360_LOG_FILE") or None,
            default_deploy_root=os.environ.get("XBOX360_DEPLOY_ROOT", "Hdd:\\Devkit"),
        )
