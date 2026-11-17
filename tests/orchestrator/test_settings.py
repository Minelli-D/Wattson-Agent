"""Settings from the environment: defaults, clear errors, and tokens never shown."""

from __future__ import annotations

from pathlib import Path

import pytest

from wattson.agents.orchestrator.settings import Settings, SettingsError

from .conftest import KNOWLEDGE_TOKEN, TOKEN, TRIAGE_TOKEN

ALL = (
    "WATTSON_TOKEN",
    "WATTSON_TOKEN_FILE",
    "TRIAGE_TOKEN",
    "TRIAGE_TOKEN_FILE",
    "KNOWLEDGE_TOKEN",
    "KNOWLEDGE_TOKEN_FILE",
    "TRIAGE_URL",
    "KNOWLEDGE_URL",
    "WATTSON_LISTEN_HOST",
    "WATTSON_PORT",
    "WATTSON_DATA_DIR",
    "HISTORY_RETENTION_DAYS",
    "TASK_TTL_MINUTES",
    "TRIAGE_TIMEOUT_S",
    "KNOWLEDGE_TIMEOUT_S",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ALL:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WATTSON_TOKEN", TOKEN)
    monkeypatch.setenv("TRIAGE_TOKEN", TRIAGE_TOKEN)
    monkeypatch.setenv("KNOWLEDGE_TOKEN", KNOWLEDGE_TOKEN)
    monkeypatch.setenv("TRIAGE_URL", "http://wattson-triage:9001/")
    monkeypatch.setenv("KNOWLEDGE_URL", "http://wattson-knowledge:9002")
    return monkeypatch


def test_defaults(env: pytest.MonkeyPatch) -> None:
    s = Settings.from_env()
    assert (s.listen_host, s.port, s.data_dir) == ("127.0.0.1", 9000, Path(".data"))
    assert (s.history_retention_days, s.task_ttl_minutes) == (90, 120)
    assert (s.triage_timeout_s, s.knowledge_timeout_s) == (60.0, 45.0)
    assert s.triage_url == "http://wattson-triage:9001"
    assert TOKEN not in repr(s) and TRIAGE_TOKEN not in repr(s) and KNOWLEDGE_TOKEN not in repr(s)


def test_values_and_token_file(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    env.delenv("WATTSON_TOKEN")
    env.setenv("WATTSON_TOKEN_FILE", str(token_file))
    env.setenv("WATTSON_PORT", "9100")
    env.setenv("WATTSON_DATA_DIR", str(tmp_path))
    env.setenv("TASK_TTL_MINUTES", "30")
    env.setenv("TRIAGE_TIMEOUT_S", "12.5")
    s = Settings.from_env()
    assert (s.token, s.port, s.data_dir, s.task_ttl_minutes, s.triage_timeout_s) == (
        TOKEN,
        9100,
        tmp_path,
        30,
        12.5,
    )


@pytest.mark.parametrize(
    ("name", "value", "says"),
    [
        ("WATTSON_PORT", "nine", "WATTSON_PORT"),
        ("WATTSON_PORT", "70000", "WATTSON_PORT"),
        ("HISTORY_RETENTION_DAYS", "0", "HISTORY_RETENTION_DAYS"),
        ("TRIAGE_TIMEOUT_S", "-1", "TRIAGE_TIMEOUT_S"),
        ("TRIAGE_URL", "wattson-triage:9001", "TRIAGE_URL"),
        ("KNOWLEDGE_URL", "http://user:pw@host:9002", "KNOWLEDGE_URL"),
        ("KNOWLEDGE_URL", "", "KNOWLEDGE_URL is missing"),
    ],
)
def test_clear_errors(env: pytest.MonkeyPatch, name: str, value: str, says: str) -> None:
    env.setenv(name, value)
    with pytest.raises(SettingsError) as info:
        Settings.from_env()
    assert says in str(info.value)


def test_short_token_refused_without_echo(env: pytest.MonkeyPatch) -> None:
    env.setenv("TRIAGE_TOKEN", "short-secret-value")
    with pytest.raises(SystemExit) as info:
        Settings.from_env()
    assert "TRIAGE_TOKEN" in str(info.value) and "short-secret-value" not in str(info.value)
