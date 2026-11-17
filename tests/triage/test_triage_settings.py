"""Settings from the environment."""

from __future__ import annotations

from pathlib import Path

import pytest

from wattson.agents.triage.settings import DEFAULT_MIN_CONFIDENCE, DEFAULT_MODEL_NAME, Settings

ENV = (
    "TRIAGE_LISTEN_HOST",
    "TRIAGE_PORT",
    "LLAMA_URL",
    "MODEL_NAME",
    "TRIAGE_TIMEOUT_S",
    "MIN_CONFIDENCE",
    "CONFIDENCE_TEMPERATURE",
    "TRIAGE_PUBLIC_URL",
    "TRIAGE_TOKEN_FILE",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRIAGE_TOKEN", "k" * 40)
    return monkeypatch


def test_defaults(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings.from_env()
    assert s.listen_host == "0.0.0.0"  # noqa: S104 - the pod interface
    assert (s.port, s.llama_url, s.timeout_s) == (9001, "http://127.0.0.1:8081", 50.0)
    assert (s.model_name, s.min_confidence, s.public_url) == (
        DEFAULT_MODEL_NAME,
        DEFAULT_MIN_CONFIDENCE,
        None,
    )
    # The llama.cpp sidecar's OpenAI API, the model by its MODEL_NAME, no key.
    assert (s.llm_base_url, s.llm_model, s.llm_api_key) == (
        "http://127.0.0.1:8081/v1",
        DEFAULT_MODEL_NAME,
        "",
    )


def test_llm_base_url_follows_llama_url(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLAMA_URL", "http://127.0.0.1:18081/")
    clean_env.setenv("MODEL_NAME", "qwen2.5-1.5b")
    s = Settings.from_env()
    assert (s.llm_base_url, s.llm_model, s.model_name) == (
        "http://127.0.0.1:18081/v1",
        "qwen2.5-1.5b",
        "qwen2.5-1.5b",
    )


def test_kubeai(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLAMA_URL", "http://127.0.0.1:8081")  # ignored when LLM_BASE_URL is set
    clean_env.setenv("LLM_BASE_URL", "http://kubeai.kubeai.svc/openai/v1/")
    clean_env.setenv("LLM_MODEL", "granite-4-h-1b")
    clean_env.setenv("LLM_API_KEY", "sk-secret-value")
    s = Settings.from_env()
    assert (s.llm_base_url, s.llm_model) == ("http://kubeai.kubeai.svc/openai/v1", "granite-4-h-1b")
    assert s.model_name == DEFAULT_MODEL_NAME  # the name in records stays MODEL_NAME
    assert s.llm_api_key == "sk-secret-value"
    assert "sk-secret-value" not in repr(s) and "k" * 40 not in repr(s)


def test_direct_construction_derives_the_base_url() -> None:
    s = Settings(llama_url="http://127.0.0.1:18081", model_name="m")  # as eval/run.py builds it
    assert (s.llm_base_url, s.llm_model) == ("http://127.0.0.1:18081/v1", "m")


def test_bad_llm_base_url(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLM_BASE_URL", "kubeai/openai/v1")
    with pytest.raises(SystemExit, match="LLM_BASE_URL"):
        Settings.from_env()


def test_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("f" * 40 + "\n")
    monkeypatch.delenv("TRIAGE_TOKEN", raising=False)
    monkeypatch.setenv("TRIAGE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("LLAMA_URL", "http://127.0.0.1:8081/")
    monkeypatch.setenv("MIN_CONFIDENCE", "0.75")
    monkeypatch.setenv("TRIAGE_PORT", "9101")
    monkeypatch.setenv("TRIAGE_PUBLIC_URL", "http://wattson-triage:9001/")
    s = Settings.from_env()
    assert (s.token, s.llama_url, s.min_confidence, s.port) == ("f" * 40, "http://127.0.0.1:8081", 0.75, 9101)
    assert s.public_url == "http://wattson-triage:9001/"


@pytest.mark.parametrize(
    ("name", "value"), [("MIN_CONFIDENCE", "1.5"), ("TRIAGE_PORT", "x"), ("TRIAGE_TOKEN", "short")]
)
def test_bad_values_stop_the_start(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv("TRIAGE_TOKEN", "k" * 40)
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit):
        Settings.from_env()
