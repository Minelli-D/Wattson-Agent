#!/usr/bin/env bash
# Puts wattson on Kubernetes: three agents (orchestrator, triage with the small model,
# knowledge), their volumes, two Services, the network rules, and the Secret with the three
# tokens. Then it writes the settings of the CLI on this laptop:
#   ~/.config/wattson/config.json   where the orchestrator is (context, namespace, deployment, port)
#   ~/.config/wattson/token         the CLI's token (mode 600), copied from the Secret
# Everything it needs to know is in settings.env. Safe to run again: it changes only what is
# different, and the Secret is made once and never changed.
# With MODEL_BACKEND=kubeai (settings.env) KubeAI serves the model instead, and the triage pod
# has no llama container: install KubeAI first (bash k8s/kubeai.sh --apply). This script only
# checks that its proxy and the Model are there; it never installs KubeAI.
# With TELEMETRY=on (settings.env) it also puts Tempo there, the store of the traces, and gives
# the agents its address. The Grafana side is k8s/grafana.sh (run it after this one).
# The rules of the Guard (k8s/guard/rules.yaml, k8s/guard/policies.cedar) are checked first, then
# go into the ConfigMap <APP_NAME>-guard. To change only them later: bash k8s/guard.sh --apply.
# With INJECTION_MODE warn or cut (settings.env) it also builds the shield's image
# (Dockerfile.shield) and puts the shield there: the small prompt-injection model, with its token
# in the Secret <APP_NAME>-shield (made once, never changed).
#
#   bash k8s/deploy.sh                 build, push, apply, wait, write the CLI settings
#   bash k8s/deploy.sh --dry-run       print the filled-in Kubernetes files; nothing else runs
#   bash k8s/deploy.sh --allow-dirty   deploy files that are not committed yet (the image tag
#                                      then says "dirty")
#
# It never prints a secret.
#
# BUILD_NETWORK=host bash k8s/deploy.sh    when "docker build" on this machine cannot look up
#                                          names (the Wi-Fi or a VPN): the build then uses the
#                                          network of this machine
# ROLLOUT_WAIT_SECONDS=1800 bash k8s/deploy.sh    to wait longer for the pods (the first start
#                                          downloads the model: up to 15 minutes by default)
set -euo pipefail

# shellcheck source=k8s/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

DRY_RUN=no
ALLOW_DIRTY=no
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=yes ;;
    --allow-dirty) ALLOW_DIRTY=yes ;;
    -h | --help)
      sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "Unknown option: $arg. See: bash k8s/deploy.sh --help" ;;
  esac
done

read_settings
check_settings
# The rules of the Guard: a wrong file stops the script here, before anything runs.
check_guard_files

# The names that are filled in in k8s/wattson.yaml. tests/k8s/test-k8s-files.sh reads this line.
# shellcheck disable=SC2016  # the ${...} names are for envsubst, not for the shell
YAML_NAMES='${APP_NAME} ${ORCHESTRATOR} ${TRIAGE} ${KNOWLEDGE} ${IMAGE} ${PULL_SECRET} ${STORAGE_CLASS} ${DATA_SIZE} ${KNOWLEDGE_DATA_SIZE} ${MODELS_SIZE} ${GRIMOIRE_MCP_URL} ${LLAMA_IMAGE} ${FETCH_IMAGE} ${MODEL_FILE} ${MODEL_URL} ${MODEL_SHA256} ${MODEL_NAME} ${LLAMA_CTX} ${LLAMA_THREADS} ${LLAMA_BATCH_THREADS} ${MIN_CONFIDENCE} ${HISTORY_RETENTION_DAYS} ${TASK_TTL_MINUTES} ${NODE_LOCAL_DNS} ${ORCHESTRATOR_PORT} ${TRIAGE_PORT} ${KNOWLEDGE_PORT} ${LLAMA_PORT} ${ORCHESTRATOR_REQ_CPU} ${ORCHESTRATOR_REQ_MEMORY} ${ORCHESTRATOR_LIM_CPU} ${ORCHESTRATOR_LIM_MEMORY} ${TRIAGE_REQ_CPU} ${TRIAGE_REQ_MEMORY} ${TRIAGE_LIM_CPU} ${TRIAGE_LIM_MEMORY} ${KNOWLEDGE_REQ_CPU} ${KNOWLEDGE_REQ_MEMORY} ${KNOWLEDGE_LIM_CPU} ${KNOWLEDGE_LIM_MEMORY} ${LLAMA_REQ_CPU} ${LLAMA_REQ_MEMORY} ${LLAMA_LIM_CPU} ${LLAMA_LIM_MEMORY} ${FETCH_REQ_CPU} ${FETCH_REQ_MEMORY} ${FETCH_LIM_CPU} ${FETCH_LIM_MEMORY} ${LLM_BASE_URL} ${LLM_MODEL} ${KUBEAI_NAMESPACE} ${KUBEAI_PROXY_PORT} ${TEMPO} ${TEMPO_IMAGE} ${TEMPO_SIZE} ${TEMPO_RETENTION} ${TEMPO_OTLP_PORT} ${TEMPO_HTTP_PORT} ${OTLP_ENDPOINT} ${GRAFANA_NAMESPACE} ${TEMPO_REQ_CPU} ${TEMPO_REQ_MEMORY} ${TEMPO_LIM_CPU} ${TEMPO_LIM_MEMORY} ${TEMPO_CONFIG_SHA256} ${GUARD_MODE} ${GUARD_TIMEOUT_S} ${GUARD_MODEL_URL} ${GUARD_MODEL} ${GUARD_CONFIG_SHA256} ${SHIELD} ${SHIELD_IMAGE} ${SHIELD_SECRET} ${SHIELD_PORT} ${SHIELD_THREADS} ${SHIELD_REQ_CPU} ${SHIELD_REQ_MEMORY} ${SHIELD_LIM_CPU} ${SHIELD_LIM_MEMORY} ${INJECTION_MODE} ${INJECTION_MODEL_REPO} ${INJECTION_BASE_URL} ${INJECTION_MODEL_FILE} ${INJECTION_MODEL_SHA256} ${INJECTION_TOKENIZER_FILE} ${INJECTION_TOKENIZER_SHA256} ${INJECTION_CONFIG_FILE} ${INJECTION_CONFIG_SHA256} ${INJECTION_LABEL} ${INJECTION_THRESHOLD} ${INJECTION_WINDOW} ${INJECTION_TIMEOUT_S} ${COGNEE} ${COGNEE_PORT} ${COGNEE_RECALL} ${COGNEE_RECALL_URL} ${COGNEE_RECALL_DATASET} ${COGNEE_RECALL_LESSONS} ${COGNEE_RECALL_TIMEOUT_S}'

# The files that make the image, and the files that say what is deployed. A deploy is made
# from a commit: when one of these is changed and not committed, the script stops (unless
# --allow-dirty).
IMAGE_FILES=(Dockerfile Dockerfile.shield .dockerignore pyproject.toml uv.lock src)
DEPLOY_FILES=("${IMAGE_FILES[@]}" k8s settings.env)
# The files of the shield's image. Its tag is a hash of their content, not the commit: a deploy
# that does not change them keeps the same image, so the shield pod (and its download of the
# model) is left alone.
SHIELD_IMAGE_FILES=(Dockerfile.shield .dockerignore pyproject.toml uv.lock src/wattson/shield/server.py)

# ---- the Kubernetes files ------------------------------------------------------------
# tests/k8s/test-k8s-files.sh uses these functions as they are.

# A line that ends with "# optional: NAME" is left out when the setting NAME is empty.
# When it is set, only the mark goes.
# The lines from "# begin: sidecar" to "# end: sidecar" are only for MODEL_BACKEND=sidecar, and
# those from "# begin: kubeai" to "# end: kubeai" only for kubeai. The lines from
# "# begin: telemetry" to "# end: telemetry" are only for TELEMETRY=on, and those from
# "# begin: shield" to "# end: shield" only for INJECTION_MODE warn or cut, and those from
# "# begin: cognee" to "# end: cognee" only for COGNEE_RECALL=on (the knowledge agent's settings
# and way out to the pod of k8s/cognee.sh, never that pod). The marks always go.
with_options() {
  awk -v pull="$PULL_SECRET" -v dns="$NODE_LOCAL_DNS" -v backend="$MODEL_BACKEND" -v telemetry="$TELEMETRY" -v injection="$INJECTION_MODE" -v recall="$COGNEE_RECALL" '
    $1 == "#" && $2 == "begin:" && ($3 == "sidecar" || $3 == "kubeai") { skip_backend = ($3 != backend); next }
    $1 == "#" && $2 == "end:" && ($3 == "sidecar" || $3 == "kubeai") { skip_backend = 0; next }
    $1 == "#" && $2 == "begin:" && $3 == "telemetry" { skip_telemetry = (telemetry != "on"); next }
    $1 == "#" && $2 == "end:" && $3 == "telemetry" { skip_telemetry = 0; next }
    $1 == "#" && $2 == "begin:" && $3 == "shield" { skip_shield = (injection == "off"); next }
    $1 == "#" && $2 == "end:" && $3 == "shield" { skip_shield = 0; next }
    $1 == "#" && $2 == "begin:" && $3 == "cognee" { skip_cognee = (recall != "on"); next }
    $1 == "#" && $2 == "end:" && $3 == "cognee" { skip_cognee = 0; next }
    skip_backend || skip_telemetry || skip_shield || skip_cognee { next }
    /# optional: PULL_SECRET$/ { if (pull == "") next }
    /# optional: NODE_LOCAL_DNS$/ { if (dns == "") next }
    { sub(/ +# optional: [A-Z_]+$/, ""); print }'
}

# The config of Tempo in the filled-in files, as its ConfigMap holds it: the lines under
# "tempo.yaml: |", without their indent.
tempo_config_of() {  # the filled-in files on standard input
  awk '/^  tempo\.yaml: \|$/ { on = 1; next } /^[^ ]/ { on = 0 } on { sub(/^    /, ""); print }'
}

# The line "# guard: the ConfigMap" of the filled-in files becomes the ConfigMap of the rules of
# the Guard (guard_configmap, lib.sh). After envsubst: a "${...}" in a rule stays as it is.
with_guard_configmap() {  # the filled-in files on standard input
  local doc
  doc="$(guard_configmap)" || return 1
  GUARD_CONFIGMAP_DOC="$doc" awk '$0 == "# guard: the ConfigMap" { print ENVIRON["GUARD_CONFIGMAP_DOC"]; next } { print }'
}

# k8s/wattson.yaml, filled in. IMAGE must be set (and SHIELD_IMAGE, the shield's, with
# INJECTION_MODE warn or cut). The pod of Tempo carries the SHA-256 of its
# config (TEMPO_CONFIG_SHA256), so a change of the config starts a new pod: the files are
# filled in once to read the config, then again with its SHA-256. The pod of the orchestrator
# carries GUARD_CONFIG_SHA256 (check_guard_files), the same way.
filled_yaml() {
  local rendered
  TEMPO_CONFIG_SHA256=""
  SHIELD_IMAGE="${SHIELD_IMAGE:-}"
  export TEMPO_CONFIG_SHA256 SHIELD_IMAGE
  rendered="$(with_options <"$HERE/wattson.yaml" | envsubst "$YAML_NAMES")"
  if [[ "$TELEMETRY" == on ]]; then
    TEMPO_CONFIG_SHA256="$(tempo_config_of <<<"$rendered" | sha256sum | cut -c1-64)"
    rendered="$(with_options <"$HERE/wattson.yaml" | envsubst "$YAML_NAMES")"
  fi
  with_guard_configmap <<<"$rendered"
}

# ---- the image tag: the commit ----------------------------------------------------

# The short hash of the commit, or nothing when there is no commit.
git_commit() {
  git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || true
}

# The deploy files that are changed, new or deleted, compared with the commit. One per line.
dirty_files() {
  git -C "$ROOT" status --porcelain --untracked-files=all -- "${DEPLOY_FILES[@]}" 2>/dev/null || true
}

# A short hash of the content of the image files as they are now (for a dirty tag).
content_hash() {
  (cd "$ROOT" && git ls-files -z --cached --others --exclude-standard -- "${IMAGE_FILES[@]}" \
    | LC_ALL=C sort -z | xargs -0 -r sha256sum 2>/dev/null) | sha256sum | cut -c1-8
}

shield_tag() {
  (cd "$ROOT" && sha256sum "${SHIELD_IMAGE_FILES[@]}") | sha256sum | cut -c1-12
}

image_tag() {
  local commit
  commit="$(git_commit)"
  [[ -n "$commit" ]] || commit="nocommit"
  if [[ -n "$(dirty_files)" ]]; then
    printf '%s-dirty-%s' "$commit" "$(content_hash)"
  else
    printf '%s' "$commit"
  fi
}

# The script stops here when another script only wants the functions above.
# shellcheck disable=SC2317  # "exit" is for a run, "return" for a script that sources this one
if [[ "${DEPLOY_FUNCTIONS_ONLY:-}" == yes ]]; then return 0 2>/dev/null || exit 0; fi

# ---- --dry-run: print the files, run nothing ---------------------------------------

if [[ "$DRY_RUN" == yes ]]; then
  command -v envsubst >/dev/null 2>&1 || die "envsubst is not installed (package gettext-base)."
  TAG="$(image_tag)"
  # The digest is known only after the push: here it is a stand-in of zeros.
  IMAGE="$REGISTRY/$APP_NAME:$TAG@sha256:$(printf '0%.0s' {1..64})"
  SHIELD_IMAGE="$REGISTRY/$SHIELD:$(shield_tag)@sha256:$(printf '0%.0s' {1..64})"
  export IMAGE SHIELD_IMAGE
  info "# DRY RUN: nothing was built, pushed or applied. The image digests below are stand-ins."
  info "# Context $K8S_CONTEXT, namespace $K8S_NAMESPACE. The Secret $APP_NAME (three tokens) is"
  info "# made by the real run, once; it is not shown here$([[ "$INJECTION_MODE" == off ]] || printf ' (nor the Secret %s)' "$SHIELD_SECRET")."
  filled_yaml
  exit 0
fi

# ---- checks before starting ---------------------------------------------------------

for tool in kubectl docker git envsubst python3 curl base64; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed."
done
[[ -f "$ROOT/Dockerfile" ]] || die "The Dockerfile was not found in $ROOT."
[[ "$INJECTION_MODE" == off || -f "$ROOT/Dockerfile.shield" ]] || die "Dockerfile.shield (the shield's image) was not found in $ROOT."

COMMIT="$(git_commit)"
[[ -n "$COMMIT" ]] || die "$ROOT has no commit yet. The image is tagged with the commit: commit first."
DIRTY="$(dirty_files)"
if [[ -n "$DIRTY" && "$ALLOW_DIRTY" != yes ]]; then
  die "These files are changed and not committed: $(awk '{print $NF}' <<<"$DIRTY" | head -10 | paste -sd ' ' -). Commit them first, or run with --allow-dirty (the image tag then says \"dirty\")."
fi
TAG="$(image_tag)"

# The context must be in the kubeconfig. (This call reads only the local kubeconfig.)
seen_context="$(kubectl --context "$K8S_CONTEXT" config view --minify -o 'jsonpath={.contexts[0].name}' 2>/dev/null || true)"
[[ "$seen_context" == "$K8S_CONTEXT" ]] || die "kubectl has no context \"$K8S_CONTEXT\" (the setting K8S_CONTEXT). See: kubectl config get-contexts"

# "It is not there" and "I could not look" are two answers. Only the first may lead to making
# something, so a failing kubectl (no connection, no right) stops the script.
could_not_look() {  # KIND NAME
  die "kubectl could not look for the $1 \"$2\" in the namespace \"$K8S_NAMESPACE\" (its own words are above). Nothing was changed. Check the connection (VPN) and run again."
}
have() {  # KIND NAME -> true when the object is there, false when the cluster says it is not
  local found
  found="$(k get "$1" "$2" --ignore-not-found -o name)" || could_not_look "$1" "$2"
  [[ -n "$found" ]]
}

# The namespace is never made here: it is shared with other apps.
ns_found="$(k get namespace "$K8S_NAMESPACE" --ignore-not-found -o name 2>/dev/null)" \
  || die "kubectl cannot reach the cluster of \"$K8S_CONTEXT\". Check the connection (VPN) and run again."
[[ -n "$ns_found" ]] || die "The namespace \"$K8S_NAMESPACE\" does not exist in \"$K8S_CONTEXT\". This script never makes a namespace: ask for it, or pick another one in settings.env (K8S_NAMESPACE)."
have storageclass "$STORAGE_CLASS" || die "The storage class \"$STORAGE_CLASS\" does not exist (the setting STORAGE_CLASS). See: kubectl --context $K8S_CONTEXT get storageclass"
if [[ -n "$PULL_SECRET" ]]; then
  have secret "$PULL_SECRET" || die "The pull secret \"$PULL_SECRET\" is not in the namespace \"$K8S_NAMESPACE\" (the setting PULL_SECRET). Without it the cluster cannot pull the image."
fi

# An object with one of our names that this script did not make is never changed. Everything
# this script makes carries the label app.kubernetes.io/part-of=<APP_NAME>.
not_ours() {  # KIND NAME -> true when the object is there and has not that label
  local seen
  seen="$(k get "$1" "$2" --ignore-not-found -o 'jsonpath={.metadata.name}{"|"}{.metadata.labels.app\.kubernetes\.io/part-of}')" \
    || could_not_look "$1" "$2"
  [[ -n "$seen" && "${seen#*|}" != "$APP_NAME" ]]
}
OBJECTS=(
  "deployment/$ORCHESTRATOR" "deployment/$TRIAGE" "deployment/$KNOWLEDGE"
  "service/$TRIAGE" "service/$KNOWLEDGE"
  "networkpolicy/$ORCHESTRATOR" "networkpolicy/$TRIAGE" "networkpolicy/$KNOWLEDGE"
  "serviceaccount/$ORCHESTRATOR" "serviceaccount/$TRIAGE" "serviceaccount/$KNOWLEDGE"
  "persistentvolumeclaim/$APP_NAME-data" "persistentvolumeclaim/$APP_NAME-knowledge-data" "persistentvolumeclaim/$APP_NAME-models"
  "configmap/$GUARD_CONFIGMAP" "secret/$APP_NAME"
)
TEMPO_OBJECTS=(
  "deployment/$TEMPO" "service/$TEMPO" "networkpolicy/$TEMPO" "serviceaccount/$TEMPO" "configmap/$TEMPO"
  "persistentvolumeclaim/$APP_NAME-tempo-data"
)
if [[ "$TELEMETRY" == on ]]; then OBJECTS+=("${TEMPO_OBJECTS[@]}"); fi
SHIELD_OBJECTS=(
  "deployment/$SHIELD" "service/$SHIELD" "networkpolicy/$SHIELD" "serviceaccount/$SHIELD" "secret/$SHIELD_SECRET"
)
if [[ "$INJECTION_MODE" != off ]]; then OBJECTS+=("${SHIELD_OBJECTS[@]}"); fi
foreign=()
for object in "${OBJECTS[@]}"; do
  if not_ours "${object%%/*}" "${object#*/}"; then foreign+=("$object"); fi
done
[[ ${#foreign[@]} -eq 0 ]] || die "These objects are in the namespace \"$K8S_NAMESPACE\" and were not made by this script: ${foreign[*]}. Nothing was changed. Rename the app (APP_NAME) or remove them yourself."
# The orchestrator must never get a Service: the CLI reaches it with port-forward only.
if have service "$ORCHESTRATOR"; then
  die "There is a Service $ORCHESTRATOR. The orchestrator must have none (docs/design.md 10.3). Remove it first: kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE delete service $ORCHESTRATOR"
fi

# The Secret of an earlier run is kept. It must have the three keys (names only are read).
have_secret=no
if have secret "$APP_NAME"; then
  have_secret=yes
  stored_keys="$(secret_keys "$APP_NAME")" || could_not_look secret "$APP_NAME"
  for key in "${SECRET_KEYS[@]}"; do
    grep -qx "$key" <<<"$stored_keys" \
      || die "The Secret $APP_NAME has no $key. It is never changed by this script. Delete it (kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE delete secret $APP_NAME) and run again: three new tokens are made."
  done
fi

# The Secret of the shield (INJECTION_MODE warn or cut), the same way: kept, and it must have
# its key. The Secret $APP_NAME is never touched for it.
have_shield_secret=no
if [[ "$INJECTION_MODE" != off ]] && have secret "$SHIELD_SECRET"; then
  have_shield_secret=yes
  stored_keys="$(secret_keys "$SHIELD_SECRET")" || could_not_look secret "$SHIELD_SECRET"
  for key in "${SHIELD_SECRET_KEYS[@]}"; do
    grep -qx "$key" <<<"$stored_keys" \
      || die "The Secret $SHIELD_SECRET has no $key. It is never changed by this script. Delete it (kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE delete secret $SHIELD_SECRET) and run again: a new token is made."
  done
fi

# With MODEL_BACKEND=kubeai: KubeAI's proxy Service and the Model must be there. Only read
# here; k8s/kubeai.sh installs them.
if [[ "$MODEL_BACKEND" == kubeai ]]; then
  kubeai_missing() {  # WHAT
    die "MODEL_BACKEND is kubeai, and $1 is not in the namespace \"$KUBEAI_NAMESPACE\" of \"$K8S_CONTEXT\". Install KubeAI first: bash k8s/kubeai.sh --apply (or set MODEL_BACKEND=sidecar). Nothing was changed."
  }
  kubeai_have() {  # KIND NAME -> true when it is there; stops when the cluster cannot be asked
    local found error error_file
    error_file="$(mktemp)"
    found="$(kubeai_k get "$1" "$2" --ignore-not-found -o name 2>"$error_file")" || found="FAILED"
    error="$(<"$error_file")"
    rm -f "$error_file"
    if [[ "$found" == FAILED ]]; then
      # No such kind: the CRD of KubeAI is not installed.
      [[ "$error" == *"the server doesn't have a resource type"* ]] && return 1
      printf '%s\n' "$error" >&2
      die "kubectl could not look for the $1 \"$2\" in the namespace \"$KUBEAI_NAMESPACE\" (its own words are above). Nothing was changed. Check the connection (VPN) and run again."
    fi
    [[ -n "$found" ]]
  }
  kubeai_have service "$KUBEAI_SERVICE" || kubeai_missing "the KubeAI proxy (Service $KUBEAI_SERVICE)"
  kubeai_have models.kubeai.org "$KUBEAI_MODEL" || kubeai_missing "the KubeAI Model $KUBEAI_MODEL"
fi

# The first start downloads the model (about 0.9 GB): it may take much longer. Not with
# KubeAI: the triage pod then downloads nothing.
first_start=no
if [[ "$MODEL_BACKEND" == sidecar ]] && { ! have deployment "$TRIAGE" || ! have persistentvolumeclaim "$APP_NAME-models"; }; then first_start=yes; fi

# The registry: we must be logged in, and it must answer.
REGISTRY_HOST="${REGISTRY%%/*}"
docker_logged_in() {  # HOST -> true when the docker settings have a login for it (names only are read)
  python3 - "${DOCKER_CONFIG:-$HOME/.docker}/config.json" "$1" <<'PY'
import json, sys
path, host = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as file:
        config = json.load(file)
except (OSError, ValueError):
    sys.exit(1)
names = set(config.get("auths", {})) | set(config.get("credHelpers", {}))
sys.exit(0 if any(name.split("://")[-1].rstrip("/") == host for name in names) else 1)
PY
}
docker_logged_in "$REGISTRY_HOST" || die "Docker is not logged in to $REGISTRY_HOST. Run: docker login $REGISTRY_HOST"
registry_answer="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$REGISTRY_HOST/v2/" 2>/dev/null || true)"
[[ "$registry_answer" == 200 || "$registry_answer" == 401 ]] \
  || die "The registry https://$REGISTRY_HOST does not answer (got \"$registry_answer\"). Check the connection (VPN) and run again."

info "Cluster $K8S_CONTEXT, namespace $K8S_NAMESPACE, app $APP_NAME, image tag $TAG"
if [[ "$MODEL_BACKEND" == kubeai ]]; then
  info "Model: KubeAI serves $KUBEAI_MODEL ($KUBEAI_BASE_URL); the triage pod has no llama container."
fi
if [[ "$TELEMETRY" == on ]]; then
  info "Traces: on. Tempo ($TEMPO, ${TEMPO_IMAGE%%@*}) keeps them for $TEMPO_RETENTION; Grafana in $GRAFANA_NAMESPACE may read them."
else
  info "Traces: off (TELEMETRY=off): no Tempo, and the agents send no traces."
  # Tempo of an earlier deploy is not removed by an apply: say how.
  tempo_left="$(k get deployment "$TEMPO" --ignore-not-found -o name)" || could_not_look deployment "$TEMPO"
  if [[ -n "$tempo_left" ]]; then
    info "NOTE: Tempo of an earlier deploy is still there. To remove it (the traces too):"
    info "  kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE delete deployment,service,networkpolicy,serviceaccount,configmap $TEMPO"
    info "  kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE delete persistentvolumeclaim $APP_NAME-tempo-data"
  fi
fi
guard_summary="$(sed -n 's/^NOTE: //p' <<<"$GUARD_CHECK_NOTES" | paste -sd ';' - | sed 's/;/; /g')"
if [[ -n "$GUARD_MODEL_URL" ]]; then
  info "Guard: $GUARD_MODE; fixed rules, then the small model $GUARD_MODEL (KubeAI). Rules: $guard_summary."
else
  info "Guard: $GUARD_MODE; fixed rules only (no model the orchestrator can reach). Rules: $guard_summary."
fi
if [[ "$INJECTION_MODE" != off ]]; then
  info "Injection model: $INJECTION_MODE; $INJECTION_MODEL_REPO ($INJECTION_MODEL_FILE), threshold $INJECTION_THRESHOLD, in the pod $SHIELD."
else
  info "Injection model: off (INJECTION_MODE=off): the Guard checks the evidence with its rules only."
  # The shield of an earlier deploy is not removed by an apply: say how.
  shield_left="$(k get deployment "$SHIELD" --ignore-not-found -o name)" || could_not_look deployment "$SHIELD"
  if [[ -n "$shield_left" ]]; then
    info "NOTE: the shield of an earlier deploy is still there. To remove it:"
    info "  kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE delete deployment,service,networkpolicy,serviceaccount,secret $SHIELD"
  fi
fi
if [[ -n "$DIRTY" ]]; then info "NOTE: files that are not committed are deployed (--allow-dirty)."; fi

# ---- the image ----------------------------------------------------------------------

BUILD_OPTIONS=()
if [[ -n "${BUILD_NETWORK:-}" ]]; then BUILD_OPTIONS+=(--network "$BUILD_NETWORK"); fi
IMAGE_REF="$REGISTRY/$APP_NAME:$TAG"
info "Image: building $IMAGE_REF (the first time takes a few minutes)."
docker build -q --platform linux/amd64 --provenance=false ${BUILD_OPTIONS[@]+"${BUILD_OPTIONS[@]}"} -t "$IMAGE_REF" "$ROOT" >/dev/null \
  || die "Could not build the image. If the step that failed downloads something (uv sync), a Docker build on this machine may not look up names: run again with BUILD_NETWORK=host in front."
docker push -q "$IMAGE_REF" >/dev/null || die "Could not push $IMAGE_REF. Run: docker login $REGISTRY_HOST"
DIGEST="$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$IMAGE_REF" 2>/dev/null || true)"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Could not read the digest of $IMAGE_REF from the registry."
IMAGE="$IMAGE_REF@$DIGEST"
export IMAGE
info "Image: pushed, $DIGEST"

# The shield's own image (Dockerfile.shield): Python, ONNX Runtime, the tokenizer and one file.
SHIELD_IMAGE=""
if [[ "$INJECTION_MODE" != off ]]; then
  SHIELD_REF="$REGISTRY/$SHIELD:$(shield_tag)"
  info "Image: building $SHIELD_REF (the shield)."
  docker build -q --platform linux/amd64 --provenance=false ${BUILD_OPTIONS[@]+"${BUILD_OPTIONS[@]}"} -f "$ROOT/Dockerfile.shield" -t "$SHIELD_REF" "$ROOT" >/dev/null \
    || die "Could not build the shield's image. If the step that failed downloads something (uv sync), run again with BUILD_NETWORK=host in front."
  docker push -q "$SHIELD_REF" >/dev/null || die "Could not push $SHIELD_REF. Run: docker login $REGISTRY_HOST"
  SHIELD_DIGEST="$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$SHIELD_REF" 2>/dev/null || true)"
  [[ "$SHIELD_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Could not read the digest of $SHIELD_REF from the registry."
  SHIELD_IMAGE="$SHIELD_REF@$SHIELD_DIGEST"
  info "Image: pushed, $SHIELD_DIGEST (the shield)"
fi
export SHIELD_IMAGE

# ---- the Secret (made once, never changed) -----------------------------------------

# Tokens of 32 random bytes each, base64url. They go to kubectl on standard input: never on a
# command line, never on screen, never on disk.
new_secret() {  # NAME KEY...
  python3 -c '
import json, secrets, sys
name, namespace, app, *keys = sys.argv[1:]
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/part-of": app}},
    "type": "Opaque",
    "stringData": {key: secrets.token_urlsafe(32) for key in keys},
}))' "$1" "$K8S_NAMESPACE" "$APP_NAME" "${@:2}"
}
if [[ "$have_secret" == yes ]]; then
  info "Secret: $APP_NAME is already there, kept."
else
  if ! new_secret "$APP_NAME" "${SECRET_KEYS[@]}" | k create -f - >/dev/null; then
    # A create that timed out may still have been done.
    have secret "$APP_NAME" || die "The Secret $APP_NAME could not be made (the words of kubectl are above)."
  fi
  info "Secret: $APP_NAME is made (three new tokens)."
fi
# The shield's token: its own Secret, made once the same way. The Secret $APP_NAME stays as it is.
if [[ "$INJECTION_MODE" != off ]]; then
  if [[ "$have_shield_secret" == yes ]]; then
    info "Secret: $SHIELD_SECRET is already there, kept."
  else
    if ! new_secret "$SHIELD_SECRET" "${SHIELD_SECRET_KEYS[@]}" | k create -f - >/dev/null; then
      have secret "$SHIELD_SECRET" || die "The Secret $SHIELD_SECRET could not be made (the words of kubectl are above)."
    fi
    info "Secret: $SHIELD_SECRET is made (a new token for the shield)."
  fi
fi

# ---- apply ------------------------------------------------------------------------

# The etcd of this cluster sometimes answers too late. Such an apply is tried again, up to
# three times in all; any other error stops at once.
APPLY_RETRY_SECONDS="${APPLY_RETRY_SECONDS:-10}"
apply_with_retries() {  # the files on standard input
  local manifests output try
  manifests="$(cat)"
  for try in 1 2 3; do
    if output="$(k apply -f - <<<"$manifests" 2>&1)"; then
      printf '%s\n' "$output" | sed 's/^/  /'
      return 0
    fi
    if [[ "$try" -lt 3 ]] && grep -Eqi 'timed? ?out|timeout|deadline exceeded|etcdserver|connection reset|unexpected EOF|TLS handshake|ServiceUnavailable|InternalError|try again' <<<"$output"; then
      info "kubectl apply: the cluster did not answer in time (try $try of 3). Trying again in ${APPLY_RETRY_SECONDS}s."
      sleep "$APPLY_RETRY_SECONDS"
      continue
    fi
    printf '%s\n' "$output" >&2
    return 1
  done
}
info "Apply:"
filled_yaml | apply_with_retries || die "The Kubernetes files could not be applied (the words of kubectl are above)."

# ---- the settings of the CLI on this laptop -----------------------------------------

CONFIG_DIR="$(cli_config_dir)"
write_cli_settings() {
  # Once: the CLI settings of the old name are copied into the new folder before it is written,
  # so the read-only kubeconfigs of "k8s add" are kept. The CLI does the same (the same file).
  python3 -I "$ROOT/src/wattson/cli/migrate.py" || true
  (umask 077 && mkdir -p "$CONFIG_DIR")
  python3 - "$CONFIG_DIR/config.json" "$K8S_CONTEXT" "$K8S_NAMESPACE" "$ORCHESTRATOR" "$ORCHESTRATOR_PORT" <<'PY'
import json, os, sys, tempfile
path, context, namespace, deployment, port = sys.argv[1:]
data = {}
try:
    with open(path, encoding="utf-8") as file:
        loaded = json.load(file)
    if isinstance(loaded, dict):
        data = loaded  # other keys of the CLI are kept
except (OSError, ValueError):
    pass
data.update(k8s_context=context, namespace=namespace, deployment=deployment, remote_port=int(port))
fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config-")
with os.fdopen(fd, "w", encoding="utf-8") as file:
    json.dump(data, file, indent=2)
    file.write("\n")
os.chmod(temp, 0o600)
os.replace(temp, path)
PY
  # The token goes from the Secret into a file of mode 600, never through the screen.
  local temp token
  temp="$(umask 077 && mktemp "$CONFIG_DIR/.token-XXXXXX")"
  if ! secret_value WATTSON_TOKEN >"$temp"; then
    rm -f "$temp"
    return 1
  fi
  token="$(<"$temp")"
  if [[ ${#token} -lt 32 || ! "$token" =~ ^[A-Za-z0-9_-]+$ ]]; then
    rm -f "$temp"
    return 1
  fi
  chmod 600 "$temp"
  mv -f "$temp" "$CONFIG_DIR/token"
}
write_cli_settings || die "The settings of the CLI could not be written to $CONFIG_DIR (the token could not be read from the Secret $APP_NAME)."
info "CLI: $CONFIG_DIR/config.json and $CONFIG_DIR/token (mode 600) are written."

# ---- wait for the pods ---------------------------------------------------------------

if [[ "$first_start" == yes ]]; then
  TRIAGE_WAIT="${ROLLOUT_WAIT_SECONDS:-900}"
  info "Waiting for the pods. The first start downloads the model (about 0.9 GB): up to $((TRIAGE_WAIT / 60)) minutes."
else
  TRIAGE_WAIT="${ROLLOUT_WAIT_SECONDS:-300}"
  info "Waiting for the pods."
fi
OTHER_WAIT="${ROLLOUT_WAIT_SECONDS:-300}"
[[ "$TRIAGE_WAIT" =~ ^[1-9][0-9]{0,4}$ && "$OTHER_WAIT" =~ ^[1-9][0-9]{0,4}$ ]] \
  || die "ROLLOUT_WAIT_SECONDS must be a whole number of seconds."
WAIT_FOR=("$ORCHESTRATOR:$OTHER_WAIT" "$KNOWLEDGE:$OTHER_WAIT" "$TRIAGE:$TRIAGE_WAIT")
if [[ "$TELEMETRY" == on ]]; then WAIT_FOR+=("$TEMPO:$OTHER_WAIT"); fi
if [[ "$INJECTION_MODE" != off ]]; then WAIT_FOR+=("$SHIELD:$OTHER_WAIT"); fi
for pair in "${WAIT_FOR[@]}"; do
  name="${pair%%:*}" seconds="${pair##*:}"
  k rollout status "deployment/$name" --timeout="${seconds}s" >/dev/null \
    || die "$name is not running after ${seconds}s. Look at it: bash k8s/doctor.sh (or kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE describe pod -l app=$name)"
  info "  $name: running."
done

# ---- the result ------------------------------------------------------------------

info ""
info "Done. The next steps, on this laptop:"
info "  wattson k8s add $K8S_CONTEXT     a read-only kubeconfig for the investigations (asks first)"
info "  wattson grimoire connect      connect the knowledge agent to Grimoire (opens the browser)"
info "  wattson status                checks the three agents"
if [[ "$TELEMETRY" == on ]]; then
  info "  bash k8s/grafana.sh --apply   the traces in Grafana ($GRAFANA_NAMESPACE): data source and dashboard (asks first)"
fi
info "Check the pods any time: bash k8s/doctor.sh"
