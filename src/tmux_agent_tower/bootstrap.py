"""Entry point for the renamed JTT package namespace."""

from __future__ import annotations

# Importing this package first installs the guarded compatibility alias used
# by the shared source modules. The installed wheel itself contains only the
# ju0_terminal_tower package tree.
import ju0_terminal_tower  # noqa: F401

from tmux_agent_tower.jtt import cli


def main() -> int:
    return cli()


if __name__ == "__main__":
    raise SystemExit(main())
