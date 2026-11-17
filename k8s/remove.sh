#!/usr/bin/env bash
# Removes Wattson Agent from the cluster: the three Deployments, the two Services, the three
# NetworkPolicies, the three ServiceAccounts, the ConfigMap with the rules of the Guard and the
# Secret with the tokens; and Tempo (its
# Deployment, Service, NetworkPolicy, ServiceAccount and ConfigMap) when it is there. It asks
# first: you type the name of the app.
#
#   bash k8s/remove.sh                 the volumes are KEPT: the task history, the Grimoire
#                                      connection, the model and the traces are there on the
#                                      next deploy
#   bash k8s/remove.sh --delete-data   the volumes go too. Their data cannot come back.
#
# Only objects that carry the label app.kubernetes.io/part-of=<APP_NAME> are removed. The
# namespace stays. The read-only access that "wattson k8s add" made (the account
# wattson-reader) is not touched: see docs/cli.md. The CLI settings on this laptop
# (~/.config/wattson) are not touched either, nor the data source and the dashboard that
# k8s/grafana.sh added to Grafana (delete them in Grafana).
set -euo pipefail

# shellcheck source=k8s/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

DELETE_DATA=no
for arg in "$@"; do
  case "$arg" in
    --delete-data) DELETE_DATA=yes ;;
    -h | --help)
      sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "Unknown option: $arg. See: bash k8s/remove.sh --help" ;;
  esac
done

read_settings
check_settings
command -v kubectl >/dev/null 2>&1 || die "kubectl is not installed."
seen_context="$(kubectl --context "$K8S_CONTEXT" config view --minify -o 'jsonpath={.contexts[0].name}' 2>/dev/null || true)"
[[ "$seen_context" == "$K8S_CONTEXT" ]] || die "kubectl has no context \"$K8S_CONTEXT\". See: kubectl config get-contexts"

OBJECTS=(
  "deployment/$ORCHESTRATOR" "deployment/$TRIAGE" "deployment/$KNOWLEDGE"
  "service/$TRIAGE" "service/$KNOWLEDGE"
  "networkpolicy/$ORCHESTRATOR" "networkpolicy/$TRIAGE" "networkpolicy/$KNOWLEDGE"
  "serviceaccount/$ORCHESTRATOR" "serviceaccount/$TRIAGE" "serviceaccount/$KNOWLEDGE"
  "configmap/$GUARD_CONFIGMAP" "secret/$APP_NAME"
  "deployment/$TEMPO" "service/$TEMPO" "networkpolicy/$TEMPO" "serviceaccount/$TEMPO" "configmap/$TEMPO"
)
VOLUMES=("persistentvolumeclaim/$APP_NAME-data" "persistentvolumeclaim/$APP_NAME-knowledge-data" "persistentvolumeclaim/$APP_NAME-models"
  "persistentvolumeclaim/$APP_NAME-tempo-data")
if [[ "$DELETE_DATA" == yes ]]; then OBJECTS+=("${VOLUMES[@]}"); fi

# What is there and ours. A failing kubectl stops the script: nothing is removed half-seen.
found=() foreign=()
for object in "${OBJECTS[@]}"; do
  seen="$(k get "${object%%/*}" "${object#*/}" --ignore-not-found -o 'jsonpath={.metadata.name}{"|"}{.metadata.labels.app\.kubernetes\.io/part-of}')" \
    || die "kubectl could not look for $object (its own words are above). Nothing was removed."
  [[ -n "$seen" ]] || continue
  if [[ "${seen#*|}" == "$APP_NAME" ]]; then found+=("$object"); else foreign+=("$object"); fi
done

info "Cluster $K8S_CONTEXT, namespace $K8S_NAMESPACE."
if [[ ${#foreign[@]} -gt 0 ]]; then
  info "These have our names but were not made by k8s/deploy.sh; they are left alone: ${foreign[*]}"
fi
if [[ ${#found[@]} -eq 0 ]]; then
  info "Nothing of $APP_NAME is there. Nothing to remove."
  exit 0
fi
info "To be removed:"
printf '  %s\n' "${found[@]}"
if [[ "$DELETE_DATA" == yes ]]; then
  info "WITH THE VOLUMES: the task history, the Grimoire connection, the model and the traces are lost."
else
  info "The volumes are kept: ${VOLUMES[*]#persistentvolumeclaim/}"
fi

printf 'Type "%s" to remove it: ' "$APP_NAME"
answer=""
IFS= read -r answer || true
if [[ "$answer" != "$APP_NAME" ]]; then
  info ""
  info "Nothing was removed."
  exit 1
fi

# The etcd of this cluster sometimes answers too late: a delete is tried up to three times.
DELETE_RETRY_SECONDS="${DELETE_RETRY_SECONDS:-10}"
failed=0
for object in "${found[@]}"; do
  done_it=no
  for try in 1 2 3; do
    if output="$(k delete "${object%%/*}" "${object#*/}" --ignore-not-found --wait=false 2>&1)"; then
      done_it=yes
      break
    fi
    if [[ "$try" -lt 3 ]]; then sleep "$DELETE_RETRY_SECONDS"; fi
  done
  if [[ "$done_it" == yes ]]; then
    info "  removed $object"
  else
    failed=$((failed + 1))
    info "  NOT removed $object: $(tail -n 1 <<<"$output")"
  fi
done
[[ "$failed" -eq 0 ]] || die "$failed object(s) could not be removed (see above). Run the script again."
info "Done. To deploy again: bash k8s/deploy.sh (with the volumes kept, the model is not downloaded again)."
info "The CLI settings on this laptop ($(cli_config_dir)) are kept. The next deploy makes new tokens"
info "and writes the new one there."
