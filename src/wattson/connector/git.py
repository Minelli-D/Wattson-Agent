"""Git context for a task: which repository this is (FR-GIT-01), and the small read-only views the
orchestrator may ask for (FR-GIT-03/04). Only `git` with an argument list; no file is sent whole
unless it is one tracked, non-secret, text file of at most 12 kB."""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from pathlib import Path

from wattson.connector.run import OpResult, Refused, RunResult, limit_lines, run
from wattson.shared.contracts import EvidenceRequest, RepoInfo
from wattson.shared.redact import is_secret_path

GIT_TIMEOUT_S = 20.0
STATUS_MAX_LINES = 100
LOG_DEFAULT, LOG_MAX = 15, 50
TREE_DEFAULT, TREE_MAX = 2, 6
TREE_MAX_LINES = 300
DIFFSTAT_MAX_LINES = 150
FILE_MAX_BYTES = 12 * 1024
GREP_MAX_LINES = 50
GREP_PATTERN_MAX = 200
GREP_LINE_MAX = 300

# No pager, no prompt for a password, no optional index lock (we only read), English messages.
_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "LC_ALL": "C",
}
# A repository's own config must not make a read run a program (fsmonitor hook, external diff).
_GIT_SAFE = ["--no-pager", "-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "color.ui=false"]


def _git(root: Path, args: list[str], timeout_s: float = GIT_TIMEOUT_S, **kw) -> RunResult:
    return run(["git", "-C", str(root), *_GIT_SAFE, *args], env=_GIT_ENV, timeout_s=timeout_s, **kw)


# ---- repository detection ------------------------------------------------------------------


def strip_credentials(url: str) -> str:
    """Remove `user:password@` from a remote URL. For http(s) any user part goes (it may be a
    token); for ssh/git URLs the user name stays and only a password goes. scp-like
    `git@host:path` has no password and is kept."""
    url = url.strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*://)(.*)$", url, re.DOTALL)
    if not m:
        return url
    scheme, rest = m.groups()
    authority, slash, path = rest.partition("/")
    if "@" not in authority:
        return url
    userinfo, _, host = authority.rpartition("@")
    if scheme.lower().startswith(("http", "ftp")):
        return f"{scheme}{host}{slash}{path}"
    user = userinfo.split(":", 1)[0]
    return f"{scheme}{user}@{host}{slash}{path}" if user else f"{scheme}{host}{slash}{path}"


def display_path(path: Path) -> str:
    """The path with the home folder written as "~"."""
    home_variants = {Path.home()}
    try:
        home_variants.add(Path.home().resolve())
    except OSError:
        pass
    for home in home_variants:
        if path == home:
            return "~"
        if path.is_relative_to(home):
            return "~/" + path.relative_to(home).as_posix()
    return str(path)


def _out(res: RunResult) -> str | None:
    value = res.stdout.strip()
    return value if res.ok and value else None


def detect_repo(cwd: Path) -> tuple[RepoInfo, Path] | None:
    """The git repository that contains `cwd`, or None. Returns the small `RepoInfo` sent with a
    task (no file contents) and the repository's root folder."""
    top = _out(_git(Path(cwd), ["rev-parse", "--show-toplevel"]))
    if top is None:
        return None
    root = Path(top)
    branch = _out(_git(root, ["symbolic-ref", "--short", "-q", "HEAD"]))
    head = _out(_git(root, ["rev-parse", "--short", "--verify", "-q", "HEAD"]))
    remote = _out(_git(root, ["remote", "get-url", "origin"]))
    status = _git(root, ["status", "--porcelain"])
    dirty = len([line for line in status.stdout.splitlines() if line.strip()]) if status.ok else 0
    info = RepoInfo(
        name=root.name,
        root_display=display_path(root),
        branch=branch,
        head=head,
        remote=strip_credentials(remote) if remote else None,
        dirty_files=dirty,
    )
    return info, root


# ---- checks shared with evidence.check_allowed --------------------------------------------------


def normalise_repo_path(path: str | None) -> str:
    """Check a path the orchestrator asked for and return it in a clean form, or raise Refused."""
    if path is None or not path.strip():
        raise Refused("a file path is needed")
    raw = path.strip()
    if "\x00" in raw or "\n" in raw or "\\" in raw:
        raise Refused("the path has characters that are not allowed")
    if len(raw) > 500:
        raise Refused("the path is too long")
    if raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", raw):
        raise Refused("only a path relative to the repository is allowed")
    parts = raw.split("/")
    if ".." in parts:
        raise Refused("'..' is not allowed in a path")
    clean = posixpath.normpath(raw)
    if clean in (".", "") or clean.startswith("../"):
        raise Refused("the path must name a file inside the repository")
    if clean == ".git" or clean.startswith(".git/"):
        raise Refused("files inside .git are not read")
    if is_secret_path(clean):
        raise Refused(f"'{clean}' looks like a secret file, so it is not read")
    return clean


def check_pattern(pattern: str | None) -> str:
    if pattern is None or not pattern.strip():
        raise Refused("a search text is needed")
    if len(pattern) > GREP_PATTERN_MAX:
        raise Refused(f"the search text is longer than {GREP_PATTERN_MAX} characters")
    if "\n" in pattern or "\x00" in pattern:
        raise Refused("the search text must be one line")
    return pattern


# ---- the operations -----------------------------------------------------------------------------


def _from_run(res: RunResult, source: str, output: str | None = None, truncated: bool = False) -> OpResult:
    if res.error is not None:
        return OpResult(source=source, ok=False, error=res.error)
    if res.returncode != 0:
        msg = res.stderr.strip() or f"git exited with code {res.returncode}"
        return OpResult(source=source, ok=False, error=msg, output=res.stdout)
    return OpResult(
        source=source,
        output=res.stdout if output is None else output,
        truncated=truncated or res.truncated,
    )


def _has_commits(root: Path) -> bool:
    return _git(root, ["rev-parse", "--verify", "-q", "HEAD"]).ok


def git_status(root: Path, timeout_s: float = GIT_TIMEOUT_S) -> OpResult:
    source = "git status --short --branch"
    res = _git(root, ["status", "--short", "--branch"], timeout_s)
    text, cut = limit_lines(res.stdout, STATUS_MAX_LINES)
    return _from_run(res, source, text, cut)


def git_log(root: Path, limit: int | None = None, timeout_s: float = GIT_TIMEOUT_S) -> OpResult:
    n = LOG_DEFAULT if limit is None else limit
    if not 1 <= n <= LOG_MAX:
        raise Refused(f"limit must be 1..{LOG_MAX}")
    source = f"git log -n {n} --date=short --format='%ad %h %s'"
    if not _has_commits(root):
        return OpResult(source=source, output="(no commits yet)\n")
    res = _git(root, ["log", "-n", str(n), "--date=short", "--format=%ad %h %s"], timeout_s)
    return _from_run(res, source)


def summarise_tree(paths: list[str], depth: int, max_lines: int = TREE_MAX_LINES) -> tuple[str, bool]:
    """Folders (with how many files they hold) and files, down to `depth` levels."""
    counts: dict[tuple[str, ...], int] = defaultdict(int)
    dirs_in: dict[tuple[str, ...], set[str]] = defaultdict(set)
    files_in: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for p in paths:
        parts = p.split("/")
        folders = parts[:-1]
        for i, name in enumerate(folders):
            dirs_in[tuple(folders[:i])].add(name)
            counts[tuple(folders[: i + 1])] += 1
        files_in[tuple(folders)].append(parts[-1])

    lines = [f"{len(paths)} tracked files"]

    def walk(prefix: tuple[str, ...], level: int) -> None:
        indent = "  " * level
        for name in sorted(dirs_in[prefix]):
            key = (*prefix, name)
            n = counts[key]
            lines.append(f"{indent}{name}/ ({n} file{'s' if n != 1 else ''})")
            if level + 1 < depth:
                walk(key, level + 1)
        for name in sorted(files_in[prefix]):
            lines.append(f"{indent}{name}")

    walk((), 0)
    if len(lines) > max_lines:
        extra = len(lines) - (max_lines - 1)
        return "\n".join([*lines[: max_lines - 1], f"... {extra} more lines"]) + "\n", True
    return "\n".join(lines) + "\n", False


def git_tree(root: Path, depth: int | None = None, timeout_s: float = GIT_TIMEOUT_S) -> OpResult:
    d = TREE_DEFAULT if depth is None else depth
    if not 1 <= d <= TREE_MAX:
        raise Refused(f"depth must be 1..{TREE_MAX}")
    source = f"git ls-files (folders to depth {d}, with file counts)"
    res = _git(root, ["ls-files", "-z"], timeout_s, max_bytes=50_000_000)
    if not res.ok:
        return _from_run(res, source)
    paths = [p for p in res.stdout.split("\x00") if p]
    text, cut = summarise_tree(paths, d)
    return OpResult(source=source, output=text, truncated=cut)


def git_diffstat(root: Path, timeout_s: float = GIT_TIMEOUT_S) -> OpResult:
    source = "git diff --stat HEAD"
    if not _has_commits(root):
        return OpResult(source=source, output="(no commits yet)\n")
    res = _git(root, ["diff", "--no-ext-diff", "--no-textconv", "--stat=200", "HEAD"], timeout_s)
    if not res.ok:
        return _from_run(res, source)
    lines = res.stdout.splitlines()
    if len(lines) > DIFFSTAT_MAX_LINES:
        # Keep the first lines and the summary line ("N files changed, ...").
        kept = [
            *lines[: DIFFSTAT_MAX_LINES - 2],
            f"... {len(lines) - DIFFSTAT_MAX_LINES + 1} more files",
            lines[-1],
        ]
        return OpResult(source=source, output="\n".join(kept) + "\n", truncated=True)
    return OpResult(source=source, output=res.stdout or "(no changes against HEAD)\n")


def git_file(root: Path, path: str | None, timeout_s: float = GIT_TIMEOUT_S) -> OpResult:
    rel = normalise_repo_path(path)
    source = f"read {rel} (working tree)"
    res = _git(root, ["ls-files", "-z", "--full-name", "--", f":(literal){rel}"], timeout_s)
    if not res.ok:
        return _from_run(res, source)
    if rel not in res.stdout.split("\x00"):
        raise Refused(f"'{rel}' is not a tracked file in this repository")
    root_real = Path(root).resolve()
    full = (root_real / rel).resolve()
    if not full.is_relative_to(root_real):
        raise Refused(f"'{rel}' points outside the repository")
    real_rel = full.relative_to(root_real).as_posix()
    if real_rel != rel and is_secret_path(real_rel):
        raise Refused(f"'{rel}' points to a secret-looking file, so it is not read")
    if not full.is_file():
        raise Refused(f"'{rel}' is not a regular file")
    try:
        with full.open("rb") as fh:
            data = fh.read(FILE_MAX_BYTES + 1)
    except OSError as exc:
        return OpResult(source=source, ok=False, error=f"cannot read '{rel}': {exc.strerror}")
    if b"\x00" in data[:8192]:
        raise Refused(f"'{rel}' is a binary file")
    truncated = len(data) > FILE_MAX_BYTES
    text = data[:FILE_MAX_BYTES].decode("utf-8", errors="replace")
    return OpResult(source=source, output=text, truncated=truncated)


def git_grep(root: Path, pattern: str | None, timeout_s: float = GIT_TIMEOUT_S) -> OpResult:
    pat = check_pattern(pattern)
    source = f"git grep -n -F -e {pat!r}"
    res = _git(root, ["grep", "-n", "-I", "-F", "--null", "-e", pat, "--"], timeout_s, max_bytes=5_000_000)
    if res.error is None and res.returncode == 1:
        return OpResult(source=source, output="(no matches)\n")
    if not res.ok:
        return _from_run(res, source)
    lines: list[str] = []
    skipped = 0
    truncated = res.truncated
    for raw in res.stdout.splitlines():
        fields = raw.split("\x00", 2)
        if len(fields) != 3:
            continue
        fpath, lnum, text = fields
        if is_secret_path(fpath):
            skipped += 1
            continue
        if len(lines) >= GREP_MAX_LINES:
            truncated = True
            continue
        if len(text) > GREP_LINE_MAX:
            text = text[:GREP_LINE_MAX] + " ..."
        lines.append(f"{fpath}:{lnum}:{text}")
    out = "\n".join(lines) + "\n" if lines else "(no matches)\n"
    if skipped:
        out += f"({skipped} matching lines in secret-looking files were left out)\n"
    return OpResult(source=source, output=out, truncated=truncated)


def run_git_op(req: EvidenceRequest, root: Path, timeout_s: float = GIT_TIMEOUT_S) -> OpResult:
    """Run one git evidence request. Raises Refused when a rule says no."""
    match req.op:
        case "git.status":
            return git_status(root, timeout_s)
        case "git.log":
            return git_log(root, req.limit, timeout_s)
        case "git.tree":
            return git_tree(root, req.depth, timeout_s)
        case "git.diffstat":
            return git_diffstat(root, timeout_s)
        case "git.file":
            return git_file(root, req.path, timeout_s)
        case "git.grep":
            return git_grep(root, req.pattern, timeout_s)
    raise Refused(f"unknown git operation: {req.op}")
