"""Where the CLI finds the orchestrator: config.json (written by k8s/deploy.sh) and the token file.

config.json: {"k8s_context": ..., "namespace": ..., "deployment": "wattson-orchestrator",
"remote_port": 9000}. The token file must have mode 600: a token others can read is refused.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from wattson.shared.auth import MIN_TOKEN_LENGTH

DEFAULT_DEPLOYMENT = "wattson-orchestrator"
DEFAULT_REMOTE_PORT = 9000
PROMPT_MODES = ("careful", "quick")
DEFAULT_PROMPT_MODE = "careful"


class ConfigError(Exception):
    """The local configuration is missing or wrong. The message says what to do."""


def config_dir() -> Path:
    return Path(os.environ.get("WATTSON_CONFIG_DIR") or Path.home() / ".config" / "wattson").expanduser()


def state_dir() -> Path:
    return Path(
        os.environ.get("WATTSON_STATE_DIR") or Path.home() / ".local" / "state" / "wattson"
    ).expanduser()


def config_path() -> Path:
    return config_dir() / "config.json"


def token_path() -> Path:
    file = os.environ.get("WATTSON_TOKEN_FILE")
    return Path(file).expanduser() if file else config_dir() / "token"


def agent_url_override() -> str | None:
    """WATTSON_URL skips the port-forward (development)."""
    url = os.environ.get("WATTSON_URL", "").strip()
    return url.rstrip("/") or None


@dataclass
class Config:
    k8s_context: str | None
    namespace: str
    deployment: str = DEFAULT_DEPLOYMENT
    remote_port: int = DEFAULT_REMOTE_PORT
    prompts: str = DEFAULT_PROMPT_MODE  # "careful" (type yes) or "quick" (y or yes)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def load_config(path: Path | None = None) -> Config | None:
    """The config, or None when the file does not exist."""
    path = path or config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a JSON object.")
    namespace = data.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise ConfigError(f"{path} has no namespace. Run k8s/deploy.sh again.")
    context = data.get("k8s_context")
    if context is not None and not isinstance(context, str):
        raise ConfigError(f"{path}: k8s_context must be text.")
    deployment = data.get("deployment") or DEFAULT_DEPLOYMENT
    try:
        port = int(data.get("remote_port") or DEFAULT_REMOTE_PORT)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: remote_port must be a number.") from exc
    prompts = data.get("prompts") or DEFAULT_PROMPT_MODE
    if prompts not in PROMPT_MODES:
        raise ConfigError(f'{path}: prompts must be "careful" or "quick", not {prompts!r}.')
    return Config(
        k8s_context=context or None,
        namespace=namespace,
        deployment=str(deployment),
        remote_port=port,
        prompts=prompts,
    )


def prompt_mode() -> str:
    """The approval mode: careful (type yes, the default) or quick (y or yes).

    A broken config means careful.
    """
    try:
        config = load_config()
    except ConfigError:
        return DEFAULT_PROMPT_MODE
    return config.prompts if config is not None else DEFAULT_PROMPT_MODE


def require_config() -> Config:
    config = load_config()
    if config is None:
        raise ConfigError(
            f"No settings found at {config_path()}. Run k8s/deploy.sh first (it writes this file), "
            "or set WATTSON_URL to reach an agent directly."
        )
    return config


def file_mode(path: Path) -> int | None:
    """The permission bits of a file (0o600), or None when it does not exist."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def token_mode_problem(path: Path) -> str | None:
    """Why the token file is not safe to use, or None when it is fine."""
    mode = file_mode(path)
    if mode is None:
        return f"The token file {path} does not exist. Run k8s/deploy.sh first (it writes this file)."
    if mode & 0o077:
        return (
            f"The token file {path} can be read by other users (mode {mode:o}). "
            f"Nothing was sent. Fix it with: chmod 600 {path}"
        )
    return None


def read_token(path: Path | None = None) -> str:
    """The bearer token. Refuses a missing, short, or group/world-readable file."""
    path = path or token_path()
    problem = token_mode_problem(path)
    if problem:
        raise ConfigError(problem)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"Cannot read the token file {path}: {exc.strerror}") from exc
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"The token in {path} is shorter than {MIN_TOKEN_LENGTH} characters.")
    return token
