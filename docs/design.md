# Wattson Agent: design (v0.1, Strands + A2A)

The requirements are in [`requirements.md`](requirements.md). This file answers its section 10
and fixes how the parts talk. The contracts are code: `src/wattson/shared/contracts.py`.

## The agents (A2A pattern)

```
laptop                                     Kubernetes (my-cluster, namespace grimoire)
------                                     -----------------------------------------
wattson CLI (Python, an A2A client)        orchestrator  (Strands Agent, A2A server, 127.0.0.1:9000)
  ├─ kubectl port-forward ──on demand──►     │  tools = workflow steps, hooks = approval gate,
  │   then A2A JSON-RPC + bearer token       │  audit, redaction; history in SQLite
  │                                          ├──A2A──► triage     (Strands Agent + llama.cpp sidecar,
  ├─ answers the interrupts:                 │                     small CPU model; Service :9001)
  │   clarify, collect_evidence,             └──A2A──► knowledge  (Strands Agent + Grimoire MCP client,
  │   approve, run_claude_code                                     OAuth, read-only tools; Service :9002)
  ├─ connector: git, kubectl (read-only kubeconfig), claude
```

- Every agent is a **Strands `Agent`** served by Strands' **`A2AServer`** (Agent Card at
  `/.well-known/agent-card.json`, JSON-RPC `message/send`, task states). Agents call each other
  with Strands' **`A2AAgent`** client, a bearer token in its httpx client.
- **The planner is deterministic.** `wattson.shared.planner.PlannerModel` is a Strands model
  provider that runs no LLM: it calls the entry tool, then the tool named by each result's
  `next`. So the order of steps is fixed in code (FR-PROMPT-01, FR-DEC-06), and an LLM planner
  can replace it later without touching tools or hooks (FR-PROMPT-07). Proven by a spike:
  planner → tool → interrupt → A2A `input-required` → resume → `completed`.
- **Human steps are Strands interrupts.** A tool calls `tool_context.interrupt(NAME, reason=...)`;
  `A2AServer` turns it into `input-required` with a DataPart `{"interrupts": [{interruptId, name,
  reason}]}`; the CLI answers with a DataPart `{"interruptResponse": {"interruptId", "response"}}`
  in the same task and context. One task = one A2A task, start to finish.
- **The small model only advises.** The triage agent's tool calls llama.cpp (classification by
  log-probs, hints by JSON-schema output). It never grants anything.
- **Safety is in hooks**, not in the planner: a `BeforeToolCallEvent` hook cancels
  `run_on_laptop` unless the human's approval for that exact prompt hash is in the agent state;
  an `AfterToolCallEvent` hook writes the audit events; redaction runs on every result that
  enters the conversation.

## The orchestrator's steps (tools)

| Tool | Does | Next |
|---|---|---|
| `start_task` (entry) | parse `StartTask` from the DataPart `wattson.start_task`, make the task id, record | `guard` |
| `guard` | the Guard (10.9): the request's verdict, from an intervention before this step; enforce: `blocked` → `finish`, risky → interrupt `wattson.guard_confirm` | `list_procedures` or `finish` |
| `list_procedures` | A2A → knowledge agent (`ProcedureListRequest` → `ProcedureList`): the Wattson procedures of Grimoire folder `wattson` and its subfolders; failure = warning, no options | `triage` |
| `triage` | A2A → triage agent (`TriageRequest` with the procedure options → `TriageResult`): the classification, the hints, and the procedure Granite picks by name (checked against its signal words); `overrides.workflow` / `overrides.procedure` win; drop a cluster not in the workspace | `clarify` |
| `clarify` | required questions (workflow if unclear; cluster + namespace for devops; "which procedure?" when the pick is unsure or disagrees with the request's words). Interrupt `wattson.clarify` while any is open | `find_procedures` |
| `find_procedures` | A2A → knowledge agent (`KnowledgeRequest`): with a chosen procedure, that procedure (why="procedure") and the runbooks it lists; without one, the free search with the relevance check (never a `wattson/` document); failure = warning, never invented | `plan_evidence` |
| `plan_evidence` | the minimum `EvidenceRequest`s for the workflow | `collect_evidence` or `compose` |
| `collect_evidence` | interrupt `wattson.collect_evidence`; the CLI shows the plan, runs it, redacts, answers. Several namespaces or all of them with a named workload: once more, for the describe, rollout and logs where the first round's listing shows the workload (D43) | `compose`, or `collect_evidence` once more |
| `compose` | redact again, render the deterministic template → `Proposal` | `request_approval` |
| `request_approval` | interrupt `wattson.approve`; "answer" → `compose` again; "reject" → `finish` | `run_on_laptop` or `finish` |
| `run_on_laptop` | gated by the approval hook; interrupt `wattson.run_claude_code`; the CLI runs Claude Code and answers `RunResponse` | `finish` |
| `finish` | record the end state; the A2A artifact is a `TaskOutcome` | — |

The bulky data (documents, evidence, proposal) stays in `agent.state` of that task's agent; tool
results that enter the conversation are small summaries. One Strands agent per A2A context
(`agent_factory`), kept in memory only; the working state is never written to disk.

## Decisions for section 10

### 10.1 Kubernetes access: through the CLI, with a read-only kubeconfig per cluster
- The agents get **no** Kubernetes credentials (no service-account token mounted). The
  orchestrator only *asks* for evidence (`EvidenceRequest`); the CLI's connector runs it.
- `wattson k8s add <context>` makes, after a yes, a service account `wattson-reader` in an
  existing namespace (on the hosting cluster the agents' own, `grimoire`; elsewhere `--namespace`;
  Wattson Agent never creates a namespace) bound to the built-in `view` ClusterRole (read-only, cannot read Secrets) plus get/list/watch on nodes
  (which `view` does not cover; needed for scheduling problems), and writes its kubeconfig to
  `~/.config/wattson/kube/<context>.yaml` (mode 600). Same for the hosting cluster and any other.
- On top of RBAC the connector has its own allow-list (the ops in `EvidenceOpName`, the kinds in
  `K8S_KINDS`, never `secrets`, never configmap contents, output limits, `--request-timeout`, no
  shell). A context without a reader kubeconfig is refused unless `--use-my-kubeconfig`.

### 10.2 Decision model: llama.cpp server on CPU, a ~1.5B instruct model in Q4
- The official llama.cpp server image as a sidecar of the triage agent, on `127.0.0.1:8081`.
- Chosen by measurement (`eval/`, `docs/decision-model.md`); first candidate
  Qwen2.5-1.5B-Instruct Q4_K_M; chosen: IBM Granite-4.0-H-1B Q4_K_M (`docs/decision-model.md`).
  Downloaded once by an init container, SHA-256 checked.
- Classification: one constrained choice; confidence = softmax of the log-probs (calibratable,
  FR-DEC-05). Hints: Strands `LlamaCppModel` structured output (JSON schema). Keyword rules
  when the model is down, slow, or under `min_confidence`; `method` says which decided.

### 10.3 CLI ↔ orchestrator: A2A over `kubectl port-forward`, plus a bearer token
- The orchestrator has no Service and no Ingress; it listens on `127.0.0.1:9000` in its pod and
  its NetworkPolicy allows no ingress. The CLI starts `kubectl port-forward deploy/wattson-orchestrator`
  when it starts and stops it when it exits. Every request carries `Authorization: Bearer`.
- Triage and knowledge have ClusterIP Services; their NetworkPolicies let only the orchestrator
  in. Each has its own token. `WATTSON_URL=http://127.0.0.1:9000` skips the port-forward in dev.

### 10.4 Claude Code sessions
- Discovery: `~/.claude/projects/<repo path, every non-alphanumeric char as "-">/*.jsonl`, newest
  first. `--session <id>`, `--new-session`, or the CLI asks.
- Interactive (default): `claude --resume <id> "<prompt>"` (new: `claude --session-id <uuid>`),
  attached to the terminal: Claude Code asks its own permission questions (FR-CC-05). After exit
  the CLI reads the session file for the reply. Headless (`--headless`): `claude -p --resume <id>
  --output-format json`, prompt on stdin, normal permission mode. Never a bypass flag.
- A session written in the last 2 minutes may be open elsewhere: resuming does not type into that
  window, it continues the conversation in a new process. The CLI warns and offers `--fork`.

### 10.5 Operational storage
- SQLite on a 1 Gi volume of the orchestrator: `tasks` (`TaskRecord`), `task_events`. Only
  metadata: hashes of the proposed and approved prompt, Grimoire keys, short redacted summaries.
  No prompt, document, evidence or Claude answer is written. Retention 90 days, swept daily.
- The knowledge agent keeps the Grimoire OAuth client and tokens on its own 100 Mi volume.

### 10.6 Execution lifecycle
- Status: `triage → awaiting-input → collecting-evidence → proposed → approved | rejected |
  expired → submitted → completed | failed | interrupted`. The Guard can end a task right after
  `triage` with `blocked` (10.9): nothing ran.
- The CLI keeps the approved prompt and session id in `~/.local/state/wattson/tasks/<id>.json`
  (mode 600) until the result is reported. If the A2A task is gone (pod restart, TTL), the CLI
  reports with `POST /v1/tasks/{id}/execution`; `wattson recover <id>` does it later.
- An unanswered task expires after 120 minutes. Timeouts: triage 60 s, each Grimoire call 15 s,
  each git/kubectl command 20 s, a headless Claude run 30 minutes. A Claude run is never retried.

### 10.7 Security configuration
- Pods: non-root, read-only root filesystem, no capabilities, `RuntimeDefault` seccomp, no
  service-account token, limits. NetworkPolicies: orchestrator no ingress, egress to triage +
  knowledge + DNS; triage ingress from orchestrator only, no egress but DNS (the model download
  is the init container's: 443); knowledge ingress from orchestrator, egress 443 + DNS. With
  KubeAI, triage and the orchestrator (the Guard's model, 10.9) may reach the KubeAI proxy
  (:8000), and the rules of KubeAI's namespace let only these two in.
- Grimoire: an OAuth client of its gateway (registration + PKCE). The browser step happens on the
  laptop (`wattson grimoire connect`, enrolled Chrome); only the one-time code passes through the
  CLI. Tokens stay in the knowledge agent. Read-only MCP tools only (`tool_filters`).
- Redaction (`wattson.shared.redact`) in the connector before evidence leaves the laptop, and in
  the orchestrator before anything enters a prompt, the conversation, or a record.
- Logs: method, path, status, duration. No bodies, tokens or prompts.

### 10.8 Evaluation targets
- `eval/requests.jsonl` ≥ 60 labelled requests. Accuracy ≥ 85 % overall, ≥ 90 % above
  `min_confidence` with ≥ 70 % coverage, ECE < 0.10, p95 < 8 s per triage on 2 CPUs, model RAM
  < 2 GiB. Measured in Docker with `--cpus 2` as a stand-in for the pod (a measurement, not part
  of the product: the laptop runs no model).

### 10.9 The Guard: Strands interventions, `aux_model` and Cedar; rules first, the model only stricter
- **What.** Each request gets a `GuardVerdict` before anything else (`work`, `out_of_scope`,
  `risky`, `malicious`); the evidence and the Grimoire documents are scanned for prompt
  injection, and the lines that give orders to the agent are cut out before the prompt.
- **How (decision).** Strands' own features, no new agent: an intervention before the `guard`
  step (the request), an intervention after `find_procedures` and `collect_evidence` (the
  evidence), the orchestrator Agent's `aux_model` (the KubeAI Granite of triage, through the
  same proxy) and Strands' `CedarAuthorization` (`policies.cedar`: a second, declarative lock:
  `run_on_laptop` only after the approval of that exact prompt hash).
- **Rules first, the model stricter only** (FR-DEC-06). Fixed rules (`rules.yaml`, the user's
  file) decide first; the model may turn a verdict into a stricter one (Deny or Confirm), never
  allow what a rule forbids. A slow or missing model (20 s, `GUARD_TIMEOUT_S`; no model with the
  sidecar) leaves the rules alone. The rules and policies are data: a ConfigMap
  `wattson-guard`, mounted read-only at `/etc/wattson/guard`; `k8s/guard.sh` changes it without
  a new image, and checks it with the Guard's own code first.
- **Modes.** `monitor` (the start: verdicts in the traces and at the review, nothing blocked;
  the injection cut still runs), `enforce` (malicious and out of scope → status `blocked`, nothing
  runs; risky → interrupt `wattson.guard_confirm`, "Go on with a read-only investigation?",
  default no), `off`. Switch to `enforce` after the evaluation (`eval/guard.py`).
- **Telemetry.** Spans `wattson.guard.request` (label, method, mode, rule hits, latency) and
  `wattson.guard.evidence` (parts removed); `wattson.task.end` has the outcome `blocked`. Never
  the request or the evidence.

## Admin routes (plain HTTP on the same apps, same bearer)

| Agent | Route | Body → answer |
|---|---|---|
| all | `GET /healthz` (no token, for probes) | `{"ok": true}` |
| orchestrator | `GET /v1/health` | `HealthView` |
| orchestrator | `GET /v1/tasks?limit=20`, `GET /v1/tasks/{id}`, `GET /v1/tasks/{id}/events` | `TaskRecord[]`, `TaskRecord`, `TaskEvent[]` |
| orchestrator | `POST /v1/tasks/{id}/execution` | `ExecutionReport` → `TaskRecord` |
| orchestrator | `GET /v1/grimoire/status`, `POST /v1/grimoire/connect`, `POST /v1/grimoire/callback`, `POST /v1/grimoire/disconnect` | proxied to the knowledge agent's same routes |
| knowledge | the four `/v1/grimoire/*` routes | `GrimoireStatus`, `GrimoireConnectRequest` → `GrimoireConnectView`, `GrimoireCallback` → `GrimoireStatus` |
| triage | `GET /v1/health` | `ComponentHealth` (model up, name) |

## Code rules
- Python 3.13, `uv`. `uv run pytest tests/<part>` and `uv run ruff check src tests` must pass.
  Dependencies only from `pyproject.toml` (Strands 1.62 with `a2a`, httpx, pydantic, rich).
- Tests never touch a real cluster, the real Grimoire, or the real `claude`: stand-ins in
  `tests/fixtures/`.
- Plain English in messages and docs: short sentences.

## Who owns what

| Part | Files |
|---|---|
| Integration | `src/wattson/shared/{contracts,planner,auth}.py`, `docs/design.md`, `README.md`, `pyproject.toml`, `uv.lock` |
| Orchestrator | `src/wattson/agents/orchestrator/` (not `workflows.py`, not `templates/`), `tests/orchestrator/` |
| Triage + eval | `src/wattson/agents/triage/`, `eval/`, `docs/decision-model.md`, `tests/triage/` |
| Knowledge | `src/wattson/agents/knowledge/`, `tests/knowledge/`, `tests/fixtures/grimoire_stub.py` |
| Workflows, templates, redaction | `src/wattson/agents/orchestrator/workflows.py`, `src/wattson/agents/orchestrator/templates/`, `src/wattson/shared/redact.py`, `tests/templates/`, `tests/redact/` |
| Connector | `src/wattson/connector/`, `k8s/reader.yaml`, `tests/connector/`, `tests/fixtures/bin/` |
| CLI | `src/wattson/cli/`, `docs/cli.md`, `tests/cli/` |
| Kubernetes | `Dockerfile`, `.dockerignore`, `settings.env`, `k8s/` (not `reader.yaml`), `tests/k8s/`, `docs/deploy.md` |
