"""Deterministic prompt templates (FR-PROMPT-01..07), one per workflow.

`render_prompt(inp)` = redact every input -> `build_sections` -> `render_sections` -> fit the
budget -> redact the final text. A later LLM optimizer can take the sections from
`build_sections`, improve them, and give them to `render_sections` (FR-PROMPT-07).
A new workflow: an entry in `workflows.WORKFLOW_TABLE` and a module here added to `TEMPLATES`.
"""

from __future__ import annotations

from wattson.agents.orchestrator.templates import devops_investigation, engineering_task, prompt_optimization
from wattson.agents.orchestrator.templates.base import (
    BUDGET_STEPS,
    MAX_DOC,
    MAX_EVIDENCE,
    MAX_PROCEDURE,
    MAX_PROMPT,
    TEMPLATE_VERSION,
    Rendered,
    Section,
    TemplateInput,
    WorkflowTemplate,
    clean_input,
    constraints_for,
    hard_cut,
    missing_for,
    render_sections,
    sections_for,
)
from wattson.agents.orchestrator.workflows import WORKFLOW_TABLE
from wattson.shared.redact import redact

__all__ = [
    "MAX_DOC",
    "MAX_EVIDENCE",
    "MAX_PROMPT",
    "TEMPLATES",
    "TEMPLATE_VERSION",
    "Rendered",
    "Section",
    "TemplateInput",
    "WorkflowTemplate",
    "build_sections",
    "render_prompt",
    "render_sections",
]

TEMPLATES: dict[str, WorkflowTemplate] = {
    t.name: t
    for t in (devops_investigation.TEMPLATE, prompt_optimization.TEMPLATE, engineering_task.TEMPLATE)
}


def template_for(workflow: str) -> WorkflowTemplate:
    spec = WORKFLOW_TABLE.get(workflow)
    if spec is None or spec.template not in TEMPLATES:
        raise ValueError(f"no template for workflow {workflow!r}")
    return TEMPLATES[spec.template]


def build_sections(inp: TemplateInput) -> list[Section]:
    """The prompt's sections at the full budget, every input already redacted."""
    clean, _ = clean_input(inp)
    return sections_for(template_for(inp.workflow), clean, MAX_DOC, MAX_EVIDENCE, MAX_PROCEDURE)


def render_prompt(inp: TemplateInput) -> Rendered:
    """The prompt for Claude Code. Same input, byte-identical output."""
    template = template_for(inp.workflow)
    clean, removed = clean_input(inp)
    prompt = ""
    for proc_cap, doc_cap, ev_cap in BUDGET_STEPS:
        prompt = render_sections(sections_for(template, clean, doc_cap, ev_cap, proc_cap))
        if len(prompt) <= MAX_PROMPT:
            break
    final = redact(hard_cut(prompt))  # last line of defence; finds nothing when the inputs were clean
    return Rendered(
        prompt=hard_cut(final.text),
        template=WORKFLOW_TABLE[inp.workflow].template,
        template_version=TEMPLATE_VERSION,
        constraints=constraints_for(template, clean),
        missing_info=missing_for(template, clean),
        redactions=removed + final.count,
    )
