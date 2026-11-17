"""Template: devops-investigation. Root cause from evidence, a fix as a plan, nothing applied."""

from __future__ import annotations

from wattson.agents.orchestrator.templates.base import TemplateInput, WorkflowTemplate

# The Claude Code skill in claude-skills/k8s-investigation (D42): read-only habits and how to report.
SKILL_LINE = "If the Claude Code skill k8s-investigation is installed, use it."


class DevopsInvestigation(WorkflowTemplate):
    name = "devops-investigation"

    def objective(self, inp: TemplateInput) -> str:
        t = inp.target
        what = f"the workload {t.workload}" if t.workload else "the failing workload"
        where = (
            f" in cluster {t.cluster}, {t.namespace_words()}"
            if t.cluster and t.namespace_words()
            else " (the cluster and namespace are not known yet: see Missing information)"
        )
        return (
            f"Find the root cause of the problem in my request, for {what}{where}. Back every finding "
            "with the evidence below or with read-only commands you run. Then propose a fix as a plan. "
            "Do not apply it."
        )

    def constraints(self, inp: TemplateInput) -> list[str]:
        t = inp.target
        if t.cluster and t.all_namespaces:
            scope = (
                f"Stay in cluster {t.cluster}; every namespace may be read. Ask me before you look "
                "anywhere else."
            )
        elif t.cluster and t.namespace_words():
            scope = f"Stay in cluster {t.cluster} and {t.namespace_words()}. "
            scope += "Ask me before you look anywhere else."
        else:
            scope = "Ask me which cluster and namespace to use before you run any kubectl command."
        return [
            "Investigate with read-only commands only: kubectl get, describe, logs, events, rollout "
            "status; git log, show, diff.",
            scope,
            "Do not read Kubernetes Secrets or print their values.",
            "Write the fix as a plan with a verify command for each step. Do not apply it.",
            SKILL_LINE,
        ]

    def missing_checks(self, inp: TemplateInput) -> list[str]:
        t = inp.target
        out: list[str] = []
        if not t.cluster:
            out.append("Which cluster (kube context) is affected? No Kubernetes evidence was collected.")
        if not t.namespace_words():
            out.append("Which namespace is affected?")
        if not t.workload:
            out.append("Which workload is failing?")
        if t.cluster and t.namespace_words() and not inp.evidence:
            out.append("No evidence was collected: it was declined or could not be run.")
        return out

    def report_steps(self, inp: TemplateInput) -> list[str]:
        return [
            "Confirmed findings: each one with the evidence that proves it (the command and the lines).",
            "The root cause, or the most likely causes in order if it is not certain.",
            "The fix as a plan: numbered steps, each with a command that verifies it. Nothing applied.",
            "Open questions: what you could not confirm, and what would confirm it.",
        ]

    def show_kubernetes(self, inp: TemplateInput) -> bool:
        return True


TEMPLATE = DevopsInvestigation()
