"""Xbox 360 RGH toolbox: XBDM client library, CLI and MCP server for Claude Code."""

from .client import XboxConsole
from .protocol import XbdmConnectionError, XbdmError

__all__ = ["XboxConsole", "XbdmError", "XbdmConnectionError"]
__version__ = "0.1.0"
