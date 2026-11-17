"""The on-demand connection to the orchestrator (design 10.3).

The orchestrator has no Service: the CLI runs
  kubectl --context <ctx> -n <ns> port-forward deploy/<name> 0:<remote_port> --address 127.0.0.1
reads the "Forwarding from 127.0.0.1:NNNNN" line, and stops kubectl when it exits (atexit and
signals). When the forward drops (for example during a long Claude Code run) it is opened again.
WATTSON_URL skips all this (development).
"""

from __future__ import annotations

import atexit
import collections
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable

from wattson.cli.config import Config, agent_url_override, require_config

FORWARD_LINE = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+)")
START_TIMEOUT_S = 15.0

_open_forwards: set[PortForward] = set()
_hooks_installed = False


class PortForwardError(Exception):
    """The port-forward could not be opened. The message says why."""


def kubectl_bin() -> str:
    return os.environ.get("WATTSON_KUBECTL_BIN") or "kubectl"


def parse_forward_line(line: str) -> int | None:
    """The local port from kubectl's "Forwarding from 127.0.0.1:NNNNN -> 9000" line."""
    match = FORWARD_LINE.search(line)
    return int(match.group(1)) if match else None


class PortForward:
    def __init__(
        self, config: Config, timeout_s: float = START_TIMEOUT_S, log: Callable[[str], None] | None = None
    ):
        self.config = config
        self.timeout_s = timeout_s
        self.log = log or (lambda _msg: None)
        self.proc: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._readers: list[threading.Thread] = []

    def command(self) -> list[str]:
        cmd = [kubectl_bin()]
        if self.config.k8s_context:
            cmd += ["--context", self.config.k8s_context]
        cmd += [
            "-n",
            self.config.namespace,
            "port-forward",
            f"deploy/{self.config.deployment}",
            f"0:{self.config.remote_port}",
            "--address",
            "127.0.0.1",
        ]
        return cmd

    @property
    def url(self) -> str:
        if self.port is None:
            raise PortForwardError("the port-forward is not open")
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.port is not None

    def start(self) -> str:
        self.stop()
        _install_exit_hooks()
        self._stderr.clear()
        try:
            # A new session: Ctrl-C in the terminal (for Claude Code) must not kill the forward.
            proc = subprocess.Popen(  # noqa: S603 - fixed argument list, no shell
                self.command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise PortForwardError(
                f"kubectl was not found ({kubectl_bin()}). Install it, or set WATTSON_KUBECTL_BIN."
            ) from exc
        self.proc = proc
        _open_forwards.add(self)
        lines: queue.Queue[str | None] = queue.Queue()
        self._readers = [
            threading.Thread(target=_pump, args=(proc.stdout, lines.put), daemon=True),
            threading.Thread(target=_pump, args=(proc.stderr, self._stderr.append), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stop()
                raise PortForwardError(
                    f"kubectl port-forward did not start within {self.timeout_s:.0f} seconds."
                    + self._stderr_hint()
                )
            try:
                line = lines.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                if proc.poll() is not None:
                    time.sleep(0.1)  # let the stderr reader catch the last lines
                    self.stop()
                    raise PortForwardError(f"kubectl port-forward stopped.{self._stderr_hint()}") from None
                continue
            if line is None:  # stdout closed
                continue
            port = parse_forward_line(line)
            if port:
                self.port = port
                # Keep reading stdout ("Handling connection for ...") so kubectl never blocks.
                threading.Thread(target=_drain, args=(lines,), daemon=True).start()
                return self.url

    def ensure(self) -> str:
        """The URL, opening the forward again when it has dropped."""
        if self.alive():
            return self.url
        if self.proc is not None:
            self.log("The connection to the agent dropped. Opening it again.")
        return self.start()

    def stop(self) -> None:
        proc, self.proc, self.port = self.proc, None, None
        readers, self._readers = self._readers, []
        _open_forwards.discard(self)
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        for reader in readers:  # the pipes end with the process
            reader.join(timeout=1)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    def _stderr_hint(self) -> str:
        text = " ".join(line.strip() for line in list(self._stderr) if line and line.strip())
        return f" kubectl said: {text[-400:]}" if text else ""


def _pump(stream, put: Callable[[str | None], None]) -> None:  # type: ignore[no-untyped-def]
    try:
        for line in stream:
            put(line)
    except (OSError, ValueError):
        pass
    finally:
        put(None)


def _drain(lines: queue.Queue[str | None]) -> None:
    while lines.get() is not None:
        pass


def stop_all() -> None:
    for forward in list(_open_forwards):
        forward.stop()


def _on_signal(signum: int, _frame: object) -> None:
    stop_all()
    raise SystemExit(128 + signum)


def _install_exit_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    atexit.register(stop_all)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            if signal.getsignal(sig) in (signal.SIG_DFL, None):
                signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # not the main thread
            pass


class Endpoint:
    """Where the orchestrator answers: WATTSON_URL, or a port-forward opened on first use."""

    def __init__(self, url: str | None = None, forward: PortForward | None = None):
        if not url and forward is None:
            raise ValueError("an endpoint needs a URL or a port-forward")
        self._url = url.rstrip("/") if url else None
        self.forward = forward

    @classmethod
    def from_env(cls, log: Callable[[str], None] | None = None) -> Endpoint:
        url = agent_url_override()
        if url:
            return cls(url=url)
        return cls(forward=PortForward(require_config(), log=log))

    @property
    def description(self) -> str:
        if self._url:
            return f"{self._url} (from WATTSON_URL)"
        cfg = self.forward.config  # type: ignore[union-attr]
        where = f"{cfg.k8s_context or 'the current context'}/{cfg.namespace}"
        return f"port-forward to deploy/{cfg.deployment} in {where}"

    def base_url(self) -> str:
        if self._url:
            return self._url
        return self.forward.ensure()  # type: ignore[union-attr]

    def reopen(self) -> bool:
        """Open the port-forward again after a connection error. False when there is none."""
        if self.forward is None:
            return False
        self.forward.start()
        return True

    def close(self) -> None:
        if self.forward is not None:
            self.forward.stop()
