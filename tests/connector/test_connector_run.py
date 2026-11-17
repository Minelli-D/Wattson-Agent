import sys
from pathlib import Path

from wattson.connector.run import limit_lines, run

PY = sys.executable


def test_runs_an_argument_list_and_captures_output():
    res = run([PY, "-c", "print('hi')"])
    assert res.ok and res.returncode == 0 and res.stdout == "hi\n" and res.error is None


def test_no_shell_metacharacters_are_interpreted(tmp_path: Path):
    marker = tmp_path / "should-not-exist"
    arg = f"$(touch {marker}); touch {marker}"
    res = run([PY, "-c", "import sys; print(sys.argv[1])", arg])
    assert res.stdout.strip() == arg
    assert not marker.exists()


def test_missing_program_is_a_clear_error():
    res = run(["/nonexistent/kubectl", "version"])
    assert not res.ok and res.returncode is None and res.error == "kubectl not found"


def test_missing_working_folder(tmp_path: Path):
    res = run([PY, "-c", "pass"], cwd=tmp_path / "nope")
    assert res.error and "working folder not found" in res.error


def test_timeout_kills_the_process():
    res = run([PY, "-c", "import time; time.sleep(10)"], timeout_s=0.5)
    assert res.timed_out and not res.ok
    assert res.error == f"{Path(PY).name} timed out after 0.5 s"
    assert res.duration_ms < 5000


def test_non_zero_exit_keeps_stderr():
    res = run([PY, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"])
    assert not res.ok and res.returncode == 3 and res.stderr == "bad" and res.error is None


def test_output_cap_head_and_tail():
    code = "print('\\n'.join(str(i) for i in range(1000)))"
    head = run([PY, "-c", code], max_bytes=10)
    tail = run([PY, "-c", code], max_bytes=10, keep="tail")
    assert head.truncated and head.stdout.startswith("0\n1\n")
    assert tail.truncated and tail.stdout.endswith("998\n999\n")


def test_input_goes_to_stdin():
    res = run([PY, "-c", "import sys; print(sys.stdin.read().upper())"], input_text="hello")
    assert res.stdout.strip() == "HELLO"


def test_stdin_is_closed_without_input():
    res = run([PY, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout_s=5)
    assert res.stdout.strip() == "''"


def test_limit_lines():
    assert limit_lines("a\nb\nc\n", 2) == ("a\nb\n", True)
    assert limit_lines("a\nb\nc\n", 2, keep="tail") == ("b\nc\n", True)
    assert limit_lines("a\n", 2) == ("a\n", False)
