"""The one-time move of the laptop folders from the old name (myagent) to the new one (wattson).

Before 23 Oct 2026 the CLI kept its files in ~/.config/myagent (config.json, token,
kube/<context>.yaml) and ~/.local/state/myagent (tasks/<task>.json). Now they are in
~/.config/wattson and ~/.local/state/wattson. When a new folder is missing and the old one is
there, these files are COPIED into the new folder: files get mode 600, folders mode 700. A new
folder with no file in it (only empty folders) counts as missing. The old folders are never
changed or deleted: delete them yourself when all works.

Only the default places are moved: a folder set with WATTSON_CONFIG_DIR or WATTSON_STATE_DIR is
never filled from an old one.

The CLI runs this before each command (src/wattson/cli/main.py). k8s/deploy.sh runs this file too,
before it writes config.json and the token (python3 -I src/wattson/cli/migrate.py): so the
read-only kubeconfigs of `k8s add` are not lost when the deploy comes first. That is why it uses
the standard library only.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

OLD_NAME = "myagent"
NEW_NAME = "wattson"


def _folders(home: Path) -> list[tuple[str, Path, tuple[str, ...]]]:
    """(the variable that moves the new folder, the parent of both folders, what to copy)."""
    return [
        ("WATTSON_CONFIG_DIR", home / ".config", ("config.json", "token", "kube/*.yaml")),
        ("WATTSON_STATE_DIR", home / ".local" / "state", ("tasks/*.json",)),
    ]


def _files(old: Path, patterns: tuple[str, ...]) -> list[Path]:
    """The regular files of `old` that match the patterns (no links, no folders)."""
    found: list[Path] = []
    for pattern in patterns:
        for path in sorted(old.glob(pattern)):
            if path.is_file() and not path.is_symlink():
                found.append(path)
    return found


def _has_files(folder: Path) -> bool:
    """True when `folder` is not a plain folder, or holds a file or a link anywhere inside."""
    if folder.is_symlink() or not folder.is_dir():
        return True
    return any(path.is_symlink() or not path.is_dir() for path in folder.rglob("*"))


def _remove_empty(folder: Path) -> None:
    """Remove a tree of empty folders. os.rmdir refuses a folder with something in it."""
    for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        os.rmdir(path)
    os.rmdir(folder)


def _copy_private(source: Path, target: Path) -> None:
    """Copy one file: mode 600, in folders of mode 700."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
        shutil.copyfileobj(src, out)
    os.chmod(target, 0o600)


def _copy_folder(old: Path, new: Path, files: list[Path]) -> None:
    """Copy the files into a temporary folder next to `new`, then rename it to `new`: a copy that
    fails half way leaves no half-filled new folder."""
    temp = Path(tempfile.mkdtemp(prefix=f".{NEW_NAME}-copy-", dir=new.parent))  # mode 700
    try:
        for source in files:
            _copy_private(source, temp / source.relative_to(old))
        os.chmod(temp, 0o700)
        if os.path.lexists(new):
            _remove_empty(new)  # only empty folders: _has_files said so
        os.rename(temp, new)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def migrate(
    home: Path | None = None, environ: Mapping[str, str] | None = None
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Copy each old folder whose new folder is missing. Returns the (old, new) pairs that were
    copied, and a line for each copy that failed."""
    home = home or Path.home()
    environ = os.environ if environ is None else environ
    copied: list[tuple[Path, Path]] = []
    problems: list[str] = []
    for variable, parent, patterns in _folders(home):
        if environ.get(variable, "").strip():
            continue  # a folder of its own: never filled from the old one
        old, new = parent / OLD_NAME, parent / NEW_NAME
        if not old.is_dir() or (os.path.lexists(new) and _has_files(new)):
            continue
        try:
            files = _files(old, patterns)
            if not files:
                continue
            _copy_folder(old, new, files)
        except OSError as exc:
            problems.append(f"could not copy {old} to {new}: {exc.strerror or exc}")
            continue
        copied.append((old, new))
    return copied, problems


def _short(path: Path, home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def message(copied: list[tuple[Path, Path]], home: Path | None = None) -> str | None:
    """One line that says what was copied, or None."""
    if not copied:
        return None
    home = home or Path.home()
    pairs = ", ".join(f"{_short(old, home)} to {_short(new, home)}" for old, new in copied)
    return f"Copied your settings of the old name {OLD_NAME}: {pairs}. The old folders are kept."


def main() -> int:
    copied, problems = migrate()
    line = message(copied)
    if line:
        print(line)
    for problem in problems:
        print(f"Warning: {problem}", file=sys.stderr)
    return 0  # a failed copy never stops the caller: it only loses the old files


if __name__ == "__main__":
    sys.exit(main())
