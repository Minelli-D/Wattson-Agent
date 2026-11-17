"""The terminal UI: rich output, plain line-based input.

Text from the agent, the cluster or the repo is never parsed as rich markup (it could hold "[...]").
Colours are off when NO_COLOR is set or the output is not a terminal; input works line by line,
so a script (or a test) can drive every question through stdin.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence
from typing import IO

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class Cancelled(Exception):
    """The user stopped a question (Ctrl-C, end of input, or "cancel")."""


class UI:
    def __init__(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None, width: int | None = None):
        self.stdin = stdin if stdin is not None else sys.stdin
        out = stdout if stdout is not None else sys.stdout
        self.out_is_tty = _isatty(out)
        self.in_is_tty = _isatty(self.stdin)
        no_color = bool(os.environ.get("NO_COLOR")) or not self.out_is_tty
        self.console = Console(
            file=out,
            no_color=no_color,
            highlight=False,
            emoji=False,
            width=width or (None if self.out_is_tty else 120),
            soft_wrap=not self.out_is_tty,
        )

    @property
    def interactive(self) -> bool:
        return self.out_is_tty and self.in_is_tty

    # ---- output ---------------------------------------------------------------------------

    def line(self, text: str = "", style: str | None = None) -> None:
        self.console.print(Text(text, style=style or ""))

    def info(self, text: str) -> None:
        self.line(text)

    def ok(self, text: str) -> None:
        self.line(text, "green")

    def dim(self, text: str) -> None:
        self.line(text, "dim")

    def warn(self, text: str) -> None:
        self.console.print(Text("Warning: ", style="bold yellow") + Text(text, style="yellow"))

    def error(self, text: str) -> None:
        self.console.print(Text("Error: ", style="bold red") + Text(text, style="red"))

    def heading(self, text: str) -> None:
        self.console.print()
        self.console.print(Text(text, style="bold underline"))

    def kv(self, rows: Iterable[tuple[str, object]]) -> None:
        table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
        table.add_column(style="bold", no_wrap=True)
        table.add_column(overflow="fold")
        for key, value in rows:
            table.add_row(Text(key), Text("-" if value is None or value == "" else str(value)))
        self.console.print(table)

    def table(
        self, columns: Sequence[str], rows: Iterable[Sequence[object]], title: str | None = None
    ) -> None:
        table = Table(title=title, show_lines=False, header_style="bold", title_justify="left")
        for column in columns:
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(*(Text("-" if v is None or v == "" else str(v)) for v in row))
        self.console.print(table)

    def panel(self, text: str, title: str | None = None, style: str = "") -> None:
        self.console.print(Panel(Text(text), title=title, title_align="left", border_style=style or "none"))

    def warnings(self, items: Sequence[str]) -> None:
        """Warnings are always shown, and shown where they cannot be missed (NFR-09)."""
        if not items:
            return
        body = "\n".join(f"- {w}" for w in items)
        self.console.print(
            Panel(Text(body, style="yellow"), title="Warnings", title_align="left", border_style="yellow")
        )

    def pager(self, text: str) -> None:
        if self.interactive:
            with self.console.pager(styles=False):
                self.console.print(Text(text))
        else:
            self.console.print(Text(text))

    # ---- input ----------------------------------------------------------------------------

    def read_line(self, prompt: str) -> str:
        """One line from the user. Ctrl-C and end of input raise Cancelled."""
        try:
            if self.stdin is sys.stdin and self.in_is_tty:
                return self.console.input(Text(prompt, style="bold"))
            self.console.print(Text(prompt, style="bold"), end="")
            line = self.stdin.readline()
            if line == "":
                self.console.print()
                raise Cancelled("end of input")
            self.console.print(line.rstrip("\n"), markup=False, highlight=False)
            return line.rstrip("\r\n")
        except (KeyboardInterrupt, EOFError) as exc:
            self.console.print()
            raise Cancelled(str(exc) or "stopped") from exc

    def ask(self, prompt: str, default: str | None = None) -> str:
        hint = f" [{default}]" if default else ""
        answer = self.read_line(f"{prompt}{hint}: ").strip()
        return answer or (default or "")

    def confirm(self, prompt: str, default: bool) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self.read_line(f"{prompt} {hint} ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.line("Please answer y (yes) or n (no).")

    def typed_yes(self, prompt: str, quick: bool = False) -> bool:
        """True only when the user types the word yes (FR-APP-04). Anything else is a no.
        quick (prompts: "quick" in config.json) also accepts a single y."""
        answer = self.read_line(f"{prompt} ").strip().lower()
        return answer == "yes" or (quick and answer == "y")

    def choose(self, prompt: str, options: Sequence[str], extra: dict[str, str] | None = None) -> int | str:
        """A numbered choice. Returns the index of the option, or the key of an extra choice."""
        for number, option in enumerate(options, start=1):
            self.line(f"  {number}. {option}")
        for key, label in (extra or {}).items():
            self.line(f"  {key}. {label}")
        while True:
            answer = self.read_line(f"{prompt}: ").strip().lower()
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            if extra and answer in extra:
                return answer
            if answer == "cancel":
                raise Cancelled("cancel")
            keys = ([f"a number from 1 to {len(options)}"] if options else []) + list(extra or {})
            self.line(f"Please type one of: {', '.join(keys)}.")


def _isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return False


def short(text: str | None, limit: int = 80) -> str:
    """One line, at most `limit` characters."""
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."
