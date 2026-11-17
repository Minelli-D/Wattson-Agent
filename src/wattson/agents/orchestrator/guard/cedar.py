"""The Cedar authorization intervention: a declarative second lock on the step order.

Strands' CedarAuthorization evaluates the policies (policies.cedar) before every step. The handler
injects context.session.approved so a policy can allow run_on_laptop only after the user's approval
(the same rule the ApprovalGate hook enforces in code). A bad policy file falls back to the
packaged default and the problem is logged and reported in health.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import cedarpy
from strands.vended_interventions.cedar import CedarAuthorization

from wattson.agents.orchestrator import working as w
from wattson.agents.orchestrator.flow import is_sha256

DEFAULT_POLICIES = "policies.cedar"


def default_policies_text() -> str:
    return resources.files(__package__).joinpath(DEFAULT_POLICIES).read_text(encoding="utf-8")


def _validate(text: str) -> None:
    cedarpy.format_policies(text)  # raises ValueError on a malformed policy


def load_policies(path: Path | None) -> tuple[str, str | None]:
    """The policy text of `path` (None: the packaged default). A file that cannot be used gives the
    packaged default and the problem, to log and to show in health."""
    if path is None:
        return default_policies_text(), None
    try:
        text = Path(path).read_text(encoding="utf-8")
        _validate(text)
    except OSError as e:
        problem = f"cannot read {path}: {e.strerror or type(e).__name__}"
    except ValueError as e:
        problem = f"{path} is not a valid Cedar policy: {e}"
    else:
        return text, None
    return default_policies_text(), f"{problem}; using the packaged default policies"


def _approved(fields: dict[str, Any]) -> bool:
    """True only when the user approved this task's exact prompt hash. Never raises (fail-closed:
    on any error run_on_laptop is denied, and the ApprovalGate hook is the second lock)."""
    try:
        agent = fields.get("invocation_state", {}).get("agent")
        if agent is None:
            return False
        task = w.load_task(agent)
        approval = w.get(agent, w.APPROVAL, {})
        approved = approval.get("prompt_sha256")
        return bool(
            approval.get("decision") == "approve"
            and is_sha256(approved)
            and task.get("approved_sha256") == approved
        )
    except Exception:  # noqa: BLE001 - a broken enricher must not throw; deny the run instead
        return False


def build_cedar_from_text(policies: str) -> CedarAuthorization:
    """A fresh CedarAuthorization handler for one Agent (handlers are agent-scoped)."""
    return CedarAuthorization(
        policies=policies,
        context_enricher=lambda fields: {"approved": _approved(fields)},
    )
