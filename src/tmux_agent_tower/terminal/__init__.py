"""Local, user-owned PTY terminals managed independently of the Tower UI.

Creating a :class:`TerminalRegistry` is side-effect-free beyond ensuring its
private registry directory exists. A shell is started only by an explicit
``registry.create()`` call. The implementation intentionally knows nothing
about tmux, SSH, or agent-specific executables.
"""

from .manager import (
    TerminalClient,
    TerminalCell,
    TerminalError,
    TerminalIdentity,
    TerminalRegistry,
    TerminalScreen,
    TerminalUpdate,
    renderer_available,
)

__all__ = [
    "TerminalClient",
    "TerminalCell",
    "TerminalError",
    "TerminalIdentity",
    "TerminalRegistry",
    "TerminalScreen",
    "TerminalUpdate",
    "renderer_available",
]
