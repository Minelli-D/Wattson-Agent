"""`wattson`: the command line of Wattson Agent, the personal engineering agent.

Exit codes: 0 ok, 1 error, 2 bad usage, 3 rejected, cancelled, or stopped by the Guard.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from wattson.cli import __version__, migrate
from wattson.cli.a2a_client import AgentError
from wattson.cli.commands import ask, grimoire, local, tasks
from wattson.cli.commands.common import ERROR, OK, STOPPED, USAGE, UsageError
from wattson.cli.config import ConfigError
from wattson.cli.portforward import PortForwardError, stop_all
from wattson.cli.ui import UI, Cancelled

EPILOG = """first time:
  wattson k8s add my-cluster      give Wattson Agent read-only access to a cluster
  wattson grimoire connect     log the agent in to Grimoire
  wattson status               check that everything answers

every day:
  wattson ask "why does the api deployment restart?"

exit codes: 0 ok, 1 error, 2 bad usage, 3 rejected, cancelled, or stopped by the Guard"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(USAGE, f"{self.prog}: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="wattson",
        description="Wattson Agent, your personal engineering agent. It prepares tasks for Claude Code "
        "from your request, your Grimoire procedures and evidence from this laptop. "
        "Nothing runs without your yes.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"Wattson Agent {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)
    ask.add_parser(sub)
    tasks.add_parsers(sub)
    local.add_parsers(sub)
    grimoire.add_parsers(sub)
    return parser


def main(argv: Sequence[str] | None = None, ui: UI | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version, or a usage error
        return exc.code if isinstance(exc.code, int) else (OK if exc.code is None else USAGE)
    if not getattr(args, "func", None):
        parser.print_help()
        return USAGE
    ui = ui or UI()
    _copy_old_folders(ui)
    try:
        return int(args.func(ui, args))
    except UsageError as exc:
        ui.error(str(exc))
        return USAGE
    except (ConfigError, AgentError, PortForwardError) as exc:
        ui.error(str(exc))
        return ERROR
    except (Cancelled, KeyboardInterrupt):
        ui.info("Stopped.")
        return STOPPED
    finally:
        stop_all()


def _copy_old_folders(ui: UI) -> None:
    """Once: the settings of the old name (~/.config/myagent) are copied to the new folders."""
    copied, problems = migrate.migrate()
    line = migrate.message(copied)
    if line:
        ui.info(line)
    for problem in problems:
        ui.warn(problem)


if __name__ == "__main__":
    sys.exit(main())
