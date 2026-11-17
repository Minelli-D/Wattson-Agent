"""What every command shares: exit codes, the connection to the agent, cleaning a Claude result."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from wattson.cli.a2a_client import AgentClient
from wattson.cli.config import read_token
from wattson.cli.portforward import Endpoint
from wattson.cli.ui import UI
from wattson.shared.contracts import RunResponse

OK = 0
ERROR = 1
USAGE = 2
STOPPED = 3  # rejected or cancelled by the user, or stopped by the Guard

SUMMARY_MAX = 2000
ERROR_MAX = 500


class UsageError(Exception):
    """A wrong argument: exit code 2."""


@contextmanager
def agent(ui: UI) -> Iterator[AgentClient]:
    """A client of the orchestrator. The port-forward (if any) is closed at the end."""
    token = read_token()
    endpoint = Endpoint.from_env(log=ui.dim)
    try:
        yield AgentClient(endpoint, token)
    finally:
        endpoint.close()


def redact_text(text: str) -> str:
    from wattson.shared.redact import redact

    return redact(text).text


def clean_result(result: RunResponse) -> RunResponse:
    """Secrets out, sizes in the contract's limits: a Claude answer leaves the laptop only like this."""
    summary = redact_text(result.summary)[:SUMMARY_MAX] if result.summary else result.summary
    error = redact_text(result.error)[:ERROR_MAX] if result.error else result.error
    return RunResponse.model_validate({**result.model_dump(), "summary": summary, "error": error})
