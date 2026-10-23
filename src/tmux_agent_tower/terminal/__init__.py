"""Local, user-owned PTY terminals managed independently of the Tower UI.

Creating a :class:`TerminalRegistry` is side-effect-free beyond ensuring its
private registry directory exists. A shell is started only by an explicit
``registry.create()`` call. The implementation intentionally knows nothing
about tmux, SSH, or agent-specific executables.
"""

import sys

from .manager import (
    TerminalCell,
    TerminalError,
    TerminalIdentity,
    TerminalObservation,
    TerminalScreen,
    TerminalUpdate,
)

if sys.platform == "win32":
    from . import windows_manager as manager
    from .windows_manager import TerminalClient, TerminalRegistry, renderer_available
else:
    from . import manager
    from .manager import (
        TerminalClient,
        TerminalRegistry,
        renderer_available,
    )

__all__ = [
    "TerminalClient",
    "TerminalCell",
    "TerminalError",
    "TerminalIdentity",
    "TerminalObservation",
    "TerminalRegistry",
    "TerminalScreen",
    "TerminalUpdate",
    "renderer_available",
]
