// Wattson Guard - Cedar policies (the source of truth).
//
// These policies are a SECOND lock on the task's step order, evaluated by Strands'
// CedarAuthorization intervention before every orchestrator step (each step is a Cedar action,
// e.g. Action::"run_on_laptop"). The ApprovalGate hook enforces the same rule in code; Cedar is
// the independent, declarative copy.
//
// The other agent mounts this file into the orchestrator from a ConfigMap at
// /etc/wattson/guard/policies.cedar, and GUARD_POLICIES_FILE points at it. A copy is packaged in
// the image as the default, used when the file is missing or invalid.
//
// Context available to each policy (injected by the handler):
//   context.session.approved   true only when the user has approved THIS task's exact prompt hash.
//   context.session.hour_utc   the current hour (0-23), added by the handler.
//   context.session.call_count how many times this action has run in the session.
//
// Cedar is deny-by-default: a request is allowed only if some `permit` matches and no `forbid`
// does. Edit here and redeploy; no code change.

// 1) Permit every step EXCEPT the one that runs Claude Code on the laptop. Triage, procedures,
//    questions, evidence planning and collection, composing and the approval request are all the
//    agent's own read-only orchestration, always allowed.
permit(principal, action, resource)
  unless { action == Action::"run_on_laptop" };

// 2) run_on_laptop hands the approved prompt to the laptop. Allow it only once the user has
//    approved this exact prompt (the handler sets context.session.approved to match the task's
//    approved hash, exactly like the ApprovalGate hook).
permit(principal, action == Action::"run_on_laptop", resource)
  when { context.session.approved == true };
