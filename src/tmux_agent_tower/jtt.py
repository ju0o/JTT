"""Standalone JTT entry point; separate from the legacy tmux Tower command."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .ui.jtt import run_jtt_ui
from .jtt_install import installation_kind


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jtt", description="JTT — Ju0 Terminal Tower")
    parser.add_argument("--version", action="store_true", help="show version and installation channel")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("doctor", help="run read-only environment diagnostics")
    update = commands.add_parser("update", help="check or apply a verified official Release")
    update_mode = update.add_mutually_exclusive_group()
    update_mode.add_argument("--check", action="store_true", help="check for an update without changing files")
    update_mode.add_argument("--dry-run", action="store_true", help="show the update plan without changing files")
    update.add_argument("--channel", choices=("auto", "stable", "rc"), default="auto")
    rollback = commands.add_parser("rollback", help="restore a previously retained official version")
    rollback.add_argument("--dry-run", action="store_true", help="show the rollback target without changing files")
    args = parser.parse_args(argv)
    if args.version and args.command:
        parser.error("--version cannot be combined with a command")
    if args.version:
        print(f"jtt {__version__} [{installation_kind().upper()}]")
        return 0
    if args.command == "doctor":
        from .jtt_diagnostics import run_doctor

        return run_doctor()
    if args.command == "update":
        from .jtt_update import run_update_cli

        action = "check" if args.check else "dry-run" if args.dry_run else "apply"
        return run_update_cli(action, requested_channel=args.channel)
    if args.command == "rollback":
        from .jtt_update import run_rollback_cli

        return run_rollback_cli(dry_run=args.dry_run)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("JTT는 대화형 터미널에서 실행해야 합니다.", file=sys.stderr)
        return 2
    try:
        return run_jtt_ui()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
