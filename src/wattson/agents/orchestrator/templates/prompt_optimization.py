"""Template: prompt-optimization. A rewritten, structured prompt for a coding agent."""

from __future__ import annotations

from wattson.agents.orchestrator.templates.base import TemplateInput, WorkflowTemplate


class PromptOptimization(WorkflowTemplate):
    name = "prompt-optimization"

    def objective(self, inp: TemplateInput) -> str:
        where = f" working in the repository {inp.repo.name}" if inp.repo else ""
        return (
            "Rewrite my request into a clear, structured prompt for a coding agent"
            f"{where}. Use the procedures and facts below to make it precise. Do not do the task itself."
        )

    def constraints(self, inp: TemplateInput) -> list[str]:
        return [
            "Do not edit files or run the task: the result is a prompt, not a change.",
            "Keep every requirement of my request. Mark anything you add as an assumption.",
        ]

    def missing_checks(self, inp: TemplateInput) -> list[str]:
        if inp.repo is None:
            return ["Which repository is the prompt for? No Git repository was found in the current folder."]
        return []

    def report_steps(self, inp: TemplateInput) -> list[str]:
        return [
            "The rewritten prompt, in one fenced block: goal, context, steps, constraints, done criteria.",
            "Confirmed findings: facts from the evidence that the prompt relies on.",
            "Assumptions you made, and open questions for me.",
        ]


TEMPLATE = PromptOptimization()
