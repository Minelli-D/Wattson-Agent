"""Run one local command, safely: an argument list (never a shell), a timeout (then kill), an
output cap, and errors in plain words. Every git and kubectl call of the connector goes here."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_BYTES = 1_000_000  # what is kept of stdout; evidence cuts again to 16 kB after redaction
STDERR_MAX_BYTES = 20_000


@dataclass(frozen=True)
class RunResult:
    args: list[str]
    returncode: int | None  # None: it did not run, or it was killed after the timeout
    stdout: str
    stderr: str
    truncated: bool = False  # stdout was longer than the cap
    timed_out: bool = False
    error: str | None = None  # "kubectl not found", "timed out after 20 s"
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and self.returncode == 0


@dataclass
class OpResult:
    """What one evidence operation produced, before redaction and the 16 kB cut."""

    source: str  # what ran, for people (never a kubeconfig path or a token)
    output: str = ""
    ok: bool = True
    error: str | None = None
    refused: str | None = None  # a rule said no while running (e.g. a symlink out of the repo)
    truncated: bool = False
    keep: str = "head"  # which end evidence keeps when it cuts: "tail" for logs


class Refused(Exception):
    """A rule refused the operation. The message is the reason shown to the user."""


def limit_lines(text: str, max_lines: int, keep: str = "head") -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    kept = lines[-max_lines:] if keep == "tail" else lines[:max_lines]
    return "\n".join(kept) + "\n", True


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _cap(data: bytes, max_bytes: int, keep: str) -> tuple[bytes, bool]:
    if len(data) <= max_bytes:
        return data, False
    if keep == "tail":
        return data[-max_bytes:], True
    return data[:max_bytes], True


def run(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    input_text: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: str = "head",
    env: Mapping[str, str] | None = None,
) -> RunResult:
    """Run `args` (a list; args[0] is the program) and return what happened. Never raises for a
    missing program, a timeout or a non-zero exit: those are in `error` / `returncode`."""
    argv = [str(a) for a in args]
    if not argv:
        raise ValueError("no command given")
    if keep not in ("head", "tail"):
        raise ValueError("keep must be 'head' or 'tail'")
    full_env = None
    if env is not None:
        full_env = dict(os.environ)
        full_env.update(env)
    name = Path(argv[0]).name
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    if cwd is not None and not Path(cwd).is_dir():
        return RunResult(argv, None, "", "", error=f"working folder not found: {cwd}")
    try:
        proc = subprocess.run(  # noqa: S603 - an argument list, never a shell
            argv,
            cwd=str(cwd) if cwd is not None else None,
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_s,
            env=full_env,
            check=False,
        )
    except FileNotFoundError:
        return RunResult(argv, None, "", "", error=f"{name} not found", duration_ms=elapsed())
    except PermissionError:
        return RunResult(
            argv, None, "", "", error=f"{name} cannot be run (permission denied)", duration_ms=elapsed()
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed the process.
        out, truncated = _cap(exc.stdout or b"", max_bytes, keep)
        err, _ = _cap(exc.stderr or b"", STDERR_MAX_BYTES, "tail")
        return RunResult(
            argv,
            None,
            _decode(out),
            _decode(err),
            truncated=truncated,
            timed_out=True,
            error=f"{name} timed out after {timeout_s:g} s",
            duration_ms=elapsed(),
        )
    out, truncated = _cap(proc.stdout, max_bytes, keep)
    err, _ = _cap(proc.stderr, STDERR_MAX_BYTES, "tail")
    return RunResult(
        argv, proc.returncode, _decode(out), _decode(err), truncated=truncated, duration_ms=elapsed()
    )
