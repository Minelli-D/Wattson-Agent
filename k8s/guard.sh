#!/usr/bin/env bash
# Changes the rules of the Guard on the cluster, with no new image and no full deploy. It checks
# your two files, k8s/guard/rules.yaml and k8s/guard/policies.cedar, puts their exact text in the
# ConfigMap <APP_NAME>-guard, and restarts the orchestrator (only it), so that it reads them.
# Without --apply it only checks the files and prints what it would do; it runs no kubectl.
#
#   bash k8s/guard.sh            check the two files and print the plan; nothing runs on the cluster
#   bash k8s/guard.sh --apply    the same, then you type the name of the context, then it applies
#                                the ConfigMap and restarts the orchestrator
#
# Run bash k8s/deploy.sh once before: it makes the ConfigMap and mounts it in the orchestrator.
# The restart ends the tasks that are open on the orchestrator: run it when no task is open.
# GUARD_MODE (enforce, monitor, off) and GUARD_TIMEOUT_S are in settings.env: to change them,
# run bash k8s/deploy.sh. Then watch the Guard in Grafana: the dashboard "Wattson Agent".
#
# ROLLOUT_WAIT_SECONDS=600 bash k8s/guard.sh --apply    to wait longer for the new pod
set -euo pipefail

# shellcheck source=k8s/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

APPLY=no
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=yes ;;
    -h | --help)
      sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "Unknown option: $arg. See: bash k8s/guard.sh --help" ;;
  esac
done

read_settings
check_settings
# A wrong file stops the script here, before any kubectl call.
check_guard_files

ANNOTATION=wattson/guard-config
PATCH="{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"$ANNOTATION\":\"$GUARD_CONFIG_SHA256\"}}}}}"
WAIT="${ROLLOUT_WAIT_SECONDS:-300}"
[[ "$WAIT" =~ ^[1-9][0-9]{0,4}$ ]] || die "ROLLOUT_WAIT_SECONDS must be a whole number of seconds."

info "# The rules of the Guard for $APP_NAME: context $K8S_CONTEXT, namespace $K8S_NAMESPACE."
info "# Files: $GUARD_DIR/$GUARD_RULES_NAME and $GUARD_DIR/$GUARD_POLICIES_NAME (SHA-256 $GUARD_CONFIG_SHA256)."
sed -n 's/^NOTE: /# Checked: /p' <<<"$GUARD_CHECK_NOTES"
if [[ -n "$GUARD_MODEL_URL" ]]; then
  info "# Mode $GUARD_MODE (settings.env): fixed rules first, then the small model $GUARD_MODEL (KubeAI)."
else
  info "# Mode $GUARD_MODE (settings.env): fixed rules only (no model the orchestrator can reach)."
fi
info "# What --apply runs, after you type the name of the context:"
info "kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE apply -f -   # the ConfigMap $GUARD_CONFIGMAP, made from the two files"
info "kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE patch deployment $ORCHESTRATOR --type merge -p '$PATCH'"
info "kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE rollout status deployment/$ORCHESTRATOR --timeout=${WAIT}s"

if [[ "$APPLY" != yes ]]; then
  info ""
  info "Nothing was run. To put these rules on the cluster: bash k8s/guard.sh --apply"
  exit 0
fi

# ---- --apply: before the question it only reads -------------------------------------------

command -v kubectl >/dev/null 2>&1 || die "kubectl is not installed."
seen_context="$(kubectl --context "$K8S_CONTEXT" config view --minify -o 'jsonpath={.contexts[0].name}' 2>/dev/null || true)"
[[ "$seen_context" == "$K8S_CONTEXT" ]] || die "kubectl has no context \"$K8S_CONTEXT\". See: kubectl config get-contexts"

could_not_look() {  # WHAT
  die "kubectl could not read $1 in the namespace \"$K8S_NAMESPACE\" (its own words are above). Nothing was changed. Check the connection (VPN) and run again."
}
owner_of() {  # KIND NAME -> "name|part-of label", or nothing when it is not there
  k get "$1" "$2" --ignore-not-found -o 'jsonpath={.metadata.name}{"|"}{.metadata.labels.app\.kubernetes\.io/part-of}'
}

seen="$(owner_of deployment "$ORCHESTRATOR")" || could_not_look "the Deployment $ORCHESTRATOR"
[[ -n "$seen" ]] || die "There is no Deployment $ORCHESTRATOR in \"$K8S_NAMESPACE\". Run bash k8s/deploy.sh first. Nothing was changed."
[[ "${seen#*|}" == "$APP_NAME" ]] || die "The Deployment $ORCHESTRATOR was not made by k8s/deploy.sh. Nothing was changed."
volumes="$(k get deployment "$ORCHESTRATOR" -o 'jsonpath={.spec.template.spec.volumes[*].configMap.name}')" \
  || could_not_look "the Deployment $ORCHESTRATOR"
if ! grep -qxF "$GUARD_CONFIGMAP" <<<"${volumes// /$'\n'}"; then
  die "The orchestrator on the cluster does not read its rules from $GUARD_CONFIGMAP: it was deployed before the Guard. Run bash k8s/deploy.sh (it puts the Guard there too). Nothing was changed."
fi
seen="$(owner_of configmap "$GUARD_CONFIGMAP")" || could_not_look "the ConfigMap $GUARD_CONFIGMAP"
if [[ -n "$seen" && "${seen#*|}" != "$APP_NAME" ]]; then
  die "The ConfigMap $GUARD_CONFIGMAP was not made by k8s/deploy.sh. Nothing was changed. Rename the app (APP_NAME) or remove it yourself."
fi
running="$(k get deployment "$ORCHESTRATOR" -o "jsonpath={.spec.template.metadata.annotations.${ANNOTATION//./\\.}}")" \
  || could_not_look "the Deployment $ORCHESTRATOR"
if [[ "$running" == "$GUARD_CONFIG_SHA256" && -n "$seen" ]]; then
  info ""
  info "The orchestrator runs these rules already (the same SHA-256). Nothing to do."
  exit 0
fi

info ""
printf 'Type "%s" to put these rules on that cluster and restart the orchestrator: ' "$K8S_CONTEXT"
answer=""
IFS= read -r answer || true
if [[ "$answer" != "$K8S_CONTEXT" ]]; then
  info ""
  info "Nothing was changed."
  exit 1
fi

info "ConfigMap: applying $GUARD_CONFIGMAP."
guard_configmap | k apply -f - | sed 's/^/  /' \
  || die "The ConfigMap $GUARD_CONFIGMAP could not be applied (the words of kubectl are above). The orchestrator was not restarted."
info "Orchestrator: restarting $ORCHESTRATOR, so that it reads the new rules."
k patch deployment "$ORCHESTRATOR" --type merge -p "$PATCH" | sed 's/^/  /' \
  || die "The orchestrator could not be restarted (the words of kubectl are above). The new rules are in $GUARD_CONFIGMAP; run this again."
k rollout status "deployment/$ORCHESTRATOR" --timeout="${WAIT}s" >/dev/null \
  || die "$ORCHESTRATOR is not running after ${WAIT}s. Look at it: bash k8s/doctor.sh (or kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE logs deployment/$ORCHESTRATOR)"
info "  $ORCHESTRATOR: running."
info ""
info "Done. The Guard uses the new rules, in mode $GUARD_MODE."
info "See its decisions in Grafana: the dashboard \"$GRAFANA_DASHBOARD_TITLE\", the panels of the Guard."
