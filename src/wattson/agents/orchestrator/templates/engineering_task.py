"""Template: engineering-task. The change, tests passing, a short summary."""

from __future__ import annotations

from wattson.agents.orchestrator.templates.base import TemplateInput, WorkflowTemplate


class EngineeringTask(WorkflowTemplate):
    name = "engineering-task"

    def objective(self, inp: TemplateInput) -> str:
        where = f" in the repository {inp.repo.name}" if inp.repo else ""
        return (
            f"Make the change described in my request{where}, with tests. Read the code it touches "
            "first, and follow the procedures below where they fit."
        )

    def constraints(self, inp: TemplateInput) -> list[str]:
        return [
            "Keep the change small and limited to this task. Do not refactor unrelated code.",
            "Do not disable, skip or weaken tests to make them pass.",
            "Do not commit, push or open a pull request unless I ask.",
        ]

    def missing_checks(self, inp: TemplateInput) -> list[str]:
        if inp.repo is None:
            return ["Which repository should be changed? No Git repository was found in the current folder."]
        return []

    def report_steps(self, inp: TemplateInput) -> list[str]:
        return [
            "What changed: each file, with one line on why.",
            "Tests: what you ran, and the result.",
            "Confirmed findings: facts you checked while working.",
            "Open questions: what you could not confirm, and what you assumed.",
        ]


TEMPLATE = EngineeringTask()
