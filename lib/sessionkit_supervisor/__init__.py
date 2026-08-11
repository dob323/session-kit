"""Read-only fleet oversight and the lane-gated supervisor MCP server."""

from types import ModuleType
from typing import Any

__all__ = ["KitAdapter", "MCPServer", "SupervisorError", "TOOL_NAMES"]

_LAZY = {
    "KitAdapter": ".adapter",
    "SupervisorError": ".adapter",
    "MCPServer": ".server",
    "TOOL_NAMES": ".server",
}


def __getattr__(name: str) -> Any:
    """Import the MCP surface only for a caller that names part of it.

    The intake producer runs inside a provider hook, in front of the human's
    own prompt. Importing the adapter there — sqlite3, subprocess, the whole
    vendored tool set — costs about thirty milliseconds of somebody's typing
    for code that hook will never call.
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __package__), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


def ratchet_module() -> ModuleType:
    """Import the steward-owned ratchet only when a caller explicitly needs it."""
    from importlib import import_module

    return import_module(f"{__package__}.ratchet")
