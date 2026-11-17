"""`wattson grimoire connect|status|disconnect`.

The OAuth login happens in the browser on this laptop (design 10.7). The CLI serves
http://127.0.0.1:<port>/callback, asks the agent for the login URL, waits for the browser to come
back (at most 5 minutes), and passes only the one-time code and state to the agent. The tokens stay
in the knowledge agent.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from wattson.cli.commands.common import ERROR, OK, STOPPED, UsageError, agent
from wattson.cli.ui import UI
from wattson.shared.contracts import GrimoireStatus

DEFAULT_PORT = 47615
WAIT_S = 300.0

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Wattson Agent</title></head>
<body style="font-family: sans-serif; margin: 3em"><h1>{title}</h1><p>{text}</p></body></html>"""


def add_parsers(sub: Any) -> None:
    g = sub.add_parser("grimoire", help="connect the agent to Grimoire (your knowledge base)")
    gsub = g.add_subparsers(dest="grimoire_command", metavar="COMMAND", required=True)
    p = gsub.add_parser("connect", help="log in to Grimoire in the browser")
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"local port for the login callback ({DEFAULT_PORT})"
    )
    p.add_argument("--no-browser", action="store_true", help="only print the login link")
    p.add_argument("--wait", type=float, default=WAIT_S, help=argparse.SUPPRESS)
    p.set_defaults(func=run_connect)
    p = gsub.add_parser("status", help="is the agent connected to Grimoire?")
    p.set_defaults(func=run_status)
    p = gsub.add_parser("disconnect", help="make the agent forget its Grimoire login")
    p.set_defaults(func=run_disconnect)


class CallbackServer(ThreadingHTTPServer):
    """Serves /callback once: keeps the first code/state (or error) and says "you can close this tab"."""

    daemon_threads = True

    def __init__(self, port: int):
        super().__init__(("127.0.0.1", port), _Handler)
        self.result: dict[str, str] | None = None
        self.done = threading.Event()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/callback"


class _Handler(BaseHTTPRequestHandler):
    server: CallbackServer

    def do_GET(self) -> None:  # noqa: N802 - the http.server API
        url = urlparse(self.path)
        if url.path != "/callback":
            self._page(404, "Not found", "This address only takes the Grimoire login callback.")
            return
        params = {k: v[0] for k, v in parse_qs(url.query).items() if v}
        if self.server.done.is_set():
            self._page(200, "Already done", "Wattson Agent already got the login. You can close this tab.")
            return
        if "error" in params:
            self.server.result = {
                "error": params["error"],
                "error_description": params.get("error_description", ""),
            }
            self._page(
                200,
                "Login not completed",
                "Grimoire did not give access. Look at the terminal. You can close this tab.",
            )
        elif params.get("code") and params.get("state"):
            self.server.result = {"code": params["code"], "state": params["state"], "iss": params.get("iss")}
            self._page(200, "Done", "Wattson Agent got the login. You can close this tab.")
        else:
            self._page(
                400, "Missing data", "The callback has no code or state. Try wattson grimoire connect again."
            )
            return
        self.server.done.set()

    def _page(self, status: int, title: str, text: str) -> None:
        body = PAGE.format(title=title, text=text).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence: the URL holds the code
        return


def show_status(ui: UI, status: GrimoireStatus) -> None:
    ui.kv(
        [
            (
                "Grimoire",
                f"connected to {status.mcp_url}" if status.connected else f"not connected ({status.mcp_url})",
            ),
            ("Read-only tools", ", ".join(status.tools)),
            ("Last error", status.last_error),
        ]
    )


def open_browser(ui: UI, url: str) -> None:
    opener = shutil.which("xdg-open")
    if opener is None:
        ui.info("Open the link above in your browser.")
        return
    try:
        subprocess.Popen(  # noqa: S603 - fixed program, the URL is one argument
            [opener, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        ui.info("Could not open the browser. Open the link above yourself.")


def run_connect(ui: UI, args: argparse.Namespace) -> int:
    if not 0 <= args.port <= 65535:
        raise UsageError("--port must be between 0 and 65535.")
    try:
        server = CallbackServer(args.port)
    except OSError as exc:
        ui.error(f"Cannot listen on 127.0.0.1:{args.port} ({exc.strerror}). Pick another port with --port.")
        return ERROR
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        with agent(ui) as client:
            view = client.grimoire_connect(server.redirect_uri)
            ui.info("Log in to Grimoire with this link (use your enrolled Chrome if it opens elsewhere):")
            ui.line(view.authorize_url, "bold")
            if not args.no_browser:
                open_browser(ui, view.authorize_url)
            ui.dim(f"Waiting for the login (at most {args.wait / 60:.0f} minutes). Ctrl-C stops.")
            try:
                got = server.done.wait(timeout=args.wait)
            except KeyboardInterrupt:
                ui.info("Stopped. Grimoire is not connected.")
                return STOPPED
            if not got or server.result is None:
                ui.error("No login came back in time. Run wattson grimoire connect again.")
                return ERROR
            result = server.result
            if "error" in result:
                detail = f": {result['error_description']}" if result.get("error_description") else ""
                ui.error(f"Grimoire refused the login ({result['error']}{detail}).")
                return ERROR
            status = client.grimoire_callback(result["code"], result["state"], result.get("iss"))
    finally:
        server.shutdown()
        server.server_close()
    show_status(ui, status)
    if status.connected:
        ui.ok("Grimoire is connected.")
        return OK
    ui.error("The agent could not finish the login.")
    return ERROR


def run_status(ui: UI, args: argparse.Namespace) -> int:
    with agent(ui) as client:
        status = client.grimoire_status()
    show_status(ui, status)
    return OK if status.connected else ERROR


def run_disconnect(ui: UI, args: argparse.Namespace) -> int:
    with agent(ui) as client:
        status = client.grimoire_disconnect()
    show_status(ui, status)
    if status.connected:
        ui.error("The agent is still connected.")
        return ERROR
    ui.ok("The agent forgot its Grimoire login.")
    return OK
