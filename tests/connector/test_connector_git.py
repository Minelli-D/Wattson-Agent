from pathlib import Path

import pytest
from connector_helpers import git

from wattson.connector import git as cgit
from wattson.connector.run import Refused


def test_detect_repo(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(repo.parent))
    git(repo, "remote", "add", "origin", "https://alice:s3cret@github.com/acme/demo.git")
    (repo / "README.md").write_text("changed\n")
    (repo / "new.txt").write_text("untracked\n")
    found = cgit.detect_repo(repo / "src" / "util")
    assert found is not None
    info, root = found
    assert root == repo.resolve()
    assert info.name == "repo"
    assert info.root_display == "~/repo"
    assert info.branch == "main"
    assert info.head == git(repo, "rev-parse", "--short", "HEAD").strip()
    assert info.remote == "https://github.com/acme/demo.git"
    assert info.dirty_files == 2


def test_detect_repo_outside_a_repo(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cgit.detect_repo(plain) is None


def test_detect_repo_without_commits_or_remote(make_repo):
    root = make_repo("empty", commit=False)
    info, _ = cgit.detect_repo(root)
    assert info.branch == "main" and info.head is None and info.remote is None and info.dirty_files == 0


@pytest.mark.parametrize(
    ("url", "clean"),
    [
        ("https://user:pass@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://ghp_tokenonly@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://user:p@ss@example.com/r.git", "https://example.com/r.git"),
        ("ssh://git:pw@host:2222/r.git", "ssh://git@host:2222/r.git"),
        ("ssh://git@host/r.git", "ssh://git@host/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("/srv/git/r.git", "/srv/git/r.git"),
    ],
)
def test_strip_credentials(url: str, clean: str):
    assert cgit.strip_credentials(url) == clean


def test_status(repo: Path):
    (repo / "README.md").write_text("changed\n")
    res = cgit.git_status(repo)
    assert res.ok and res.output.startswith("## main") and " M README.md" in res.output
    assert res.source == "git status --short --branch"


def test_status_is_capped(repo: Path):
    for i in range(150):
        (repo / f"u{i:03d}.txt").write_text("x")
    res = cgit.git_status(repo)
    assert res.truncated and len(res.output.splitlines()) == cgit.STATUS_MAX_LINES


def test_log(repo: Path):
    res = cgit.git_log(repo)
    lines = res.output.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("second commit") and lines[1].endswith("first commit")
    assert cgit.git_log(repo, 1).output.count("\n") == 1
    with pytest.raises(Refused):
        cgit.git_log(repo, 51)


def test_log_and_diffstat_without_commits(make_repo):
    root = make_repo("empty", commit=False)
    assert cgit.git_log(root).output == "(no commits yet)\n"
    assert cgit.git_diffstat(root).output == "(no commits yet)\n"


def test_tree(repo: Path):
    one = cgit.git_tree(repo, 1).output
    assert "src/ (2 files)" in one and "README.md" in one and "util/" not in one
    two = cgit.git_tree(repo).output
    assert "  util/ (1 file)" in two and "  app.py" in two
    assert two.splitlines()[0].endswith("tracked files")


def test_tree_is_capped():
    paths = [f"d{i:03d}/f.txt" for i in range(400)]
    text, cut = cgit.summarise_tree(paths, 1)
    assert cut and len(text.splitlines()) == cgit.TREE_MAX_LINES and "more lines" in text


def test_diffstat(repo: Path):
    assert cgit.git_diffstat(repo).output == "(no changes against HEAD)\n"
    (repo / "README.md").write_text("changed\n")
    res = cgit.git_diffstat(repo)
    assert "README.md" in res.output and "1 file changed" in res.output


def test_file_reads_a_tracked_file(repo: Path):
    res = cgit.git_file(repo, "./src/app.py")
    assert res.ok and "DATABASE_URL" in res.output and not res.truncated
    assert res.source == "read src/app.py (working tree)"


def test_file_is_cut_at_12_kb(repo: Path):
    res = cgit.git_file(repo, "big.txt")
    assert res.truncated and len(res.output.encode()) == cgit.FILE_MAX_BYTES


@pytest.mark.parametrize(
    ("path", "why"),
    [
        (".env", "secret"),
        ("config/secrets.yaml", "secret"),
        ("../etc/passwd", "'..'"),
        ("src/../.env", "'..'"),
        ("/etc/passwd", "relative"),
        ("~/.ssh/id_rsa", "relative"),
        (".git/config", ".git"),
        ("", "needed"),
    ],
)
def test_file_refusals_before_running(path: str, why: str):
    with pytest.raises(Refused, match=why):
        cgit.normalise_repo_path(path)


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("untracked.txt", "not a tracked file"),
        ("outside", "outside the repository"),
        ("env-link", "secret-looking"),
        ("blob.bin", "binary"),
        ("src", "not a tracked file"),
    ],
)
def test_file_refusals_while_running(repo: Path, path: str, why: str):
    (repo / "untracked.txt").write_text("x")
    with pytest.raises(Refused, match=why):
        cgit.git_file(repo, path)


def test_grep(repo: Path):
    res = cgit.git_grep(repo, "DATABASE_URL")
    lines = res.output.splitlines()
    assert "README.md:2:The api reads DATABASE_URL at start." in lines
    assert "src/app.py:2:URL = os.environ['DATABASE_URL']" in lines
    assert not any(line.startswith(".env") for line in lines)
    assert "left out" in res.output  # the .env line was found and dropped


def test_grep_fixed_string_and_dash_pattern(repo: Path):
    assert "src/app.py:3:print('-v flag')" in cgit.git_grep(repo, "-v flag").output
    assert cgit.git_grep(repo, "os.environ[").ok
    assert cgit.git_grep(repo, "no such text anywhere").output == "(no matches)\n"


def test_grep_limits(repo: Path):
    res = cgit.git_grep(repo, "long file")
    assert res.truncated and len(res.output.splitlines()) == cgit.GREP_MAX_LINES
    with pytest.raises(Refused, match="200"):
        cgit.git_grep(repo, "x" * 201)
    with pytest.raises(Refused, match="one line"):
        cgit.git_grep(repo, "a\nb")
