# Personal Engineering Agent
## Requirements Specification — v0.1

The application built from this spec is called Wattson Agent.

**Status:** Initial requirements established  
**Scope:** MVP definition  
**Primary user:** Individual engineer  
**Primary knowledge source:** Grimoire MCP  
**Deployment target:** Kubernetes  
**Initial coding agent:** Claude Code CLI  
**User interface:** Local command-line interface

---

## 1. Project Overview

### 1.1 Purpose

Build a personal AI engineering agent that helps the user investigate technical problems, understand project context, improve task instructions, and coordinate coding work.

The agent will use the user's existing Grimoire MCP server as its operational knowledge source. A small, quantized decision model will run on CPU inside Kubernetes, while Claude Code performs substantial coding and engineering tasks on the user's laptop.

The system must combine contextual knowledge, automated decision support, controlled access to technical information, and human approval.

### 1.2 Core objectives

The system must:

1. Retrieve relevant procedures and runbooks from Grimoire.
2. Understand engineering requests and classify them into appropriate workflows.
3. Gather relevant information from local Git repositories and authorized Kubernetes clusters.
4. Construct structured prompts using deterministic templates.
5. Identify missing information and request clarification when necessary.
6. Let the user inspect and edit proposed tasks before submission.
7. Submit approved tasks to an existing Claude Code conversation.
8. Track task execution and maintain a minimal operational history.

### 1.3 Design principles

- **Grimoire first:** Reuse the existing knowledge base rather than building a competing one.
- **Kubernetes-hosted intelligence:** All agent orchestration and model inference run in Kubernetes.
- **No local inference:** The laptop hosts no AI model.
- **Human oversight:** The user controls task submission and consequential actions.
- **Least privilege:** Components receive only the permissions they require.
- **Data minimization:** Retrieve and transmit only relevant information.
- **Deterministic prompt generation:** Use structured templates in the MVP.
- **Extensibility:** Allow additional models, tools, and coding-agent integrations later.

---

## 2. System Architecture

### 2.1 High-level architecture

**Component A — Agent Platform.** Runs in Kubernetes and contains the orchestration logic, decision model integration, task workflow, and approval-state management.

**Component B — Grimoire Integration.** Connects to the existing Grimoire MCP server to retrieve procedures, runbooks, documentation, and relevant technical context.

**Component C — Local CLI and Connector.** Runs on the laptop when the user starts the CLI. It communicates with the Kubernetes agent and provides authorized access to local repositories and the Claude Code CLI. It performs no model inference and does not require a persistent background service.

**Component D — Decision Model.** A small, quantized model running on CPU in Kubernetes. It classifies requests, recommends relevant context, and supports decisions about the next step.

### 2.2 Conceptual architecture

    USER -> Local CLI -> (secure, on-demand connection) -> KUBERNETES
      Agent Orchestrator; Decision Model (request classification, context selection
      recommendations, uncertainty assessment); Grimoire MCP Integration; Context Assembly;
      Deterministic Prompt Templates; Approval Workflow
    -> Approved Task -> Local CLI Connector (local Git repositories, Claude Code CLI,
       authorized local Kubernetes access) -> Execution Result -> Kubernetes Agent
       (result reporting, minimal operational history)

The diagram describes logical responsibilities, not a finalized deployment topology.

---

## 3. Functional Requirements

### 3.1 Grimoire Integration

- **FR-GRIM-01 — Existing MCP integration.** The agent must use the existing Grimoire MCP server to access the user's knowledge base.
- **FR-GRIM-02 — Relevant document retrieval.** The agent must retrieve procedures and documentation relevant to the current request.
- **FR-GRIM-03 — Contextual retrieval.** When useful, the agent should retrieve related documents rather than relying exclusively on an isolated search result.
- **FR-GRIM-04 — Source attribution.** The agent must identify which Grimoire documents informed a proposed task.
- **FR-GRIM-05 — No duplicated knowledge base.** The MVP must not introduce a second knowledge repository that duplicates Grimoire's existing functionality.

### 3.2 Project and Git Context

- **FR-GIT-01 — Repository detection.** The system must identify the current project and Git repository when the local environment makes this information available.
- **FR-GIT-02 — Repository selection.** The user must be able to select or override the detected repository.
- **FR-GIT-03 — Relevant Git information.** The agent must be able to obtain authorized information such as repository structure, Git history, and working-tree status.
- **FR-GIT-04 — Context minimization.** The agent must collect relevant project information rather than automatically transmitting entire repositories.
- **FR-GIT-05 — Context transparency.** The user must be able to inspect which repository and project information will be used.

### 3.3 Kubernetes Integration

- **FR-K8S-01 — Hosting cluster.** The system must support investigations involving authorized resources in the cluster hosting the agent.
- **FR-K8S-02 — Additional clusters.** The architecture must support other explicitly authorized Kubernetes clusters.
- **FR-K8S-03 — Cluster selection.** The user must be able to select the target cluster when automatic selection is unavailable or ambiguous.
- **FR-K8S-04 — Read-only initial access.** The MVP must use read-only Kubernetes permissions for infrastructure investigations.
- **FR-K8S-05 — Relevant diagnostics.** The agent must be able to inspect authorized resources such as deployment status, pod status, events, relevant configuration, and application logs.
- **FR-K8S-06 — Explicit target context.** The proposed investigation must identify the target cluster and namespace (one, several, or "all namespaces": D43).
- **FR-K8S-07 — No automatic infrastructure modification.** The agent must not independently modify Kubernetes resources during the initial implementation.

### 3.4 Request Classification and Decision Support

- **FR-DEC-01 — Request classification.** The decision model must classify requests into supported task categories, including: prompt optimization; DevOps investigation; other engineering tasks, where supported by the configured workflow.
- **FR-DEC-02 — Context recommendations.** The model must help identify which procedures, repositories, and infrastructure resources are relevant.
- **FR-DEC-03 — Next-action recommendation.** The model must recommend the next step based on the request, available context, and uncertainty.
- **FR-DEC-04 — Uncertainty handling.** The system must recognize insufficient information and support asking the user for clarification.
- **FR-DEC-05 — Confidence evaluation.** Confidence scores must be evaluated against representative examples before being used to influence automation decisions.
- **FR-DEC-06 — Deterministic authorization.** The decision model must not grant permissions, override security controls, or authorize its own proposed actions.

These requirements do not assume that a small model will perform every decision equally well. Its classification, routing, and uncertainty-assessment capabilities must be evaluated separately.

### 3.5 Prompt Generation

- **FR-PROMPT-01 — Deterministic templates.** The orchestrator must generate prompts using structured, reusable templates.
- **FR-PROMPT-02 — Context enrichment.** Prompts must incorporate relevant information from Grimoire, authorized Git data, and Kubernetes diagnostics.
- **FR-PROMPT-03 — Explicit objectives.** Each generated task must clearly state its objective and expected outcome.
- **FR-PROMPT-04 — Evidence and constraints.** Prompts must distinguish available evidence from hypotheses and identify applicable constraints.
- **FR-PROMPT-05 — Missing information.** The agent must identify missing information that materially affects the task and ask the user for clarification when necessary.
- **FR-PROMPT-06 — Extensible templates.** New task types must be supportable through additional templates and workflow definitions.
- **FR-PROMPT-07 — Future LLM support.** The design must allow an LLM-based prompt optimizer to be added later without replacing the main orchestration layer.

### 3.6 Human Approval

- **FR-APP-01 — Review before submission.** The CLI must display the complete proposed prompt before it is submitted to Claude Code.
- **FR-APP-02 — Direct editing.** The user must be able to edit the proposed prompt.
- **FR-APP-03 — Evidence inspection.** The user must be able to inspect the relevant Grimoire documents, Git information, and Kubernetes evidence used to prepare the task.
- **FR-APP-04 — Explicit approval.** The system must require explicit user approval before submitting a prepared task to Claude Code.
- **FR-APP-05 — Rejection.** The user must be able to reject a proposed task without submitting it.
- **FR-APP-06 — Source transparency.** The system must show where relevant contextual information originated.
- **FR-APP-07 — Freshness.** The system must distinguish current diagnostic information from cached or potentially outdated evidence where applicable.
- **FR-APP-08 — Approval boundaries.** Approving a task for submission must not automatically authorize unrelated or consequential actions that the coding agent might attempt.

### 3.7 Claude Code Integration

- **FR-CC-01 — Initial provider.** Claude Code CLI must be the first supported coding-agent integration.
- **FR-CC-02 — Existing conversations.** The integration must support selecting and resuming an existing Claude Code conversation when the relevant session is available.
- **FR-CC-03 — Conversation identification.** The system must allow the user to select a conversation when automatic identification is ambiguous.
- **FR-CC-04 — Approved submission.** The local connector must submit the approved task to Claude Code.
- **FR-CC-05 — Permission preservation.** The integration must preserve Claude Code's existing permission controls and must not bypass them by default.
- **FR-CC-06 — Execution reporting.** The system must report the execution status and available results to the Kubernetes agent.
- **FR-CC-07 — Error handling.** The system must report failures to start, resume, or complete a task.
- **FR-CC-08 — Future provider support.** The design should allow a future Codex integration without redesigning the entire orchestration system.

The precise mechanism for invoking Claude Code and associating the request with the correct session must be validated during implementation design.

### 3.8 Local CLI and Connectivity

- **FR-CLI-01 — Command-line interface.** The user must interact with the agent through a terminal-based CLI.
- **FR-CLI-02 — On-demand operation.** The local connector must run on demand, without requiring an always-running background service.
- **FR-CLI-03 — No local inference.** The CLI must not host or execute the decision model.
- **FR-CLI-04 — Secure connection.** The CLI must establish authenticated communication with the Kubernetes agent.
- **FR-CLI-05 — Local execution.** The CLI must provide the controlled local execution capability required to access Git repositories and invoke Claude Code.
- **FR-CLI-06 — Manual overrides.** The user must be able to override automatic project and conversation selection.
- **FR-CLI-07 — Safe disconnection.** The system must define how active tasks are tracked when the CLI disconnects or the user exits.

---

## 4. Security and Safety Requirements

- **SEC-01** Retrieve and transmit only information necessary for the current task.
- **SEC-02** Avoid transmitting complete repositories, unrelated logs, or unnecessary infrastructure configuration.
- **SEC-03** Retrieve only the Kubernetes resources relevant to an investigation.
- **SEC-04** Detect likely credentials, including API keys, access tokens, passwords, and private keys.
- **SEC-05** Redact detected secrets before transmitting contextual information to another component or including it in a prompt.
- **SEC-06** Do not deliberately include credentials in coding-agent prompts.
- **SEC-07** If sensitive information cannot be handled safely, stop the affected operation and notify the user.
- **SEC-08** Each component must have only the permissions needed for its responsibilities.
- **SEC-09** The initial Kubernetes investigation workflow must use read-only access.
- **SEC-10** The agent must not independently authorize or perform consequential infrastructure operations.
- **SEC-11** Existing Claude Code permission controls must remain effective.
- **SEC-12** Sensitive credentials must not be transferred unnecessarily between the laptop and Kubernetes.
- **SEC-13** The system must maintain an operational record of task submission, approval, execution status, and relevant errors.
- **SEC-14** Sensitive information must not be unnecessarily copied into logs or task history.
- **SEC-15** The system must distinguish a proposed action from an approved action and from a completed action.

Secret detection and redaction are defense-in-depth controls. They do not replace restricted access, secure credential storage, or appropriate authorization.

---

## 5. Operational History and Memory

- **MEM-01 — Task records.** Store task identifiers and the minimum metadata necessary to track and audit execution.
- **MEM-02 — Approval records.** Record the approval decision and its associated task.
- **MEM-03 — Execution status.** Record timestamps, status changes, and relevant error summaries.
- **MEM-04 — No automatic conversational memory.** Do not automatically build a persistent personal-memory system from previous conversations.
- **MEM-05 — No training assumption.** Storing task records must not imply that the task history will be used to train or fine-tune a model.
- **MEM-06 — Separate concerns.** Keep operational history separate from Grimoire's reusable procedures and documentation.

---

## 6. MVP Scope

### 6.1 Included

1. A Kubernetes-hosted agent orchestrator.
2. A small, quantized, CPU-based decision model.
3. Integration with the existing Grimoire MCP server.
4. A local CLI that connects to Kubernetes on demand.
5. Automatic project and repository context detection, with manual overrides.
6. Access to authorized Git and project information.
7. Read-only investigation of authorized Kubernetes resources.
8. Deterministic prompt templates.
9. Prompt review and editing through the CLI.
10. Evidence inspection before approval.
11. Submission of approved tasks to an existing Claude Code conversation.
12. Execution status and error reporting.
13. Minimal operational history.
14. Secret detection, redaction, and least-privilege controls.

### 6.2 Explicitly outside the initial scope

Independent modification of Kubernetes infrastructure; autonomous deployment or production changes; a mandatory GPU-based inference environment; a local AI model on the laptop; a persistent local background service; a web-based user interface; a second knowledge base duplicating Grimoire; automatic creation of persistent conversational memory; training or fine-tuning a model using task history; native integration with multiple coding agents from day one; mandatory integration with CI/CD or cloud-observability systems; an LLM dedicated to prompt optimization.

---

## 7. Proposed End-to-End Workflow

1. **Request** — `myagent ask "Investigate why my Kubernetes deployment is failing"`
2. **Context detection** — project, repository, conversation, infrastructure; ask when ambiguous.
3. **Request classification** — the decision model recommends a workflow.
4. **Knowledge retrieval** — Grimoire MCP.
5. **Evidence collection** — only authorized, relevant Git and Kubernetes information.
6. **Context assembly** — request, procedures, evidence, constraints; secrets redacted.
7. **Prompt generation** — deterministic template: objective, context, evidence, constraints, expected outcome.
8. **Human review** — inspect, edit, approve, or reject.
9. **Submission** — the local connector invokes Claude Code with the selected conversation.
10. **Execution tracking** — status and results.
11. **Result reporting** — confirmed findings vs unresolved issues.
12. **Operational record** — minimum information for tracking and auditing.

---

## 8. Non-Functional Requirements

- **NFR-01 — Resource efficiency.** CPU only; memory and CPU measured and documented.
- **NFR-02 — Reliability.** Failures in MCP calls, model inference, Kubernetes access, and Claude Code invocation handled explicitly.
- **NFR-03 — Timeouts.** Defined timeouts and error handling for external operations.
- **NFR-04 — Observability.** Enough operational information to diagnose failures without logging sensitive context.
- **NFR-05 — Reproducibility.** Enough task metadata to reconstruct the sequence of major workflow events.
- **NFR-06 — Extensibility.** New workflows, templates, and coding-agent integrations through modular components.
- **NFR-07 — Decision quality.** The classifier is evaluated on representative requests before it influences workflow decisions.
- **NFR-08 — Confidence calibration.** Confidence estimates tested for reliability; no threshold bypasses security or approval controls.
- **NFR-09 — Graceful failure.** If a source is unavailable, report the limitation rather than invent evidence.
- **NFR-10 — Safe task recovery.** Defined behaviour when the CLI disconnects, Claude Code fails, or an execution remains incomplete.

---

## 9. Acceptance Criteria for the MVP

- **AC-01 — Grimoire retrieval.** Relevant procedures retrieved and sources identified.
- **AC-02 — Project context.** The repository identified and only relevant information collected.
- **AC-03 — Kubernetes investigation.** Authorized diagnostics obtained without modifying resources.
- **AC-04 — Structured prompt.** Objective, evidence, constraints, expected outcome.
- **AC-05 — Human approval.** No submission before explicit approval.
- **AC-06 — Prompt editing.** The user edits and submits the reviewed version.
- **AC-07 — Evidence inspection.** The user inspects the sources behind the task.
- **AC-08 — Claude Code integration.** An approved task reaches the selected existing conversation.
- **AC-09 — Secret protection.** Known test credentials detected and redacted before submission.
- **AC-10 — Error handling.** Grimoire, Kubernetes, model and Claude Code failures reported explicitly.
- **AC-11 — Operational history.** Ids, approvals, status changes and error summaries recorded without complete conversations or sensitive data.
- **AC-12 — CPU-only inference.** The decision model runs in the target Kubernetes environment without a GPU.

---

## 10. Outstanding Engineering Decisions

10.1 Kubernetes access architecture · 10.2 Decision model · 10.3 Local connector protocol ·
10.4 Claude Code session handling · 10.5 Operational storage · 10.6 Execution lifecycle ·
10.7 Security configuration · 10.8 Evaluation and performance targets.
The answers are in [`design.md`](design.md).

---

## 11. Definition of Success

The user submits an engineering request through the CLI and the Kubernetes-hosted agent reliably: understands and classifies it; retrieves useful procedures from Grimoire; gathers relevant authorized project and infrastructure context; produces an actionable prompt through deterministic templates; makes its evidence and proposed actions transparent; obtains explicit user approval; submits the approved task to the intended Claude Code conversation; reports the outcome; records the minimum operational history — without running an AI model on the laptop, duplicating Grimoire, or allowing the decision model to override security controls.

**The immediate objective is a reliable, context-aware engineering assistant — not an unrestricted autonomous agent.**
