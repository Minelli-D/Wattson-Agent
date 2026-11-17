#!/usr/bin/env bash
# Removes the OLD install of Wattson Agent, from before its names changed from myagent to
# wattson (23 Oct 2026). Run it LAST, when the new install works (wattson status and
# bash k8s/doctor.sh say ok): see "Move from myagent to wattson" in running.md.
# Without --apply it only prints what is there; it deletes nothing.
#
#   bash k8s/remove-old-myagent.sh                     print what belongs to the old install
#   bash k8s/remove-old-myagent.sh --apply             then you type the name of the context, then
#                                                      it deletes them. The old volumes go only
#                                                      after a second question: you type "yes"
#   bash k8s/remove-old-myagent.sh --apply --keep-data the same, and the old volumes are kept
#
# What it looks for, each object only when it has the label app.kubernetes.io/part-of=myagent:
#   namespace K8S_NAMESPACE: the Deployments, Services, NetworkPolicies, ServiceAccounts and the
#     ConfigMap myagent-*; the Secret myagent (the old tokens); the old read-only account
#     myagent-reader and its Secret myagent-reader-token; and the volumes myagent-data,
#     myagent-knowledge-data, myagent-models and myagent-tempo-data (the old task history, the
#     old Grimoire login, the old model file, the old traces)
#   the whole cluster: the ClusterRoleBindings myagent-reader-view and myagent-reader-nodes, and
#     the ClusterRole myagent-reader-nodes
#   namespace KUBEAI_NAMESPACE: nothing to delete. The KubeAI Model (KUBEAI_MODEL) is kept:
#     bash k8s/kubeai.sh --apply gives it the new label.
# An object with an old name and another label is left alone. Every kubectl call names the
# context of settings.env. The laptop folders of the old name (~/.config/myagent,
# ~/.local/state/myagent) are not touched: delete them yourself. The old dashboard in Grafana:
# bash k8s/grafana.sh --apply removes it.
set -euo pipefail

# shellcheck source=k8s/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

APPLY=no
KEEP_DATA=no
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=yes ;;
    --keep-data) KEEP_DATA=yes ;;
    -h | --help)
      sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "Unknown option: $arg. See: bash k8s/remove-old-myagent.sh --help" ;;
  esac
done

read_settings
check_settings
OLD=myagent
[[ "$APP_NAME" != "$OLD" ]] || die "APP_NAME is $OLD in settings.env: that is the install this script would delete. Nothing was done."
command -v kubectl >/dev/null 2>&1 || die "kubectl is not installed."
seen_context="$(kubectl --context "$K8S_CONTEXT" config view --minify -o 'jsonpath={.contexts[0].name}' 2>/dev/null || true)"
[[ "$seen_context" == "$K8S_CONTEXT" ]] || die "kubectl has no context \"$K8S_CONTEXT\". See: kubectl config get-contexts"

# The objects of the old install. The Deployments come first: their pods stop using the volumes.
NAMESPACED=(
  "deployment/$OLD-orchestrator" "deployment/$OLD-triage" "deployment/$OLD-knowledge" "deployment/$OLD-tempo"
  "service/$OLD-triage" "service/$OLD-knowledge" "service/$OLD-tempo"
  "networkpolicy/$OLD-orchestrator" "networkpolicy/$OLD-triage" "networkpolicy/$OLD-knowledge" "networkpolicy/$OLD-tempo"
  "serviceaccount/$OLD-orchestrator" "serviceaccount/$OLD-triage" "serviceaccount/$OLD-knowledge" "serviceaccount/$OLD-tempo"
  "configmap/$OLD-tempo" "secret/$OLD"
  "serviceaccount/$OLD-reader" "secret/$OLD-reader-token"
)
CLUSTER_WIDE=("clusterrolebinding/$OLD-reader-view" "clusterrolebinding/$OLD-reader-nodes" "clusterrole/$OLD-reader-nodes")
VOLUMES=("persistentvolumeclaim/$OLD-data" "persistentvolumeclaim/$OLD-knowledge-data" "persistentvolumeclaim/$OLD-models"
  "persistentvolumeclaim/$OLD-tempo-data")

# kubectl for one object: in the namespace of the settings, or for the whole cluster.
old_k() {  # WHERE ARGS... (WHERE: namespace or cluster)
  local where="$1"
  shift
  if [[ "$where" == cluster ]]; then
    kubectl --context "$K8S_CONTEXT" "$@"
  else
    k "$@"
  fi
}

# What is there, and whether it is ours. A failing kubectl stops the script: nothing is deleted
# half-seen.
found_namespaced=() found_cluster=() found_volumes=() foreign=()
look() {  # WHERE LIST_NAME OBJECT...
  local where="$1" object seen
  local -n into="$2"
  shift 2
  for object in "$@"; do
    seen="$(old_k "$where" get "${object%%/*}" "${object#*/}" --ignore-not-found \
      -o 'jsonpath={.metadata.name}{"|"}{.metadata.labels.app\.kubernetes\.io/part-of}')" \
      || die "kubectl could not look for $object (its own words are above). Nothing was deleted. Check the connection (VPN)."
    [[ -n "$seen" ]] || continue
    if [[ "${seen#*|}" == "$OLD" ]]; then
      into+=("$object")
    else
      foreign+=("$object (label: ${seen#*|})")
    fi
  done
}
look namespace found_namespaced "${NAMESPACED[@]}"
look cluster found_cluster "${CLUSTER_WIDE[@]}"
look namespace found_volumes "${VOLUMES[@]}"

# The new install must be there before the old one goes (only read).
new_orchestrator="$(k get deployment "$APP_NAME-orchestrator" --ignore-not-found \
  -o 'jsonpath={.metadata.name}{"|"}{.metadata.labels.app\.kubernetes\.io/part-of}')" \
  || die "kubectl could not look for deployment/$APP_NAME-orchestrator (its own words are above). Nothing was deleted."
new_there=no
[[ "$new_orchestrator" == "$APP_NAME-orchestrator|$APP_NAME" ]] && new_there=yes

# The KubeAI Model: kept. Only its label is read, to say whether kubeai.sh gave it the new one.
model_label=""
if [[ "$MODEL_BACKEND" == kubeai ]]; then
  model_label="$(kubeai_k get models.kubeai.org "$KUBEAI_MODEL" --ignore-not-found \
    -o 'jsonpath={.metadata.name}{"|"}{.metadata.labels.app\.kubernetes\.io/part-of}' 2>/dev/null)" || model_label="?|?"
fi

# The read-only kubeconfig on this laptop: it stops working when the old account goes, unless
# "wattson k8s add" made a new one (with the account wattson-reader).
reader_file="$(cli_config_dir)/kube/$K8S_CONTEXT.yaml"
old_reader_file=no
if [[ -f "$reader_file" ]] && grep -q "\"$OLD-reader\"" "$reader_file"; then old_reader_file=yes; fi

info "The old install ($OLD) on the context $K8S_CONTEXT:"
info "In the namespace $K8S_NAMESPACE:"
if [[ ${#found_namespaced[@]} -gt 0 ]]; then printf '  %s\n' "${found_namespaced[@]}"; else info "  nothing"; fi
info "Its volumes (the old task history, Grimoire login, model file and traces):"
if [[ ${#found_volumes[@]} -gt 0 ]]; then printf '  %s\n' "${found_volumes[@]}"; else info "  nothing"; fi
info "In the whole cluster (the old read-only access of \"k8s add\"):"
if [[ ${#found_cluster[@]} -gt 0 ]]; then printf '  %s\n' "${found_cluster[@]}"; else info "  nothing"; fi
if [[ "$MODEL_BACKEND" == kubeai ]]; then
  case "$model_label" in
    "") info "In the namespace $KUBEAI_NAMESPACE: nothing to delete (there is no Model $KUBEAI_MODEL)." ;;
    "$KUBEAI_MODEL|$OLD") info "In the namespace $KUBEAI_NAMESPACE: nothing to delete. The Model $KUBEAI_MODEL is kept, but it still has the label part-of=$OLD: run bash k8s/kubeai.sh --apply." ;;
    "$KUBEAI_MODEL|$APP_NAME") info "In the namespace $KUBEAI_NAMESPACE: nothing to delete. The Model $KUBEAI_MODEL is kept (label part-of=$APP_NAME)." ;;
    *) info "In the namespace $KUBEAI_NAMESPACE: nothing to delete. The Model $KUBEAI_MODEL is kept." ;;
  esac
fi
if [[ ${#foreign[@]} -gt 0 ]]; then
  info "Left alone (an old name, but not the label part-of=$OLD):"
  printf '  %s\n' "${foreign[@]}"
fi
if [[ "$new_there" != yes ]]; then
  info "NOT READY: the new install is not there (deployment/$APP_NAME-orchestrator). Deploy it first: bash k8s/deploy.sh"
fi
if [[ "$old_reader_file" == yes ]]; then
  info "NOTE: $reader_file still uses the old account $OLD-reader: run wattson k8s add $K8S_CONTEXT first, or the investigations lose their read-only access."
fi

total=$((${#found_namespaced[@]} + ${#found_cluster[@]} + ${#found_volumes[@]}))
if [[ "$APPLY" != yes ]]; then
  info ""
  if [[ "$total" -eq 0 ]]; then info "Nothing of the old install is there."; else info "Nothing was deleted. To delete it: bash k8s/remove-old-myagent.sh --apply"; fi
  exit 0
fi

# ---- --apply ----------------------------------------------------------------------------

if [[ "$total" -eq 0 ]]; then
  info ""
  info "Nothing of the old install is there. Nothing to delete."
  exit 0
fi
[[ "$new_there" == yes ]] || die "The new install is not there: nothing was deleted. Deploy it first (bash k8s/deploy.sh), check it (wattson status), then run this again."

# The etcd of this cluster sometimes answers too late: a delete is tried up to three times.
DELETE_RETRY_SECONDS="${DELETE_RETRY_SECONDS:-10}"
failed=0
delete_all() {  # WHERE OBJECT...
  local where="$1" object done_it try output
  shift
  for object in "$@"; do
    done_it=no
    for try in 1 2 3; do
      if output="$(old_k "$where" delete "${object%%/*}" "${object#*/}" --ignore-not-found --wait=false 2>&1)"; then
        done_it=yes
        break
      fi
      if [[ "$try" -lt 3 ]]; then sleep "$DELETE_RETRY_SECONDS"; fi
    done
    if [[ "$done_it" == yes ]]; then
      info "  deleted $object"
    else
      failed=$((failed + 1))
      info "  NOT deleted $object: $(tail -n 1 <<<"$output")"
    fi
  done
}

# ask_context QUESTION -> stops unless the name of the context is typed.
ask_context() {
  local answer=""
  printf 'Type "%s" %s: ' "$K8S_CONTEXT" "$1"
  IFS= read -r answer || true
  if [[ "$answer" != "$K8S_CONTEXT" ]]; then
    info ""
    info "Nothing was deleted."
    exit 1
  fi
}

asked=no volumes_kept=no
if [[ $((${#found_namespaced[@]} + ${#found_cluster[@]})) -gt 0 ]]; then
  info ""
  ask_context "to delete the objects above (not the volumes: you are asked about them next)"
  asked=yes
  delete_all namespace ${found_namespaced[@]+"${found_namespaced[@]}"}
  delete_all cluster ${found_cluster[@]+"${found_cluster[@]}"}
fi

if [[ ${#found_volumes[@]} -gt 0 ]]; then
  if [[ "$KEEP_DATA" == yes ]]; then
    volumes_kept=yes
  else
    info ""
    if [[ "$asked" == no ]]; then ask_context "to go on"; fi
    info "The old volumes hold the old task history, the old Grimoire login, the old model file and"
    info "the old traces. The new install does not use them. Their data cannot come back."
    printf 'Type "yes" to delete the old volumes too: '
    answer=""
    IFS= read -r answer || true
    if [[ "$answer" == yes ]]; then
      delete_all namespace "${found_volumes[@]}"
    else
      info ""
      volumes_kept=yes
    fi
  fi
fi

[[ "$failed" -eq 0 ]] || die "$failed object(s) could not be deleted (see above). Run the script again."
info ""
if [[ "$volumes_kept" == yes ]]; then
  info "Done. The old objects are deleted; the old volumes are kept. To delete them later:"
  info "  kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE delete ${found_volumes[*]}"
else
  info "Done. The old install is removed from the cluster."
fi
info "Left for you: the laptop folders of the old name (~/.config/$OLD, ~/.local/state/$OLD); delete them when all works."
