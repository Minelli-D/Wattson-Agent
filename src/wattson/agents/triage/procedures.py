"""The pick of one Wattson procedure (Grimoire folder "wattson") for a request.

The small model chooses among the procedure NAMES, constrained by a JSON-schema enum ("none"
included): measured on production Granite 4 1B, 18/20 right, against 3/20 when it chose among
letters. Only the procedures of the triage's workflow are offered (`for_workflow`, D42). The signal
words of the request check its answer:

- score of an option = how many of its tags are words of the request (lower case, light plural
  strip). Only real tags count: the words of its name do not ("pod" fits every Kubernetes request).
- owned tags: the tags of only one offered option.
- the user's procedure (Overrides.procedure) wins: method "user"; "none" means no procedure.
- the model picks P: method "model"; agreed = score(P) >= 1, or no option scores >= 1. But when
  one option has more owned tags in the request than P, and no other option as many, its tags win:
  that option, method "signals", with a warning.
- the model says none: no procedure; but when one option scores >= 2 and clearly best, its name
  stays in the pick (key None, name set, method "none") so the orchestrator can ASK.
- the model failed: the option that scores >= 2 and alone at the top (method "signals"), else none.
  Both with a warning.

Pure functions, no I/O: the triage engine sends the request; the orchestrator uses
`ask_candidates` to decide whether to ask the user; eval/pick.py runs the same code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from wattson.shared.contracts import WORKFLOWS, ProcedureOption, ProcedurePick

NONE = "none"
PICK_SCHEMA_NAME = "pick"
PICK_MAX_TOKENS = 40
# Production Granite 4.2 runs with --reasoning on; turn the think step off per request so the JSON
# pick is not eaten by a think preamble. Unknown key: other servers ignore it.
THINKING_OFF: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}
MAX_OPTIONS = 24  # the enum and the prompt stay small
MAX_WHEN = 300
MAX_REQUEST_CHARS = 4000
MAX_CANDIDATES = 3  # names in the procedure question, before "none"

PICK_SYSTEM_HEAD = "You choose the one procedure that fits an engineering request.\n\nProcedures:\n"
NONE_LINE = f"- {NONE}: none of the above fits"

_WORD = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_PARTS = re.compile(r"[-_.]")


# ---- words and scores ---------------------------------------------------------------------------


def norm(word: str) -> str:
    """Lower case and a light plural strip, the same for the request and the signals. Only words of
    letters lose a plural: "k8s" and "my-cluster" stay as they are."""
    w = word.lower()
    if not w.isalpha():
        return w
    if len(w) > 4 and w.endswith(("sses", "ches", "shes", "xes")):
        return w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def words(text: str) -> set[str]:
    """The words of a text: each token, and each part of a token with "-", "_" or "."."""
    out: set[str] = set()
    for token in _WORD.findall(text.lower()):
        out.add(norm(token))
        if _PARTS.search(token):
            out.update(norm(p) for p in _PARTS.split(token) if p)
    return out


def name_of(key_or_name: str) -> str:
    """ "wattson/k8s-pod-crashloop.md" -> "k8s-pod-crashloop"."""
    last = key_or_name.strip().rstrip("/").rsplit("/", 1)[-1]
    return last[:-3] if last.lower().endswith(".md") else last


def signal_words(option: ProcedureOption) -> set[str]:
    """Its tags only. The words of its name do not count: "pod" or "check" would fit most requests."""
    return {norm(t) for s in option.signals for t in _WORD.findall(s.lower())}


def signal_scores(request: str, options: list[ProcedureOption]) -> dict[str, int]:
    """Option name -> how many of its signal words the request has. In the options' order."""
    seen = words(request)
    return {o.name: len(signal_words(o) & seen) for o in options}


def owned_scores(request: str, options: list[ProcedureOption]) -> dict[str, int]:
    """Option name -> how many of the request's words are tags of this option and of no other."""
    seen = words(request)
    tags = {o.name: signal_words(o) & seen for o in options}
    return {
        name: len(mine - set().union(*(t for other, t in tags.items() if other != name)))
        for name, mine in tags.items()
    }


def ranked(scores: dict[str, int]) -> list[tuple[str, int]]:
    """Best first; a tie keeps the options' order."""
    return sorted(scores.items(), key=lambda kv: -kv[1])


def clear_best(scores: dict[str, int], least: int = 2) -> str | None:
    """The one option that scores >= least and more than every other."""
    order = ranked(scores)
    if not order or order[0][1] < least:
        return None
    if len(order) > 1 and order[1][1] == order[0][1]:
        return None
    return order[0][0]


def for_workflow(options: list[ProcedureOption], workflow: str | None) -> list[ProcedureOption]:
    """The options of one workflow, and those with no workflow tag. All of them when the workflow is
    not known ("unclear", None) or when none of its own is there."""
    if workflow not in WORKFLOWS:
        return options
    kept = [o for o in options if o.workflow in (workflow, None)]
    return kept if any(o.workflow == workflow for o in kept) else options


# ---- the model call -----------------------------------------------------------------------------


def usable(options: list[ProcedureOption]) -> list[ProcedureOption]:
    """One option per name ("none" is not a name), at most MAX_OPTIONS, in the given order."""
    out: list[ProcedureOption] = []
    names: set[str] = set()
    for o in options:
        name = o.name.strip()
        if not name or name.lower() == NONE or name in names:
            continue
        names.add(name)
        out.append(o)
    return out[:MAX_OPTIONS]


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def pick_system(options: list[ProcedureOption]) -> str:
    lines = [f"- {o.name}: {_one_line(o.when_to_use, MAX_WHEN)}" for o in options]
    return PICK_SYSTEM_HEAD + "\n".join([*lines, NONE_LINE])


def pick_response_format(options: list[ProcedureOption]) -> dict[str, Any]:
    """Names, never letters: the server's grammar lets the model write only one of them."""
    schema = {
        "type": "object",
        "properties": {"procedure": {"type": "string", "enum": [*(o.name for o in options), NONE]}},
        "required": ["procedure"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {"name": PICK_SCHEMA_NAME, "strict": True, "schema": schema},
    }


def pick_body(request: str, options: list[ProcedureOption]) -> dict[str, Any]:
    """The chat/completions body without "model"."""
    return {
        "messages": [
            {"role": "system", "content": pick_system(options)},
            {"role": "user", "content": f"Request: {request[:MAX_REQUEST_CHARS]}"},
        ],
        "temperature": 0,
        "max_tokens": PICK_MAX_TOKENS,
        "response_format": pick_response_format(options),
        **THINKING_OFF,
    }


def parse_pick(body: dict[str, Any], options: list[ProcedureOption]) -> str:
    """The name in choices[0].message.content ({"procedure": name}), or "none". ValueError else."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("the pick answer has no message") from None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("the pick answer is empty")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip().removeprefix("json").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("the pick answer is not valid JSON") from None
    value = data.get("procedure") if isinstance(data, dict) else None
    if not isinstance(value, str):
        raise ValueError("the pick answer has no procedure")
    wanted = value.strip().lower()
    if wanted == NONE:
        return NONE
    for o in options:
        if o.name.lower() == wanted:
            return o.name
    raise ValueError("the pick answer is not one of the procedure names")


# ---- the decision -------------------------------------------------------------------------------


@dataclass
class Decision:
    pick: ProcedurePick
    warnings: list[str] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)


def find(options: list[ProcedureOption], value: str) -> ProcedureOption | None:
    """The option with this name or key (".md" or not, any case)."""
    raw = value.strip().lower()
    wanted = name_of(raw)
    for o in options:
        if o.key.lower() == raw or o.name.lower() == wanted:
            return o
    return None


def resolve_override(
    value: str | None, options: list[ProcedureOption]
) -> tuple[ProcedurePick | None, str | None]:
    """The user's procedure: (pick, None), or (None, warning) when it is not one of the options."""
    if not value or not value.strip():
        return None, None
    if value.strip().lower() == NONE:
        return ProcedurePick(method="user"), None
    option = find(options, value)
    if option is None:
        return None, f"procedure {value.strip()[:80]!r} is not one of the Wattson procedures; ignored"
    return ProcedurePick(key=option.key, name=option.name, method="user"), None


def decide(
    request: str,
    options: list[ProcedureOption],
    model_answer: str | None,
    model_error: str | None = None,
    latency_ms: int = 0,
) -> Decision:
    """model_answer: a name, "none", or None when the model failed (model_error says why)."""
    scores = signal_scores(request, options)
    by_name = {o.name: o for o in options}
    best = clear_best(scores)
    if model_answer is not None and model_answer != NONE and model_answer in by_name:
        owned = owned_scores(request, options)
        owner = clear_best(owned, least=1)
        if owner is not None and owned[owner] > owned[model_answer]:
            option = by_name[owner]
            pick = ProcedurePick(key=option.key, name=owner, method="signals", latency_ms=latency_ms)
            note = f"procedure {owner} chosen by its own tags in the request; the model chose {model_answer}"
            return Decision(pick, [note], scores)
        option = by_name[model_answer]
        agreed = scores[option.name] >= 1 or max(scores.values(), default=0) < 1
        pick = ProcedurePick(
            key=option.key, name=option.name, method="model", agreed=agreed, latency_ms=latency_ms
        )
        return Decision(pick, [], scores)
    if model_answer == NONE:
        # No procedure; a clear signal stays as the name, so the orchestrator can ask.
        return Decision(ProcedurePick(name=best, method="none", latency_ms=latency_ms), [], scores)
    why = model_error or "no usable answer"
    if best is not None:
        option = by_name[best]
        pick = ProcedurePick(key=option.key, name=option.name, method="signals", latency_ms=latency_ms)
        return Decision(pick, [f"procedure model unavailable ({why}); {best} chosen by signal words"], scores)
    warning = f"procedure model unavailable ({why}); no procedure chosen"
    return Decision(ProcedurePick(method="none", latency_ms=latency_ms), [warning], scores)


def ask_candidates(pick: ProcedurePick | None, request: str, options: list[ProcedureOption]) -> list[str]:
    """The names to ask the user about (before "none"), or [] when there is nothing to ask:
    - the model said none, but the signals point to one (key None, name set);
    - the model's pick and the signals disagree (agreed False) and another option scores >= 2."""
    if pick is None or pick.method not in ("model", "none"):
        return []
    names = {o.name for o in options}
    scores = signal_scores(request, options)
    if pick.key is None:
        if pick.method != "none" or not pick.name or pick.name not in names:
            return []
        others = [n for n, s in ranked(scores) if s >= 2 and n != pick.name]
        return [pick.name, *others][:MAX_CANDIDATES]
    if pick.agreed or pick.name not in names:
        return []
    others = [n for n, s in ranked(scores) if s >= 2 and n != pick.name]
    return [pick.name, *others][:MAX_CANDIDATES] if others else []
