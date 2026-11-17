"""The smoke test of Cognee on the cluster (k8s/cognee.sh --smoke runs it).

    python3 -I k8s/cognee/smoke.py MCP_URL API_URL UI_URL PASSWORD_FILE
    python3 -I k8s/cognee/smoke.py http://127.0.0.1:8765/mcp http://127.0.0.1:8766 \
        http://127.0.0.1:8767 ~/.config/wattson/cognee-ui-password

First the web part: the API server answers (/health), the web UI answers (its first page, and the
API address it gives the browser is API_URL), the API server lets the UI's pages call it (CORS),
and the login of the UI works (the user default_user@example.com, the password in PASSWORD_FILE,
which is never printed).

Then the memory, through cognee-mcp (in API mode: it passes each call to the API server). Made-up
data only, in its own dataset "smoke-test": it forgets that dataset first (a left-over of an
earlier run), remembers three invented notes and one invented Python file, checks that the API
server has the dataset (so the MCP container wrote through it, as the user of the UI), recalls
one note by its meaning (different words), asks the code graph about the file, and forgets the
dataset again at the end, also after a failure. It prints each step with its time, and PASS or
FAIL. Exit 0 when every step passed.

The standard library only (an MCP client over streamable HTTP, JSON-RPC 2.0): no package to
install. It talks only to the URLs it is given.
"""

import base64
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DATASET = "smoke-test"
# One call may load a model for the first time (the API server loads them at the first remember): wait long.
CALL_TIMEOUT_S = 900
PROTOCOL = "2025-06-18"
# Cognee's default user: the owner of the memory, and the login of the web UI.
UI_USER = "default_user@example.com"

NOTES = [
    "Lesson (invented for a smoke test): the batch job nightly-tulip-export fails when its "
    "scratch volume is more than 90 percent full. Empty the scratch folder first; only then "
    "make the volume bigger.",
    "Decision (invented for a smoke test): the team Blue Heron uses Redis only as a cache, never "
    "as the source of truth. Every value must be rebuildable from PostgreSQL.",
    "Lesson (invented for a smoke test): when parrot-gateway answers 502 right after a deploy, "
    "wait for the readiness probe; the old pods need 45 seconds to drain.",
]
# Other words than the note: it must be found by meaning.
RECALL_QUERY = "Why does the night export job break, and what should be done before resizing storage?"
RECALL_EXPECT = "nightly-tulip-export"

CODE_FILE = "smoke_ledger.py"
CODE = '''"""An invented module for the cognee smoke test."""


def add_entry(ledger, amount):
    """Add an amount to the ledger and return the new total."""
    ledger.append(amount)
    return compute_total(ledger)


def compute_total(ledger):
    """Sum every entry of the ledger."""
    return sum(ledger)
'''
CODE_QUERY = "total of the ledger"
CODE_EXPECT = "compute_total"


class Mcp:
    def __init__(self, url):
        self.url = url
        self.session = None
        self.next_id = 0

    def _post(self, message, timeout):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
            headers["MCP-Protocol-Version"] = PROTOCOL
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            session = answer.headers.get("Mcp-Session-Id")
            if session:
                self.session = session
            body = answer.read().decode("utf-8", "replace")
            kind = answer.headers.get("Content-Type", "")
        if "id" not in message:
            return None
        if "text/event-stream" in kind:
            # The answer is one or more SSE events; the one with our id is the result.
            for line in body.splitlines():
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip() or "{}")
                    if data.get("id") == message["id"]:
                        return data
            raise RuntimeError("no answer with our id in the event stream")
        return json.loads(body)

    def call(self, method, params=None, timeout=60):
        self.next_id += 1
        answer = self._post(
            {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}, timeout
        )
        if "error" in answer:
            raise RuntimeError(f"{method}: {answer['error']}")
        return answer["result"]

    def notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method}, 30)

    def tool(self, name, arguments):
        result = self.call("tools/call", {"name": name, "arguments": arguments}, CALL_TIMEOUT_S)
        text = "\n".join(part.get("text", "") for part in result.get("content", []) if isinstance(part, dict))
        # cognee-mcp reports its own failures as text that starts with "Error".
        failed = bool(result.get("isError")) or text.lstrip().startswith("Error")
        return failed, text

    def close(self):
        if not self.session:
            return
        request = urllib.request.Request(
            self.url, headers={"Mcp-Session-Id": self.session}, method="DELETE"
        )
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except (urllib.error.URLError, OSError):
            pass


class Web:
    """Plain HTTP to the API server and the UI, with the login cookie of the API server."""

    def __init__(self, api_url, ui_url):
        self.api_url = api_url.rstrip("/")
        self.ui_url = ui_url.rstrip("/")
        self.cookie = None

    def get(self, url, headers=None, method="GET", data=None, timeout=60):
        """Status, headers and body; an HTTP error status is an answer too."""
        headers = dict(headers or {})
        if self.cookie and url.startswith(self.api_url):
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                return answer.status, answer.headers, answer.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read().decode("utf-8", "replace")

    def login(self, password):
        form = urllib.parse.urlencode({"username": UI_USER, "password": password}).encode()
        status, headers, _ = self.get(
            self.api_url + "/api/v1/auth/login",
            {"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
            data=form,
        )
        # Only the name=value pair of the cookie is kept (never printed: it is a login).
        cookie = (headers.get("Set-Cookie") or "").split(";", 1)[0].strip()
        if status == 200 and "=" in cookie:
            self.cookie = cookie
        return status, cookie.split("=", 1)[0] if cookie else ""

    def dataset_names(self):
        status, _, body = self.get(self.api_url + "/api/v1/datasets")
        if status != 200:
            raise RuntimeError(f"GET /api/v1/datasets: HTTP {status}")
        return sorted(item.get("name", "") for item in json.loads(body))


def short(text, size=240):
    text = " ".join(text.split())
    return text if len(text) <= size else text[:size] + " ..."


def main():
    args = sys.argv[1:]
    if len(args) != 4:
        print("usage: smoke.py MCP_URL API_URL UI_URL PASSWORD_FILE (k8s/cognee.sh --smoke gives them)")
        return 2
    url, api_url, ui_url, password_file = args
    mcp = Mcp(url)
    web = Web(api_url, ui_url)
    results = []

    def step(name, run):
        started = time.monotonic()
        try:
            ok, detail = run()
        except Exception as error:  # a failed step is reported, the test goes on
            ok, detail = False, f"{type(error).__name__}: {error}"
        seconds = time.monotonic() - started
        results.append((name, ok, seconds))
        print(f"{'PASS' if ok else 'FAIL'}  {seconds:7.1f} s  {name}")
        if detail:
            print(f"                 {short(detail)}")
        sys.stdout.flush()
        return ok

    def api_health():
        status, _, body = web.get(web.api_url + "/health")
        return status == 200, f"HTTP {status}: {body}"

    def ui_page():
        # The first page of the UI (it asks for the login in the browser, not here).
        status, _, body = web.get(web.ui_url + "/")
        return status == 200 and "<html" in body.lower(), f"HTTP {status}, {len(body)} bytes"

    def ui_backend():
        status, _, body = web.get(web.ui_url + "/api/runtime-config")
        backend = json.loads(body).get("backendUrl") if status == 200 else None
        return (backend or "").rstrip("/") == web.api_url, f"HTTP {status}, the browser calls {backend}"

    def api_cors():
        # What the browser asks before a call of the UI's pages to the API server.
        status, headers, _ = web.get(
            web.api_url + "/api/v1/datasets",
            {"Origin": web.ui_url, "Access-Control-Request-Method": "GET"},
            method="OPTIONS",
        )
        allowed = headers.get("Access-Control-Allow-Origin")
        credentials = headers.get("Access-Control-Allow-Credentials")
        ok = status == 200 and allowed == web.ui_url and credentials == "true"
        return ok, f"HTTP {status}, allowed origin {allowed}, credentials {credentials}"

    def ui_login():
        try:
            password = pathlib.Path(password_file).expanduser().read_text().strip()
        except OSError as error:
            return False, f"the password file {password_file}: {error.strerror}"
        status, cookie_name = web.login(password)
        if not web.cookie:
            return False, f"HTTP {status}: the login of {UI_USER} failed (password of {password_file})"
        me_status, _, me = web.get(web.api_url + "/api/v1/auth/me")
        email = json.loads(me).get("email") if me_status == 200 else None
        return email == UI_USER, f"logged in as {email} (cookie {cookie_name})"

    def dataset_in_api():
        if not web.cookie:
            return False, "no login (see the login step above)"
        names = web.dataset_names()
        return DATASET in names, f"datasets of {UI_USER}: {', '.join(names) or 'none'}"

    def initialize():
        info = mcp.call(
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "wattson-smoke", "version": "1"}},
        )
        mcp.notify("notifications/initialized")
        server = info.get("serverInfo", {})
        return True, f"server {server.get('name')} {server.get('version')}, protocol {info.get('protocolVersion')}"

    def list_tools():
        names = sorted(tool["name"] for tool in mcp.call("tools/list").get("tools", []))
        missing = {"remember", "recall", "code_search", "forget"} - set(names)
        return not missing, f"tools: {', '.join(names)}" + (f"; missing: {sorted(missing)}" if missing else "")

    def forget(must_work):
        def run():
            failed, text = mcp.tool("forget", {"dataset": DATASET})
            # Before the first run the dataset does not exist: not a failure there.
            return (not failed) or not must_work, text
        return run

    def remember(note):
        def run():
            failed, text = mcp.tool("remember", {"data": note, "dataset_name": DATASET})
            return not failed, text
        return run

    def remember_code():
        content = base64.b64encode(CODE.encode()).decode()
        failed, text = mcp.tool(
            "remember", {"filename": CODE_FILE, "content_base64": content, "dataset_name": DATASET}
        )
        return not failed, text

    def recall():
        failed, text = mcp.tool("recall", {"query": RECALL_QUERY, "datasets": DATASET, "top_k": 3})
        return (not failed and RECALL_EXPECT in text), text

    def code_search():
        failed, text = mcp.tool(
            "code_search", {"operation": "search", "query": CODE_QUERY, "datasets": DATASET, "top_k": 5}
        )
        return (not failed and CODE_EXPECT in text), text

    print(f"Cognee smoke test: MCP {url}, API {web.api_url}, UI {web.ui_url}; dataset {DATASET} (made-up data only)")
    step("the API server answers (/health)", api_health)
    step("the web UI answers (its first page)", ui_page)
    step(f"the web UI sends the browser to {web.api_url}", ui_backend)
    step("the API server lets the UI's pages call it (CORS)", api_cors)
    step(f"the login of the UI ({UI_USER})", ui_login)
    if not step("connect to cognee-mcp (initialize)", initialize):
        print("FAIL: the MCP server does not answer. Nothing was written.")
        return 1
    try:
        step("the tools are there", list_tools)
        step("forget a left-over smoke-test dataset", forget(must_work=False))
        for number, note in enumerate(NOTES, 1):
            step(f"remember note {number} of {len(NOTES)}", remember(note))
        step(f"the API server has the dataset {DATASET} (MCP wrote through it)", dataset_in_api)
        step(f"recall by meaning (expects {RECALL_EXPECT})", recall)
        step(f"remember a made-up code file ({CODE_FILE})", remember_code)
        step(f"code_search (expects {CODE_EXPECT})", code_search)
    finally:
        step("forget the smoke-test dataset", forget(must_work=True))
        mcp.close()

    failed = [name for name, ok, _ in results if not ok]
    notes = [seconds for name, ok, seconds in results if name.startswith("remember note") and ok]
    if notes:
        print(f"remember: {sum(notes) / len(notes):.1f} s a note on average (the first one loads the models)")
    print("PASS: every step worked." if not failed else f"FAIL: {len(failed)} step(s) failed: {'; '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
