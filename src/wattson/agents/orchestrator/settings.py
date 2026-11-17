"""The orchestrator's settings, read once from the environment at start.

Errors stop the process with a plain message. Tokens are never part of a message or a repr."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from wattson.shared.auth import read_token


class SettingsError(SystemExit):
    """A setting is missing or wrong. A SystemExit: main() stops with this message."""


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    triage_url: str
    triage_token: str = field(repr=False)
    knowledge_url: str
    knowledge_token: str = field(repr=False)
    listen_host: str = "127.0.0.1"
    port: int = 9000
    data_dir: Path = Path(".data")
    history_retention_days: int = 90
    task_ttl_minutes: int = 120
    triage_timeout_s: float = 60.0
    knowledge_timeout_s: float = 45.0
    # The Guard (guard/): GUARD_MODE enforce | monitor | off. GUARD_MODEL_URL empty: rules only.
    guard_mode: str = "monitor"
    guard_model_url: str = ""
    guard_model: str = ""
    guard_timeout_s: float = 20.0
    guard_rules_file: Path | None = None  # None: the packaged default rules
    guard_policies_file: Path | None = None  # None: the packaged default Cedar policies
    # The prompt-injection model (guard/shield.py): INJECTION_MODE off | warn | cut. Off: the rules only.
    injection_mode: str = "off"
    shield_url: str = ""
    shield_token: str = field(default="", repr=False)
    injection_model: str = ""
    injection_threshold: float = 0.95
    injection_timeout_s: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            # read_token reads os.environ itself and refuses short tokens without echoing them.
            token=read_token("WATTSON_TOKEN", "WATTSON_TOKEN_FILE"),
            triage_url=_url(env, "TRIAGE_URL"),
            triage_token=read_token("TRIAGE_TOKEN", "TRIAGE_TOKEN_FILE"),
            knowledge_url=_url(env, "KNOWLEDGE_URL"),
            knowledge_token=read_token("KNOWLEDGE_TOKEN", "KNOWLEDGE_TOKEN_FILE"),
            listen_host=env.get("WATTSON_LISTEN_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_int(env, "WATTSON_PORT", 9000, low=1, high=65535),
            data_dir=Path(env.get("WATTSON_DATA_DIR", "").strip() or ".data"),
            history_retention_days=_int(env, "HISTORY_RETENTION_DAYS", 90, low=1, high=3650),
            task_ttl_minutes=_int(env, "TASK_TTL_MINUTES", 120, low=1, high=7 * 24 * 60),
            triage_timeout_s=_float(env, "TRIAGE_TIMEOUT_S", 60.0),
            knowledge_timeout_s=_float(env, "KNOWLEDGE_TIMEOUT_S", 45.0),
            **_guard(env),
            **_injection(env),
        )


GUARD_MODES = ("enforce", "monitor", "off")


def _guard(env: Mapping[str, str]) -> dict[str, object]:
    mode = env.get("GUARD_MODE", "").strip().lower() or "monitor"
    if mode not in GUARD_MODES:
        raise SettingsError("GUARD_MODE must be one of: enforce, monitor, off")
    url = env.get("GUARD_MODEL_URL", "").strip()
    model = env.get("GUARD_MODEL", "").strip()
    if url:
        url = _url(env, "GUARD_MODEL_URL")
        if not model:
            raise SettingsError("GUARD_MODEL is missing: the model name the server knows, e.g. granite")
    rules = env.get("GUARD_RULES_FILE", "").strip()
    policies = env.get("GUARD_POLICIES_FILE", "").strip()
    return {
        "guard_mode": mode,
        "guard_model_url": url,
        "guard_model": model,
        "guard_timeout_s": _float(env, "GUARD_TIMEOUT_S", 20.0),
        "guard_rules_file": Path(rules) if rules else None,
        "guard_policies_file": Path(policies) if policies else None,
    }


INJECTION_MODES = ("off", "warn", "cut")


def _injection(env: Mapping[str, str]) -> dict[str, object]:
    mode = env.get("INJECTION_MODE", "").strip().lower() or "off"
    if mode not in INJECTION_MODES:
        raise SettingsError("INJECTION_MODE must be one of: off, warn, cut")
    if mode == "off":
        return {"injection_mode": "off"}
    raw = env.get("INJECTION_THRESHOLD", "").strip() or "0.95"
    try:
        threshold = float(raw)
    except ValueError:
        threshold = -1.0
    if not 0 < threshold <= 1:
        raise SettingsError("INJECTION_THRESHOLD must be a number above 0 and at most 1, like 0.95")
    return {
        "injection_mode": mode,
        "shield_url": _url(env, "SHIELD_URL"),
        # read_token reads os.environ itself and refuses short tokens without echoing them.
        "shield_token": read_token("SHIELD_TOKEN", "SHIELD_TOKEN_FILE"),
        "injection_model": env.get("INJECTION_MODEL", "").strip() or "the injection model",
        "injection_threshold": threshold,
        "injection_timeout_s": _float(env, "INJECTION_TIMEOUT_S", 30.0),
    }


def _url(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SettingsError(f"{name} is missing: the base URL of that agent, e.g. http://wattson-triage:9001")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SettingsError(f"{name} must be an http(s) URL with a host, e.g. http://wattson-triage:9001")
    if parsed.username or parsed.password:
        # A URL with a password would end up in logs; the token goes in its own variable.
        raise SettingsError(f"{name} must not contain a user or password; use the token variable")
    return value.rstrip("/")


def _int(env: Mapping[str, str], name: str, default: int, *, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number between {low} and {high}") from None
    if not low <= value <= high:
        raise SettingsError(f"{name} must be a whole number between {low} and {high}")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a number of seconds between 1 and 3600") from None
    if not 1 <= value <= 3600:
        raise SettingsError(f"{name} must be a number of seconds between 1 and 3600")
    return value
