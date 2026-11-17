#!/usr/bin/env bash
# Installs KubeAI and the KubeAI Model of Granite, for MODEL_BACKEND=kubeai (settings.env), and
# the network rules of KubeAI's namespace (k8s/kubeai/networkpolicy.yaml: only the triage and
# orchestrator pods may ask the proxy, only the proxy may ask the model pods). Without --apply
# it only prints what it would install and the commands; it runs neither helm nor kubectl.
#
#   bash k8s/kubeai.sh            print the plan: the filled-in Helm values, the Model, the network
#                                 rules, the commands
#   bash k8s/kubeai.sh --apply    the same, then you type the name of the context, then it runs
#                                 helm upgrade --install (the pinned chart) and applies the Model
#                                 and the network rules
#
# ON A SHARED CLUSTER: the KubeAI chart installs, for the WHOLE cluster, the
# CustomResourceDefinition models.kubeai.org and the ClusterRole "kubeai"; and in its own
# namespace (KUBEAI_NAMESPACE, which helm makes) an operator that is also an OpenAI proxy, and
# the model pod (4 CPU reserved, the llama.cpp image LLAMA_IMAGE). See k8s/kubeai/values.yaml.
# k8s/deploy.sh never installs KubeAI: run this first, then set MODEL_BACKEND=kubeai and deploy.
#
# The Model runs on KubeAI's llama.cpp engine (since 23 Oct 2026; before: Ollama). When the
# Model on the cluster has another engine, --apply deletes it first, before the chart: else
# KubeAI would start its old pod again with the new values, next to the new pod (4 more CPU).
# Until the new pod is ready, the triage agent answers with its keyword rules.
#
# To remove it later (first MODEL_BACKEND=sidecar and bash k8s/deploy.sh):
#   helm --kube-context <context> -n <KUBEAI_NAMESPACE> uninstall kubeai
#   kubectl --context <context> -n <KUBEAI_NAMESPACE> delete networkpolicy -l app.kubernetes.io/part-of=<APP_NAME>
# The first deletes the CRD too (it is a template of the chart), and with it every Model.
#
# KUBEAI_WAIT_SECONDS=1800 bash k8s/kubeai.sh --apply    to wait longer for the model pod (the
#                                                       first start on a node pulls the image too)
set -euo pipefail

# shellcheck source=k8s/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

APPLY=no
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=yes ;;
    -h | --help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "Unknown option: $arg. See: bash k8s/kubeai.sh --help" ;;
  esac
done

read_settings
check_settings
[[ -n "$KUBEAI_BASE_URL" ]] || die "KUBEAI_BASE_URL is empty in settings.env."
# The model of KubeAI's llama.cpp: the Hugging Face repo and file of MODEL_URL (lib.sh).
[[ -n "$MODEL_HF_REPO" ]] \
  || die "MODEL_URL must be https://huggingface.co/<user>/<repo>/resolve/<commit>/$MODEL_FILE: KubeAI's llama.cpp downloads the model from Hugging Face. It is: \"$MODEL_URL\""

# The chart, pinned: its version, and the SHA-256 of its file (from the index of the repo,
# 7 Oct 2026). KUBEAI_CHART_SHA256 in the environment replaces it (the tests use a stand-in).
CHART_REPO=https://www.kubeai.org
CHART_NAME=kubeai
CHART_VERSION=0.24.0
CHART_SHA256="${KUBEAI_CHART_SHA256:-701c23ffe4002a471e4d18db4c47561179be05540ade850e4e67222223067ce6}"
# The release name: the chart then names its proxy Service "kubeai" (fullnameOverride).
RELEASE=kubeai
# The names filled in in k8s/kubeai/*.yaml. tests/k8s/test-k8s-files.sh reads this line.
# shellcheck disable=SC2016  # the ${...} names are for envsubst, not for the shell
KUBEAI_YAML_NAMES='${LLAMA_IMAGE} ${APP_NAME} ${KUBEAI_MODEL} ${KUBEAI_MIN_REPLICAS} ${KUBEAI_SCALE_DOWN_DELAY_SECONDS} ${MODEL_HF_REPO} ${MODEL_FILE} ${LLAMA_CTX} ${LLAMA_THREADS} ${LLAMA_BATCH_THREADS} ${K8S_NAMESPACE} ${TRIAGE} ${ORCHESTRATOR} ${KUBEAI_PROXY_PORT}'

command -v envsubst >/dev/null 2>&1 || die "envsubst is not installed (package gettext-base)."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
envsubst "$KUBEAI_YAML_NAMES" <"$HERE/kubeai/values.yaml" >"$WORK/values.yaml"
envsubst "$KUBEAI_YAML_NAMES" <"$HERE/kubeai/granite-model.yaml" >"$WORK/granite-model.yaml"
envsubst "$KUBEAI_YAML_NAMES" <"$HERE/kubeai/networkpolicy.yaml" >"$WORK/networkpolicy.yaml"
# The engine of the Model above (LlamaCpp).
NEW_ENGINE="$(sed -n 's/^  engine: *\([A-Za-z]*\) *$/\1/p' "$WORK/granite-model.yaml")"
[[ -n "$NEW_ENGINE" ]] || die "k8s/kubeai/granite-model.yaml names no engine."

info "# KubeAI for $APP_NAME: context $K8S_CONTEXT, namespace $KUBEAI_NAMESPACE."
info "# The chart $CHART_NAME $CHART_VERSION ($CHART_REPO) installs, for the WHOLE cluster, the CRD"
info "# models.kubeai.org and the ClusterRole kubeai; in $KUBEAI_NAMESPACE (made when missing) the"
info "# operator and proxy (Service $RELEASE) and the model pod. The Helm values, filled in:"
info "---"
cat "$WORK/values.yaml"
info "---"
info "# The Model, filled in:"
cat "$WORK/granite-model.yaml"
info "---"
info "# The network rules of $KUBEAI_NAMESPACE, filled in (${KUBEAI_POLICIES[*]}):"
cat "$WORK/networkpolicy.yaml"
info "---"
info "# The commands:"
info "#   helm pull $CHART_NAME --repo $CHART_REPO --version $CHART_VERSION   (then its SHA-256 must be $CHART_SHA256)"
info "#   kubectl --context $K8S_CONTEXT -n $KUBEAI_NAMESPACE delete model $KUBEAI_MODEL   (only when it has another engine than $NEW_ENGINE)"
info "#   helm upgrade --install $RELEASE <that chart> --kube-context $K8S_CONTEXT --namespace $KUBEAI_NAMESPACE --create-namespace --values <the values above> --wait --timeout 10m"
info "#   kubectl --context $K8S_CONTEXT -n $KUBEAI_NAMESPACE apply -f <the Model above>"
info "#   kubectl --context $K8S_CONTEXT -n $KUBEAI_NAMESPACE apply -f <the network rules above>"

if [[ "$APPLY" != yes ]]; then
  info ""
  info "Nothing was run. To install: bash k8s/kubeai.sh --apply"
  exit 0
fi

# ---- --apply ----------------------------------------------------------------------------

for tool in helm kubectl sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed."
done
seen_context="$(kubectl --context "$K8S_CONTEXT" config view --minify -o 'jsonpath={.contexts[0].name}' 2>/dev/null || true)"
[[ "$seen_context" == "$K8S_CONTEXT" ]] || die "kubectl has no context \"$K8S_CONTEXT\". See: kubectl config get-contexts"

# Another KubeAI on this cluster (another release or namespace) owns the CRD: a second one
# would fight it. Only to read.
crd_owner="$(kubectl --context "$K8S_CONTEXT" get crd models.kubeai.org --ignore-not-found \
  -o 'jsonpath={.metadata.annotations.meta\.helm\.sh/release-namespace}/{.metadata.annotations.meta\.helm\.sh/release-name}')" \
  || die "kubectl could not look for the CRD models.kubeai.org (its own words are above). Nothing was changed. Check the connection (VPN)."
if [[ -n "$crd_owner" && "$crd_owner" != "$KUBEAI_NAMESPACE/$RELEASE" ]]; then
  die "The cluster has the CRD models.kubeai.org already, from \"$crd_owner\" (namespace/release): another KubeAI runs there. Nothing was changed. Use that one (KUBEAI_NAMESPACE, KUBEAI_BASE_URL) or ask its owner."
fi
# The engine of the Model on the cluster (empty: no Model yet). Only to read.
old_engine=""
if [[ -n "$crd_owner" ]]; then
  old_engine="$(kubeai_k get models.kubeai.org "$KUBEAI_MODEL" --ignore-not-found -o 'jsonpath={.spec.engine}')" \
    || die "kubectl could not read the Model $KUBEAI_MODEL (its own words are above). Nothing was changed. Check the connection (VPN)."
fi
if [[ -n "$old_engine" && "$old_engine" != "$NEW_ENGINE" ]]; then
  info ""
  info "The Model $KUBEAI_MODEL runs on $old_engine now: it is deleted first (its pod stops), then made again on $NEW_ENGINE."
  info "Until the new pod is ready the triage agent answers with its keyword rules."
fi

info ""
printf 'Type "%s" to install KubeAI on that cluster: ' "$K8S_CONTEXT"
answer=""
IFS= read -r answer || true
if [[ "$answer" != "$K8S_CONTEXT" ]]; then
  info ""
  info "Nothing was installed."
  exit 1
fi

helm pull "$CHART_NAME" --repo "$CHART_REPO" --version "$CHART_VERSION" --destination "$WORK" >/dev/null \
  || die "Could not download the chart $CHART_NAME $CHART_VERSION from $CHART_REPO. Nothing was installed."
CHART_FILE="$WORK/$CHART_NAME-$CHART_VERSION.tgz"
[[ -f "$CHART_FILE" ]] || die "helm pull made no $CHART_NAME-$CHART_VERSION.tgz. Nothing was installed."
seen_sha256="$(sha256sum "$CHART_FILE" | cut -c1-64)"
[[ "$seen_sha256" == "$CHART_SHA256" ]] \
  || die "The chart file has the SHA-256 $seen_sha256, not the pinned $CHART_SHA256. Nothing was installed."
info "Chart: $CHART_NAME $CHART_VERSION downloaded, SHA-256 checked."

if [[ -n "$old_engine" && "$old_engine" != "$NEW_ENGINE" ]]; then
  info "Model: deleting $KUBEAI_MODEL ($old_engine)."
  kubeai_k delete models.kubeai.org "$KUBEAI_MODEL" --wait=true --timeout=5m | sed 's/^/  /' \
    || die "The Model $KUBEAI_MODEL ($old_engine) could not be deleted (the words of kubectl are above). The chart was not changed."
fi

info "Helm: installing (it waits for the operator, up to 10 minutes)."
helm upgrade --install "$RELEASE" "$CHART_FILE" --kube-context "$K8S_CONTEXT" --namespace "$KUBEAI_NAMESPACE" \
  --create-namespace --values "$WORK/values.yaml" --wait --timeout 10m \
  || die "helm could not install KubeAI (its words are above). See: helm --kube-context $K8S_CONTEXT -n $KUBEAI_NAMESPACE status $RELEASE"

info "Model: applying $KUBEAI_MODEL."
kubeai_k apply -f - <"$WORK/granite-model.yaml" | sed 's/^/  /' \
  || die "The Model could not be applied (the words of kubectl are above)."

info "Network rules: applying ${KUBEAI_POLICIES[*]} (only the triage and orchestrator pods may ask KubeAI)."
kubeai_k apply -f - <"$WORK/networkpolicy.yaml" | sed 's/^/  /' \
  || die "The network rules could not be applied (the words of kubectl are above). Run this again."

if [[ "$KUBEAI_MIN_REPLICAS" -ge 1 ]]; then
  WAIT="${KUBEAI_WAIT_SECONDS:-900}"
  [[ "$WAIT" =~ ^[1-9][0-9]{0,4}$ ]] || die "KUBEAI_WAIT_SECONDS must be a whole number of seconds."
  info "Waiting for the model pod: it pulls the llama.cpp image (the first time on a node) and downloads the model (0.9 GB). Up to $((WAIT / 60)) minutes."
  kubeai_k wait "model/$KUBEAI_MODEL" --for=jsonpath='{.status.replicas.ready}'=1 --timeout="${WAIT}s" >/dev/null \
    || die "KubeAI is installed, but the model pod is not ready after ${WAIT}s. Look at it: kubectl --context $K8S_CONTEXT -n $KUBEAI_NAMESPACE get pods (and describe pod)"
  info "  $KUBEAI_MODEL: ready."
fi

info ""
if [[ "$MODEL_BACKEND" == kubeai ]]; then
  info "Done. The triage pod already asks KubeAI (MODEL_BACKEND=kubeai): no deploy is needed."
else
  info "Done. Next: set MODEL_BACKEND=kubeai in settings.env, commit, and run bash k8s/deploy.sh."
fi
info "Check any time: bash k8s/doctor.sh"
