"""Point the Claude Code plugin cognee-memory at the Cognee pod (docs/cognee.md, "The Claude Code plugin").

Run on the laptop while `bash k8s/cognee.sh --port-forward` runs:  python3 k8s/cognee/plugin_settings.py

It logs in to Cognee's API server (127.0.0.1:8766, the port-forward) as Cognee's default user, with
the password of ~/.config/wattson/cognee-ui-password, replaces the API key named
"claude-code-plugin" with a new one, and writes the settings block of the plugin into
~/.cognee/.env (mode 600): the pod's URL, that key, raw capture off, no observer, no update check,
no code indexing. Other lines of the file stay. Nothing secret is printed. Standard library only.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

API = "http://127.0.0.1:8766"
KEY_NAME = "claude-code-plugin"
USER = "default_user@example.com"
PASSWORD_FILE = os.path.expanduser("~/.config/wattson/cognee-ui-password")
ENV_FILE = os.path.expanduser("~/.cognee/.env")
BEGIN = "# ---- Wattson: the plugin uses the cluster Cognee (begin) ----"
END = "# ---- Wattson (end) ----"

# The plugin's settings (its README, "Configuration reference"). COGNEE_API_KEY is added below.
SETTINGS = {
    # 127.0.0.1, not another address: with any other host the plugin asks cognee.ai for a
    # "credits" balance with this key.
    "COGNEE_BASE_URL": '"http://127.0.0.1:8766"',
    # An outage says OFFLINE; never a local server downloaded from PyPI on that port.
    "COGNEE_MANAGED_ENDPOINT": "true",
    "COGNEE_PLUGIN_DATASET": '"claude_code_memory"',
    # No raw prompts, tool output or answers stored; recall and explicit remember still work.
    "COGNEE_CAPTURE": "false",
    "COGNEE_AUTO_IMPROVE_EVERY": "0",
    # No LLM calls on the Claude subscription (the observer runs only with a local server anyway).
    "COGNEE_LLM_OBSERVER": "false",
    # No hourly check of GitHub for a new plugin version.
    "COGNEE_UPDATE_CHECK": "off",
    # One API key, Cognee's default user: no agent user, tenant or role made in the pod.
    "COGNEE_PLUGIN_IDENTITY": "false",
    "COGNEE_SHARED_AGENT_MEMORY": "false",
    # Claude Code's own memory (MEMORY.md) stays on.
    "COGNEE_DISABLE_AUTO_MEMORY": "false",
    # 127.0.0.1 counts as "local" for the plugin: it would index the code of every repository
    # opened in Claude Code into the pod, client projects too.
    "COGNEE_CODE_AUTOINDEX": "off",
}

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
headers: dict[str, str] = {}


def call(method: str, path: str, body: dict | None = None, form: bool = False):
    data, h = None, dict(headers)
    if body is not None:
        if form:
            data, h["Content-Type"] = urllib.parse.urlencode(body).encode(), "application/x-www-form-urlencoded"
        else:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=h)
    with opener.open(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw.strip() else None


def new_key() -> str:
    password = open(PASSWORD_FILE).read().strip()
    login = call("POST", "/api/v1/auth/login", {"username": USER, "password": password}, form=True)
    if isinstance(login, dict) and login.get("access_token"):
        headers["Authorization"] = "Bearer " + login["access_token"]
    old = [k for k in call("GET", "/api/v1/auth/api-keys") or [] if k.get("name") == KEY_NAME]
    for k in old:
        call("DELETE", f"/api/v1/auth/api-keys/{k['id']}")
    made = call("POST", "/api/v1/auth/api-keys", {"name": KEY_NAME}) or {}
    key = made.get("key", "")
    if len(key) < 20:
        sys.exit("The API server gave no key.")
    print(f"API key {KEY_NAME}: {len(old)} old one(s) removed, a new one made (id {made.get('id')}).")
    return key


def write_env(key: str) -> None:
    values = {"COGNEE_API_KEY": f'"{key}"', **SETTINGS}
    ours = re.compile(r"^\s*(?:export\s+)?(" + "|".join(values) + r")\s*=")
    lines = open(ENV_FILE).read().splitlines() if os.path.exists(ENV_FILE) else []
    kept, inside = [], False
    for line in lines:
        if line in (BEGIN, END):
            inside = line == BEGIN
            continue
        if not inside and not ours.match(line):
            kept.append(line)
    block = [BEGIN, *[f"{k}={v}" for k, v in values.items()], END]
    os.makedirs(os.path.dirname(ENV_FILE), exist_ok=True)
    fd = os.open(ENV_FILE + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(kept + [""] + block) + "\n")
    os.replace(ENV_FILE + ".new", ENV_FILE)
    os.chmod(ENV_FILE, 0o600)
    print(f"Settings written to {ENV_FILE} (mode 600). Start a new Claude Code session to use them.")


if __name__ == "__main__":
    write_env(new_key())
