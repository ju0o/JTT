"""Standalone JTT entry point; separate from the legacy tmux Tower command."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .ui.jtt import run_jtt_ui


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jtt", description="Ju0 Terminal Tower")
    parser.add_argument("--version", action="version", version=f"jtt {__version__}")
    args = parser.parse_args(argv)
    del args
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("JTT는 대화형 터미널에서 실행해야 합니다.", file=sys.stderr)
        return 2
    try:
        return run_jtt_ui()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
