"""Claude Code on the laptop (FR-CC-01..07, docs/design.md 10.4): find the sessions of a repository,
send an approved prompt to one of them (or a new one), and read the reply back from the session
file. Claude Code keeps its own permission questions: no bypass flag is ever passed (FR-CC-05).

Session files: `<claude home>/projects/<cwd with every non-alphanumeric char as "-">/<id>.jsonl`,
one JSON object per line. The lines used here: `type` ("user" / "assistant" / others),
`timestamp` (ISO, UTC), `isSidechain`, `isMeta`, `isCompactSummary`, and `message.content`
(a string, or a list of blocks with `type` "text" / "tool_use" / "tool_result" / "thinking")."""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattson.shared.contracts import RunResponse, SessionChoice
from wattson.shared.redact import redact

HEADLESS_TIMEOUT_S = 1800.0
OPEN_WINDOW_S = 120  # a session written this recently may be open in another window
SUMMARY_MAX = 2000
ERROR_MAX = 500
FIRST_PROMPT_MAX = 100
ARG_PROMPT_MAX = 100_000  # Linux allows 128 kB in one argument; headless uses stdin instead
STOP_GRACE_S = 10.0  # after terminate, before kill
CLOCK_SLACK = timedelta(seconds=2)
NOT_FOUND = "Claude Code CLI not found"

# FR-CC-05: never passed, and checked before every start.
FORBIDDEN_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--permission-mode",
)

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_UUID_NAME = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# User lines that are not something the person typed.
_NOT_A_PROMPT = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<system-reminder>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-prompt-submit-hook>",
    "<task-notification>",
    "Caveat:",
    "[Request interrupted",
)


@dataclass
class SessionInfo:
    id: str
    first_prompt: str  # redacted, at most 100 characters
    last_activity: str  # ISO time of the file's last change
    messages: int  # prompts typed + assistant messages
    maybe_open: bool  # written in the last 2 minutes: maybe open in another window


# ---- where the sessions are -------------------------------------------------------------------


def claude_home() -> Path:
    for var in ("WATTSON_CLAUDE_HOME", "CLAUDE_CONFIG_DIR"):
        value = os.environ.get(var)
        if value:
            return Path(value).expanduser()
    return Path.home() / ".claude"


def project_dir_name(cwd: Path | str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def project_dir(cwd: Path) -> Path:
    """The folder of `cwd`'s sessions (it may not exist yet)."""
    base = claude_home() / "projects"
    candidates = [Path(cwd).resolve(), Path(cwd).absolute()]
    for path in candidates:
        name = project_dir_name(path)
        if (base / name).is_dir():
            return base / name
        if len(name) > 200:  # Claude Code cuts long names and adds a hash
            found = sorted(base.glob(glob.escape(name[:200]) + "-*"))
            if len(found) == 1:
                return found[0]
    return base / project_dir_name(candidates[0])


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def valid_session_id(session_id: str | None) -> bool:
    return bool(session_id) and _SESSION_ID.fullmatch(session_id) is not None


def session_file(cwd: Path, session_id: str) -> Path:
    if not valid_session_id(session_id):
        raise ValueError(f"not a valid session id: {session_id!r}")
    return project_dir(cwd) / f"{session_id}.jsonl"


# ---- reading session files ---------------------------------------------------------------------


def _lines(path: Path) -> Iterator[dict]:
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue  # a line still being written
            if isinstance(obj, dict):
                yield obj


def _content(obj: dict) -> object:
    msg = obj.get("message")
    return msg.get("content") if isinstance(msg, dict) else None


def user_text(obj: dict) -> str | None:
    """What the person typed, or None for tool results, meta lines and command echoes."""
    if obj.get("type") != "user" or obj.get("isSidechain"):
        return None
    if obj.get("isMeta") or obj.get("isCompactSummary") or obj.get("isVisibleInTranscriptOnly"):
        return None
    content = _content(obj)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None
    text = text.strip()
    if not text or text.startswith(_NOT_A_PROMPT):
        return None
    return text


def assistant_text(obj: dict) -> str | None:
    if obj.get("type") != "assistant" or obj.get("isSidechain"):
        return None
    content = _content(obj)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None
    return text.strip() or None


def parse_time(value: str) -> datetime:
    """An ISO time; one without a zone is taken as UTC. Raises ValueError."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def replies_after(path: Path, since: datetime) -> list[tuple[datetime, str]]:
    """The assistant texts written at or after `since`, oldest first."""
    out = []
    for obj in _lines(path):
        text = assistant_text(obj)
        if text is None:
            continue
        try:
            at = parse_time(str(obj.get("timestamp", "")))
        except ValueError:
            continue
        if at >= since:
            out.append((at, text))
    return out


def _one_line(text: str, limit: int) -> str:
    text = " ".join(redact(text).text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _scan(path: Path) -> tuple[str | None, str | None, int]:
    """(first prompt typed, AI title, number of messages)."""
    first = title = None
    prompts = 0
    assistant_ids: set[str] = set()
    for obj in _lines(path):
        kind = obj.get("type")
        if kind == "ai-title" and isinstance(obj.get("aiTitle"), str):
            title = obj["aiTitle"]
        elif kind == "user":
            text = user_text(obj)
            if text is not None:
                prompts += 1
                first = first or text
        elif kind == "assistant" and not obj.get("isSidechain"):
            msg = obj.get("message")
            mid = msg.get("id") if isinstance(msg, dict) else None
            assistant_ids.add(str(mid or obj.get("uuid") or len(assistant_ids)))
    return first, title, prompts + len(assistant_ids)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _session_files(folder: Path) -> list[tuple[float, Path]]:
    if not folder.is_dir():
        return []
    files = []
    for p in folder.glob("*.jsonl"):
        if _UUID_NAME.fullmatch(p.stem) and p.is_file():
            try:
                files.append((p.stat().st_mtime, p))
            except OSError:
                continue
    files.sort(key=lambda t: t[0], reverse=True)
    return files


def list_sessions(cwd: Path, limit: int = 10) -> list[SessionInfo]:
    """The Claude Code sessions of `cwd`, newest first. Empty sessions are left out."""
    now = time.time()
    out: list[SessionInfo] = []
    for mtime, path in _session_files(project_dir(cwd)):
        if len(out) >= limit:
            break
        first, title, count = _scan(path)
        if count == 0:
            continue
        out.append(
            SessionInfo(
                id=path.stem,
                first_prompt=_one_line(first or title or "(no prompt)", FIRST_PROMPT_MAX),
                last_activity=_iso(mtime),
                messages=count,
                maybe_open=now - mtime < OPEN_WINDOW_S,
            )
        )
    return out


# ---- running Claude Code --------------------------------------------------------------------------


def claude_bin() -> str | None:
    override = os.environ.get("WATTSON_CLAUDE_BIN")
    if override:
        return override if os.path.isfile(override) and os.access(override, os.X_OK) else None
    return shutil.which("claude")


def session_args(session: SessionChoice, new_id: str) -> list[str]:
    """--resume <id> [--fork-session], or --session-id <new id> for a new conversation."""
    if session.id:
        return ["--resume", session.id, *(["--fork-session"] if session.fork else [])]
    return ["--session-id", new_id]


def check_no_bypass(args: list[str]) -> None:
    for arg in args:
        if arg.split("=", 1)[0] in FORBIDDEN_FLAGS:
            raise RuntimeError(f"refusing to start Claude Code with {arg} (FR-CC-05)")


def _summary(text: str | None, note: str = "") -> str | None:
    body = redact(text).text.strip() if text else ""
    room = SUMMARY_MAX - (len(note) + 2 if note else 0)
    if len(body) > room:
        body = body[: max(room - 1, 0)] + "…"
    joined = "\n\n".join(p for p in (body, note) if p)
    return joined[:SUMMARY_MAX] or None


def _error(text: str) -> str:
    return redact(text).text.strip()[:ERROR_MAX]


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _noop(signum, frame) -> None:
    return None


@contextmanager
def _sigint_to_child_only() -> Iterator[None]:
    """While an interactive Claude Code runs, Ctrl-C belongs to it. A Python handler (not
    SIG_IGN) is used so the child starts with the default handler again."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _noop)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, old if old is not None else signal.default_int_handler)


def _stop(proc: subprocess.Popen) -> tuple[bytes, bytes]:
    """Terminate, then kill after STOP_GRACE_S."""
    proc.terminate()
    try:
        return proc.communicate(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


def submit(
    prompt: str,
    cwd: Path,
    session: SessionChoice,
    timeout_s: float = HEADLESS_TIMEOUT_S,
    new_session_id: str | None = None,
) -> RunResponse:
    """Send `prompt` to Claude Code in `cwd` (the repository) and report what happened. Never
    raises for a Claude Code failure; the result is in `state` / `error`. `timeout_s` applies to
    headless runs; an interactive run lasts as long as the person keeps it open.
    `new_session_id`: the id for a NEW conversation, chosen and saved by the caller before the
    run, so `recover` knows it even if the laptop dies mid-run. Not used for resume or fork."""
    if new_session_id is not None and not _is_uuid(new_session_id):
        # claude --session-id takes only a UUID; refuse before anything runs.
        return RunResponse(state="failed", error=_error(f"not a valid new session id: {new_session_id}"))
    new_id = new_session_id or str(uuid.uuid4())
    binary = claude_bin()
    if binary is None:
        return RunResponse(state="failed", session_id=session.id, error=NOT_FOUND)
    if session.id and not valid_session_id(session.id):
        return RunResponse(state="failed", error=_error(f"not a valid session id: {session.id}"))
    if session.mode == "headless":
        return _run_headless(binary, prompt, Path(cwd), session, timeout_s, new_id)
    return _run_interactive(binary, prompt, Path(cwd), session, new_id)


def _run_interactive(binary: str, prompt: str, cwd: Path, session: SessionChoice, new_id: str) -> RunResponse:
    if len(prompt.encode("utf-8")) > ARG_PROMPT_MAX:
        return RunResponse(
            state="failed",
            session_id=session.id,
            error="the prompt is too long for an interactive start; use --headless (it reads stdin)",
        )
    args = [binary, *session_args(session, new_id)]
    if prompt.startswith("-"):
        args.append("--")  # the prompt is not an option
    args.append(prompt)
    check_no_bypass(args)

    folder = project_dir(cwd)
    before = {p for _, p in _session_files(folder)}
    started = datetime.now(UTC) - CLOCK_SLACK
    t0 = time.monotonic()
    stopped_by_user = False
    try:
        with _sigint_to_child_only():
            proc = subprocess.Popen(args, cwd=str(cwd))  # noqa: S603 - a list, stdio is the terminal's
            try:
                rc = proc.wait()
            except KeyboardInterrupt:  # only when not on the main thread
                stopped_by_user = True
                try:
                    rc = proc.wait(timeout=STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    _stop(proc)
                    rc = proc.returncode
    except OSError as exc:
        return RunResponse(
            state="failed",
            session_id=session.id,
            error=_error(f"cannot start Claude Code: {exc}"),
            duration_ms=_ms(t0),
        )
    duration = _ms(t0)

    # Which session file holds this run.
    folder = project_dir(cwd)
    if session.id and session.fork:
        new_files = [(m, p) for m, p in _session_files(folder) if p not in before and p.stem != session.id]
        sid = new_files[0][1].stem if new_files else None
    else:
        sid = session.id or new_id
    replies = replies_after(folder / f"{sid}.jsonl", started) if sid else []
    summary = _summary(replies[-1][1]) if replies else None

    if rc is not None and rc < 0:
        return RunResponse(
            state="interrupted",
            session_id=sid,
            summary=summary,
            exit_code=rc,
            duration_ms=duration,
            error=f"Claude Code was stopped by signal {-rc}",
        )
    if stopped_by_user:
        return RunResponse(
            state="interrupted",
            session_id=sid,
            summary=summary,
            exit_code=rc,
            duration_ms=duration,
            error="stopped with Ctrl-C",
        )
    if rc == 0 and replies:
        return RunResponse(
            state="completed", session_id=sid, summary=summary, exit_code=0, duration_ms=duration
        )
    if rc == 0:
        return RunResponse(
            state="interrupted",
            session_id=sid,
            exit_code=0,
            duration_ms=duration,
            error="Claude Code closed without a reply in the session",
        )
    if replies:
        return RunResponse(
            state="interrupted",
            session_id=sid,
            summary=summary,
            exit_code=rc,
            duration_ms=duration,
            error=f"Claude Code exited with code {rc} after replying",
        )
    return RunResponse(
        state="failed",
        session_id=sid,
        exit_code=rc,
        duration_ms=duration,
        error=f"Claude Code exited with code {rc} and no reply",
    )


def parse_result(stdout: str) -> dict | None:
    """The result object of `claude -p --output-format json` (one object; some versions print a
    list of messages whose last one is the result)."""
    text = stdout.strip()
    if not text:
        return None
    candidates: list[object] = []
    try:
        candidates.append(json.loads(text))
    except ValueError:
        for line in reversed(text.splitlines()):
            try:
                candidates.append(json.loads(line))
                break
            except ValueError:
                continue
    for obj in candidates:
        if isinstance(obj, list):
            results = [o for o in obj if isinstance(o, dict) and o.get("type") == "result"]
            obj = results[-1] if results else None
        if isinstance(obj, dict) and ("result" in obj or obj.get("type") == "result"):
            return obj
    return None


def denials_note(denials: object) -> str:
    """'Claude Code refused 2 tool uses that need a permission: Bash (2).'"""
    if not isinstance(denials, list) or not denials:
        return ""
    counts: dict[str, int] = {}
    for d in denials:
        name = str(d.get("tool_name") or "a tool") if isinstance(d, dict) else "a tool"
        counts[name] = counts.get(name, 0) + 1
    names = ", ".join(f"{n} ({c})" if c > 1 else n for n, c in counts.items())
    n = len(denials)
    return (
        f"Claude Code refused {n} tool use{'s' if n != 1 else ''} that need{'s' if n == 1 else ''} "
        f"a permission: {names}. Run it interactively to answer them."
    )


def _run_headless(
    binary: str, prompt: str, cwd: Path, session: SessionChoice, timeout_s: float, new_id: str
) -> RunResponse:
    args = [binary, "-p", "--output-format", "json", *session_args(session, new_id)]
    check_no_bypass(args)
    expected_id = None if (session.id and session.fork) else (session.id or new_id)
    started = datetime.now(UTC) - CLOCK_SLACK
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 - a list; the prompt goes on stdin
            args, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        return RunResponse(
            state="failed", session_id=session.id, error=_error(f"cannot start Claude Code: {exc}")
        )
    try:
        out, err = proc.communicate(prompt.encode("utf-8"), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop(proc)
        return RunResponse(
            state="interrupted",
            session_id=expected_id,
            exit_code=proc.returncode,
            duration_ms=_ms(t0),
            error=f"Claude Code timed out after {timeout_s:g} s and was stopped",
        )
    except KeyboardInterrupt:
        _stop(proc)
        return RunResponse(
            state="interrupted",
            session_id=expected_id,
            exit_code=proc.returncode,
            duration_ms=_ms(t0),
            error="stopped with Ctrl-C",
        )
    rc = proc.returncode
    duration = _ms(t0)
    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    result = parse_result(stdout)

    if result is None:
        if rc is not None and rc < 0:
            return RunResponse(
                state="interrupted",
                session_id=expected_id,
                exit_code=rc,
                duration_ms=duration,
                error=f"Claude Code was stopped by signal {-rc}",
            )
        replies = replies_after(project_dir(cwd) / f"{expected_id}.jsonl", started) if expected_id else []
        if rc == 0 and replies:
            return RunResponse(
                state="completed",
                session_id=expected_id,
                summary=_summary(replies[-1][1]),
                exit_code=0,
                duration_ms=duration,
            )
        detail = stderr.strip()[-ERROR_MAX:] or f"exit code {rc}"
        return RunResponse(
            state="failed",
            session_id=expected_id,
            exit_code=rc,
            duration_ms=duration,
            error=_error(f"Claude Code printed no result: {detail}"),
        )

    sid = result.get("session_id") if isinstance(result.get("session_id"), str) else expected_id
    text = result.get("result") if isinstance(result.get("result"), str) else None
    note = denials_note(result.get("permission_denials"))
    subtype = result.get("subtype")
    is_error = bool(result.get("is_error")) or (isinstance(subtype, str) and subtype != "success")
    if rc is not None and rc < 0:
        return RunResponse(
            state="interrupted",
            session_id=sid,
            summary=_summary(text, note),
            exit_code=rc,
            duration_ms=duration,
            error=f"Claude Code was stopped by signal {-rc}",
        )
    if is_error or rc != 0:
        reason = (
            text or (subtype if isinstance(subtype, str) else None) or stderr.strip() or f"exit code {rc}"
        )
        return RunResponse(
            state="failed",
            session_id=sid,
            summary=_summary(None, note),
            exit_code=rc,
            duration_ms=duration,
            error=_error(f"Claude Code reported an error: {reason}"),
        )
    return RunResponse(
        state="completed",
        session_id=sid,
        summary=_summary(text or "(empty reply)", note),
        exit_code=rc,
        duration_ms=duration,
    )


def recover(cwd: Path, session_id: str, since: str) -> RunResponse | None:
    """A late result: the last assistant reply written in the session after `since` (ISO time;
    empty = from the start). None when the session file is missing or holds no reply after
    `since`. Raises ValueError for a `since` that is not an ISO time."""
    try:
        path = session_file(cwd, session_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    start = parse_time(since) if since and since.strip() else datetime.min.replace(tzinfo=UTC)
    replies = replies_after(path, start)
    if not replies:
        return None
    last_at, last_text = replies[-1]
    return RunResponse(
        state="completed",
        session_id=session_id,
        summary=_summary(last_text),
        duration_ms=max(int((last_at - start).total_seconds() * 1000), 0),
    )
