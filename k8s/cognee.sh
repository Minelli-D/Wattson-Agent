#!/usr/bin/env bash
# Puts Cognee on the cluster: a memory for Claude Code, and its web UI. One pod in the namespace
# of Wattson with three containers: Cognee's API server (the only process that opens the stores
# on the volume), cognee-mcp in API mode (for Claude Code) and the web UI; its volume, a Secret
# (the UI password and the MCP's API key, random, made once), a Service inside the cluster only
# (MCP), and its network rule (k8s/cognee/cognee.yaml). Local models only, no LLM, no telemetry
# (docs/cognee.md). Its two images are ours, built from k8s/cognee/ and k8s/cognee/ui/ (pinned
# packages, models and source). Without an option it only prints what it would do; it runs
# neither docker nor kubectl.
#
#   bash k8s/cognee.sh                 print the plan: the filled-in files, the images, the commands
#   bash k8s/cognee.sh --dry-run       the cluster checks the filled-in files (kubectl apply
#                                      --dry-run=server); nothing is built or changed
#   bash k8s/cognee.sh --apply         checks, then you type the name of the context, then it builds
#                                      and pushes the two images (each only when the registry does
#                                      not have its tag yet), makes the Secret (once), applies the
#                                      files and waits for the pod
#   bash k8s/cognee.sh --status        the pod and its three containers, the images, the volume,
#                                      the last events (read-only)
#   bash k8s/cognee.sh --port-forward  the three ports of COGNEE_LOCAL_PORT, COGNEE_API_LOCAL_PORT and
#                                      COGNEE_UI_LOCAL_PORT on 127.0.0.1 -> the pod, until Ctrl-C;
#                                      it prints the UI address and the command for Claude Code
#   bash k8s/cognee.sh --smoke         through a short port-forward: the API and the UI answer, the
#                                      UI login works; through MCP: remember 3 made-up notes and a
#                                      made-up code file in the dataset "smoke-test", recall,
#                                      code_search; the API sees that dataset; forget it; PASS or FAIL
#   bash k8s/cognee.sh --top           CPU and memory of each container now (kubectl top)
#   bash k8s/cognee.sh --delete        you type the name of the context, then the Deployment, the
#                                      Service, the network rule and the ServiceAccount go; the
#                                      volume (the memory) and the Secret (its login) are KEPT
#   bash k8s/cognee.sh --delete --with-data    the same, and the volume and the Secret go too: the
#                                      memory is lost
#
# The images are $REGISTRY/<APP_NAME>-cognee:<cognee version>-<hash of the files of k8s/cognee>
# and $REGISTRY/<APP_NAME>-cognee-ui:<cognee version>-<hash of the files of k8s/cognee/ui>: the
# same files make the same tag, so a second --apply does not build again.
# The UI password is copied from the Secret into ~/.config/wattson/cognee-ui-password (mode 600),
# never onto the screen.
#
# BUILD_NETWORK=host bash k8s/cognee.sh --apply      when "docker build" cannot look up names
#                                                    (the Wi-Fi or a VPN)
# COGNEE_WAIT_SECONDS=1800 bash k8s/cognee.sh --apply    to wait longer for the pod (the first
#                                                    start on a node pulls the image: 3.4 GB unpacked)
set -euo pipefail

# shellcheck source=k8s/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ACTION=plan
WITH_DATA=no
for arg in "$@"; do
  case "$arg" in
    --dry-run | --apply | --status | --port-forward | --smoke | --top | --delete)
      [[ "$ACTION" == plan ]] || die "One action at a time. See: bash k8s/cognee.sh --help"
      ACTION="${arg#--}"
      ;;
    --with-data) WITH_DATA=yes ;;
    -h | --help)
      sed -n '2,43p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "Unknown option: $arg. See: bash k8s/cognee.sh --help" ;;
  esac
done
[[ "$WITH_DATA" == no || "$ACTION" == delete ]] || die "--with-data goes with --delete only."

read_settings
check_settings

# ---- the settings of cognee (COGNEE_* in settings.env) ---------------------------------

COGNEE_DIR="$HERE/cognee"
UI_DIR="$COGNEE_DIR/ui"
# The ports in the pod: MCP (COGNEE_PORT, in k8s/lib.sh: the knowledge agent's network rule
# names it too; also the port of the Service) and the UI. The API server listens on
# COGNEE_API_LOCAL_PORT, the same number as on the laptop: the UI's server calls it at the
# address the browser uses.
COGNEE_UI_PORT=3000
is_local_port() {  # VALUE
  [[ "$1" =~ ^[1-9][0-9]{3,4}$ ]] && (($1 >= 1024 && $1 <= 65535))
}
check_cognee() {
  is_size "$COGNEE_SIZE" || die "COGNEE_SIZE must look like 10Gi. It is: \"$COGNEE_SIZE\""
  split_resources COGNEE_REQUESTS COGNEE_REQ
  split_resources COGNEE_LIMITS COGNEE_LIM
  split_resources COGNEE_MCP_REQUESTS COGNEE_MCP_REQ
  split_resources COGNEE_MCP_LIMITS COGNEE_MCP_LIM
  split_resources COGNEE_UI_REQUESTS COGNEE_UI_REQ
  split_resources COGNEE_UI_LIMITS COGNEE_UI_LIM
  local name
  for name in COGNEE_LOCAL_PORT COGNEE_API_LOCAL_PORT COGNEE_UI_LOCAL_PORT; do
    is_local_port "${!name}" || die "$name must be a port from 1024 to 65535, like 8765. It is: \"${!name}\""
  done
  [[ "$COGNEE_LOCAL_PORT" != "$COGNEE_API_LOCAL_PORT" && "$COGNEE_LOCAL_PORT" != "$COGNEE_UI_LOCAL_PORT" && "$COGNEE_API_LOCAL_PORT" != "$COGNEE_UI_LOCAL_PORT" ]] \
    || die "COGNEE_LOCAL_PORT, COGNEE_API_LOCAL_PORT and COGNEE_UI_LOCAL_PORT must be three different ports."
  [[ "$COGNEE_API_LOCAL_PORT" != "$COGNEE_PORT" && "$COGNEE_API_LOCAL_PORT" != "$COGNEE_UI_PORT" ]] \
    || die "COGNEE_API_LOCAL_PORT is also the API server's port in the pod: it cannot be $COGNEE_PORT (MCP) or $COGNEE_UI_PORT (the UI)."
  COGNEE_API_PORT="$COGNEE_API_LOCAL_PORT"
  # torch (GLiNER) uses as many threads as the CPU limit of the API server has whole cores (at least one).
  COGNEE_THREADS=$(($(cpu_millis "$COGNEE_LIM_CPU") / 1000))
  ((COGNEE_THREADS >= 1)) || COGNEE_THREADS=1
  COGNEE="$APP_NAME-cognee"
  export COGNEE COGNEE_PORT COGNEE_API_PORT COGNEE_UI_PORT COGNEE_UI_LOCAL_PORT COGNEE_SIZE COGNEE_THREADS
  export COGNEE_REQ_CPU COGNEE_REQ_MEMORY COGNEE_LIM_CPU COGNEE_LIM_MEMORY
  export COGNEE_MCP_REQ_CPU COGNEE_MCP_REQ_MEMORY COGNEE_MCP_LIM_CPU COGNEE_MCP_LIM_MEMORY
  export COGNEE_UI_REQ_CPU COGNEE_UI_REQ_MEMORY COGNEE_UI_LIM_CPU COGNEE_UI_LIM_MEMORY
}
check_cognee

# The files each image is made of; their hash is in its tag. The version is the line
# "ARG COGNEE_VERSION=" of each Dockerfile.
BUILD_FILES=(Dockerfile .dockerignore pyproject.toml uv.lock models.txt fetch_models.py api_start.py)
UI_BUILD_FILES=(Dockerfile .dockerignore)
for file in "${BUILD_FILES[@]}" cognee.yaml smoke.py; do
  [[ -f "$COGNEE_DIR/$file" ]] || die "k8s/cognee/$file is missing."
done
for file in "${UI_BUILD_FILES[@]}"; do
  [[ -f "$UI_DIR/$file" ]] || die "k8s/cognee/ui/$file is missing."
done
version_of() {  # DOCKERFILE
  sed -n 's/^ARG COGNEE_VERSION=\([0-9][0-9.]*\)$/\1/p' "$1"
}
COGNEE_VERSION="$(version_of "$COGNEE_DIR/Dockerfile")"
UI_VERSION="$(version_of "$UI_DIR/Dockerfile")"
[[ -n "$COGNEE_VERSION" ]] || die "k8s/cognee/Dockerfile has no line ARG COGNEE_VERSION=<version>."
[[ "$UI_VERSION" == "$COGNEE_VERSION" ]] || die "k8s/cognee/ui/Dockerfile must build the same cognee version as k8s/cognee/Dockerfile ($COGNEE_VERSION)."
TAG="$COGNEE_VERSION-$( (cd "$COGNEE_DIR" && sha256sum "${BUILD_FILES[@]}") | sha256sum | cut -c1-12)"
UI_TAG="$UI_VERSION-$( (cd "$UI_DIR" && sha256sum "${UI_BUILD_FILES[@]}") | sha256sum | cut -c1-12)"
IMAGE_REF="$REGISTRY/$COGNEE:$TAG"
UI_IMAGE_REF="$REGISTRY/$COGNEE-ui:$UI_TAG"
# The addresses on the laptop. 127.0.0.1 everywhere, never localhost: the browser must load the
# UI and call the API on the same host, or it does not send the login cookie to the API.
URL="http://127.0.0.1:$COGNEE_LOCAL_PORT/mcp"
API_URL="http://127.0.0.1:$COGNEE_API_LOCAL_PORT"
UI_URL="http://127.0.0.1:$COGNEE_UI_LOCAL_PORT"
CLAUDE_ADD="claude mcp add --transport http --scope user cognee $URL"
UI_USER="default_user@example.com"
# The UI password on this laptop: a copy of the Secret's, mode 600.
UI_PASSWORD_FILE="$(cli_config_dir)/cognee-ui-password"

# The names filled in in k8s/cognee/cognee.yaml. tests/k8s/test-k8s-files.sh reads this line.
# shellcheck disable=SC2016  # the ${...} names are for envsubst, not for the shell
COGNEE_YAML_NAMES='${APP_NAME} ${COGNEE} ${K8S_NAMESPACE} ${COGNEE_IMAGE} ${COGNEE_UI_IMAGE} ${PULL_SECRET} ${STORAGE_CLASS} ${COGNEE_SIZE} ${COGNEE_PORT} ${COGNEE_API_PORT} ${COGNEE_UI_PORT} ${COGNEE_UI_LOCAL_PORT} ${COGNEE_REQ_CPU} ${COGNEE_REQ_MEMORY} ${COGNEE_LIM_CPU} ${COGNEE_LIM_MEMORY} ${COGNEE_MCP_REQ_CPU} ${COGNEE_MCP_REQ_MEMORY} ${COGNEE_MCP_LIM_CPU} ${COGNEE_MCP_LIM_MEMORY} ${COGNEE_UI_REQ_CPU} ${COGNEE_UI_REQ_MEMORY} ${COGNEE_UI_LIM_CPU} ${COGNEE_UI_LIM_MEMORY} ${COGNEE_THREADS} ${ORCHESTRATOR} ${KNOWLEDGE} ${NODE_LOCAL_DNS}'

# k8s/cognee/cognee.yaml, filled in. COGNEE_IMAGE and COGNEE_UI_IMAGE must be set. A line that
# ends with "# optional: NAME" is left out when the setting NAME is empty; when it is set, only
# the mark goes.
filled_yaml() {
  awk -v pull="$PULL_SECRET" -v dns="$NODE_LOCAL_DNS" '
    /# optional: PULL_SECRET$/ { if (pull == "") next }
    /# optional: NODE_LOCAL_DNS$/ { if (dns == "") next }
    { sub(/ +# optional: [A-Z_]+$/, ""); print }' <"$COGNEE_DIR/cognee.yaml" | envsubst "$COGNEE_YAML_NAMES"
}

# Every object of cognee.yaml but the volume. The volume and the Secret are the memory and its
# login: --delete keeps them, --delete --with-data removes them.
OBJECTS=("deployment/$COGNEE" "service/$COGNEE" "networkpolicy/$COGNEE" "serviceaccount/$COGNEE")
VOLUME="persistentvolumeclaim/$COGNEE-data"
SECRET="secret/$COGNEE"
# The keys of the Secret, each made once with random values (never on a command line, on the
# screen or in a file of the repository): the password of the UI login (of the default user,
# set on the first start), the API key of the MCP container, and the three keys that sign the
# login tokens.
SECRET_TOKEN_KEYS=(DEFAULT_USER_PASSWORD FASTAPI_USERS_JWT_SECRET FASTAPI_USERS_RESET_PASSWORD_TOKEN_SECRET FASTAPI_USERS_VERIFICATION_TOKEN_SECRET)
SECRET_HEX_KEYS=(COGNEE_MCP_API_KEY)

need_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed."
  done
}
need_context() {
  local seen_context
  seen_context="$(kubectl --context "$K8S_CONTEXT" config view --minify -o 'jsonpath={.contexts[0].name}' 2>/dev/null || true)"
  [[ "$seen_context" == "$K8S_CONTEXT" ]] || die "kubectl has no context \"$K8S_CONTEXT\". See: kubectl config get-contexts"
}
could_not_look() {  # WHAT
  die "kubectl could not read $1 in the namespace \"$K8S_NAMESPACE\" (its own words are above). Nothing was changed. Check the connection (VPN) and run again."
}
# "name|part-of label" of an object, or nothing when it is not there.
owner_of() {  # KIND/NAME
  k get "${1%%/*}" "${1#*/}" --ignore-not-found -o 'jsonpath={.metadata.name}{"|"}{.metadata.labels.app\.kubernetes\.io/part-of}'
}
# The pod must be there and ready (for --port-forward and --smoke).
need_ready_pod() {
  local ready
  ready="$(k get deployment "$COGNEE" --ignore-not-found -o 'jsonpath={.status.readyReplicas}')" || could_not_look "the Deployment $COGNEE"
  [[ "$ready" == 1 ]] || die "$COGNEE is not running and ready. Look at it: bash k8s/cognee.sh --status (or put it there: bash k8s/cognee.sh --apply)"
}
# True when something on this laptop already listens on the port.
port_in_use() {  # PORT
  python3 -I -c 'import socket, sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$1"
}
# True when cognee-mcp answers /health on its local port.
local_health() {
  [[ "$(curl -s -m 5 "http://127.0.0.1:$COGNEE_LOCAL_PORT/health" 2>/dev/null || true)" == *'"ok"'* ]]
}
# The three ports, laptop:pod, for kubectl port-forward.
FORWARD_PORTS=("$COGNEE_LOCAL_PORT:$COGNEE_PORT" "$COGNEE_API_LOCAL_PORT:$COGNEE_API_PORT" "$COGNEE_UI_LOCAL_PORT:$COGNEE_UI_PORT")
ask_context() {  # WHAT-IT-DOES
  local answer=""
  info ""
  printf 'Type "%s" to %s: ' "$K8S_CONTEXT" "$1"
  IFS= read -r answer || true
  if [[ "$answer" != "$K8S_CONTEXT" ]]; then
    info ""
    info "Nothing was changed."
    exit 1
  fi
}
# The UI password goes from the Secret into a file of mode 600 on this laptop, never through
# the screen.
write_ui_password() {
  local dir temp value
  dir="$(dirname "$UI_PASSWORD_FILE")"
  (umask 077 && mkdir -p "$dir")
  temp="$(umask 077 && mktemp "$dir/.cognee-ui-password-XXXXXX")"
  if ! k get secret "$COGNEE" -o 'jsonpath={.data.DEFAULT_USER_PASSWORD}' | base64 -d >"$temp" 2>/dev/null; then
    rm -f "$temp"
    return 1
  fi
  value="$(<"$temp")"
  if [[ ${#value} -lt 16 ]]; then
    rm -f "$temp"
    return 1
  fi
  chmod 600 "$temp"
  mv -f "$temp" "$UI_PASSWORD_FILE"
}
need_ui_password() {
  [[ -s "$UI_PASSWORD_FILE" ]] && return 0
  write_ui_password || die "The UI password could not be read from the Secret $COGNEE (the words of kubectl are above). Put it there with: bash k8s/cognee.sh --apply"
  info "The UI password is copied from the Secret $COGNEE into $UI_PASSWORD_FILE (mode 600)."
}

# ---- the plan, and --dry-run ------------------------------------------------------------

if [[ "$ACTION" == plan || "$ACTION" == dry-run ]]; then
  need_tools envsubst sha256sum
  # The digests are known only after the push: here they are stand-ins of zeros.
  COGNEE_IMAGE="$IMAGE_REF@sha256:$(printf '0%.0s' {1..64})"
  COGNEE_UI_IMAGE="$UI_IMAGE_REF@sha256:$(printf '0%.0s' {1..64})"
  export COGNEE_IMAGE COGNEE_UI_IMAGE
  info "# Cognee $COGNEE_VERSION for $APP_NAME: context $K8S_CONTEXT, namespace $K8S_NAMESPACE."
  info "# One pod $COGNEE, three containers: api (Cognee's API server, the only one on the volume), mcp (cognee-mcp in API mode), ui (the web UI)."
  info "# Image $IMAGE_REF (api and mcp; built from k8s/cognee/ by --apply)."
  info "# Image $UI_IMAGE_REF (ui; built from k8s/cognee/ui/ by --apply). The digests below are stand-ins."
  info "# Volume $COGNEE-data, $COGNEE_SIZE on $STORAGE_CLASS. Secret $COGNEE (made once by --apply, random values)."
  info "# CPU/memory: api $COGNEE_REQ_CPU/$COGNEE_REQ_MEMORY up to $COGNEE_LIM_CPU/$COGNEE_LIM_MEMORY, mcp $COGNEE_MCP_REQ_CPU/$COGNEE_MCP_REQ_MEMORY up to $COGNEE_MCP_LIM_CPU/$COGNEE_MCP_LIM_MEMORY, ui $COGNEE_UI_REQ_CPU/$COGNEE_UI_REQ_MEMORY up to $COGNEE_UI_LIM_CPU/$COGNEE_UI_LIM_MEMORY."
  info "# The files, filled in:"
  info "---"
  filled_yaml
  info "---"
  info "# What --apply runs, after you type the name of the context:"
  info "#   docker build --platform linux/amd64 -f k8s/cognee/Dockerfile -t $IMAGE_REF k8s/cognee   (only when the registry has no such tag)"
  info "#   docker build --platform linux/amd64 -f k8s/cognee/ui/Dockerfile -t $UI_IMAGE_REF k8s/cognee/ui   (only when the registry has no such tag)"
  info "#   docker push of the image(s) it built"
  info "#   kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE create -f -   (the Secret $COGNEE, only when it is not there; its values on standard input)"
  info "#   kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE apply -f <the files above, with the digests of the push>"
  info "#   kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE rollout status deployment/$COGNEE"
  info "# Then: bash k8s/cognee.sh --smoke, and for Claude Code and the UI: bash k8s/cognee.sh --port-forward"
  if [[ "$ACTION" == plan ]]; then
    info ""
    info "Nothing was run. The cluster's own check of the files: bash k8s/cognee.sh --dry-run. To put it there: bash k8s/cognee.sh --apply"
    exit 0
  fi
  need_tools kubectl
  need_context
  info ""
  info "The cluster checks the files (kubectl apply --dry-run=server): nothing is changed."
  filled_yaml | k apply --dry-run=server -f - | sed 's/^/  /' \
    || die "The cluster does not accept the files (its words are above)."
  info "Done: the cluster accepts the files. Nothing was changed."
  exit 0
fi

need_tools kubectl
need_context

# ---- --status, --top -------------------------------------------------------------------

if [[ "$ACTION" == status ]]; then
  info "# Cognee on $K8S_CONTEXT, namespace $K8S_NAMESPACE (only read; no value of the Secret)."
  k get deployment,service,networkpolicy,serviceaccount,persistentvolumeclaim,secret \
    -l "app.kubernetes.io/component=cognee,app.kubernetes.io/part-of=$APP_NAME" || could_not_look "the objects of $COGNEE"
  info ""
  k get pods -l "app=$COGNEE" -o wide || could_not_look "the pods of $COGNEE"
  info ""
  info "# The containers (api, mcp, ui):"
  k get pods -l "app=$COGNEE" -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{"  "}{.name}{": ready="}{.ready}{" restarts="}{.restartCount}{" waiting="}{.state.waiting.reason}{" last="}{.lastState.terminated.reason}{"\n"}{end}{end}' || true
  k get deployment "$COGNEE" --ignore-not-found -o jsonpath='{range .spec.template.spec.containers[*]}{"  image "}{.name}{": "}{.image}{"\n"}{end}' || true
  info "  this checkout builds: $IMAGE_REF (api, mcp) and $UI_IMAGE_REF (ui)"
  pod="$(k get pods -l "app=$COGNEE" -o 'jsonpath={.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -n "$pod" ]]; then
    info ""
    info "# The last events of $pod:"
    k get events --field-selector "involvedObject.name=$pod" --sort-by=.lastTimestamp 2>/dev/null | tail -n 10 || true
    info ""
    info "# The logs: kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE logs deployment/$COGNEE -c api   (or -c mcp, -c ui)"
  fi
  exit 0
fi

if [[ "$ACTION" == top ]]; then
  info "# CPU and memory of each container of $COGNEE now:"
  info "#   api: requests $COGNEE_REQ_CPU/$COGNEE_REQ_MEMORY, limits $COGNEE_LIM_CPU/$COGNEE_LIM_MEMORY (COGNEE_REQUESTS, COGNEE_LIMITS)"
  info "#   mcp: requests $COGNEE_MCP_REQ_CPU/$COGNEE_MCP_REQ_MEMORY, limits $COGNEE_MCP_LIM_CPU/$COGNEE_MCP_LIM_MEMORY (COGNEE_MCP_REQUESTS, COGNEE_MCP_LIMITS)"
  info "#   ui:  requests $COGNEE_UI_REQ_CPU/$COGNEE_UI_REQ_MEMORY, limits $COGNEE_UI_LIM_CPU/$COGNEE_UI_LIM_MEMORY (COGNEE_UI_REQUESTS, COGNEE_UI_LIMITS)"
  k top pod -l "app=$COGNEE" --containers \
    || die "kubectl top gave no numbers (the words of kubectl are above): is $COGNEE running, and does the cluster have metrics-server?"
  exit 0
fi

# ---- --port-forward, --smoke --------------------------------------------------------------

if [[ "$ACTION" == port-forward ]]; then
  need_ready_pod
  for port in "$COGNEE_LOCAL_PORT" "$COGNEE_API_LOCAL_PORT" "$COGNEE_UI_LOCAL_PORT"; do
    port_in_use "$port" && die "Something on this laptop listens on port $port already (another port-forward?). Stop it, or change COGNEE_LOCAL_PORT, COGNEE_API_LOCAL_PORT or COGNEE_UI_LOCAL_PORT in settings.env (then --apply)."
  done
  need_ui_password
  info "While this runs (stop it with Ctrl-C):"
  info "  the web UI:  $UI_URL   (open it in the browser; use 127.0.0.1, not localhost)"
  info "               log in as $UI_USER, password in $UI_PASSWORD_FILE"
  info "  MCP:         $URL   (streamable HTTP). To connect Claude Code (once; it keeps the setting):"
  info "               $CLAUDE_ADD"
  info "  the API:     $API_URL   (the pages of the UI call it; it wants the UI login or an API key)"
  exec kubectl --context "$K8S_CONTEXT" -n "$K8S_NAMESPACE" port-forward --address 127.0.0.1 "deployment/$COGNEE" "${FORWARD_PORTS[@]}"
fi

if [[ "$ACTION" == smoke ]]; then
  need_tools python3 curl
  need_ready_pod
  need_ui_password
  FORWARD_PID=""
  stop_forward() {
    if [[ -n "$FORWARD_PID" ]]; then kill "$FORWARD_PID" 2>/dev/null || true; fi
    if [[ -n "${FORWARD_LOG:-}" ]]; then rm -f "$FORWARD_LOG"; fi
  }
  trap stop_forward EXIT
  busy=0
  for port in "$COGNEE_LOCAL_PORT" "$COGNEE_API_LOCAL_PORT" "$COGNEE_UI_LOCAL_PORT"; do
    if port_in_use "$port"; then busy=$((busy + 1)); fi
  done
  if [[ "$busy" -eq 3 ]]; then
    local_health || die "Something on this laptop listens on port $COGNEE_LOCAL_PORT, and it is not cognee-mcp. Stop it, or set COGNEE_LOCAL_PORT in settings.env."
    info "The port-forward on $COGNEE_LOCAL_PORT, $COGNEE_API_LOCAL_PORT and $COGNEE_UI_LOCAL_PORT is running already: the smoke test uses it."
  elif [[ "$busy" -gt 0 ]]; then
    die "Some of the ports $COGNEE_LOCAL_PORT, $COGNEE_API_LOCAL_PORT, $COGNEE_UI_LOCAL_PORT are in use on this laptop, not all three (an old port-forward?). Stop it, or change them in settings.env."
  else
    FORWARD_LOG="$(mktemp)"
    # kubectl itself in the background (not the function k): $! is its PID, so the trap stops it.
    kubectl --context "$K8S_CONTEXT" -n "$K8S_NAMESPACE" port-forward --address 127.0.0.1 "deployment/$COGNEE" "${FORWARD_PORTS[@]}" >"$FORWARD_LOG" 2>&1 &
    FORWARD_PID=$!
    for _ in $(seq 1 30); do
      local_health && break
      kill -0 "$FORWARD_PID" 2>/dev/null || break
      sleep 1
    done
    if ! local_health; then
      cat "$FORWARD_LOG" >&2
      die "The port-forward to $COGNEE does not answer on $COGNEE_LOCAL_PORT (the words of kubectl are above)."
    fi
  fi
  info ""
  started=$SECONDS
  status=0
  python3 -I "$COGNEE_DIR/smoke.py" "$URL" "$API_URL" "$UI_URL" "$UI_PASSWORD_FILE" || status=$?
  info ""
  info "The smoke test took $((SECONDS - started)) s. Now the real CPU and memory: bash k8s/cognee.sh --top"
  exit "$status"
fi

# ---- --delete ------------------------------------------------------------------------

if [[ "$ACTION" == delete ]]; then
  targets=("${OBJECTS[@]}")
  if [[ "$WITH_DATA" == yes ]]; then targets+=("$VOLUME" "$SECRET"); fi
  found=() foreign=()
  for object in "${targets[@]}"; do
    seen="$(owner_of "$object")" || could_not_look "$object"
    [[ -n "$seen" ]] || continue
    if [[ "${seen#*|}" == "$APP_NAME" ]]; then found+=("$object"); else foreign+=("$object"); fi
  done
  info "Cognee on $K8S_CONTEXT, namespace $K8S_NAMESPACE."
  if [[ ${#foreign[@]} -gt 0 ]]; then
    info "These have our names but were not made by k8s/cognee.sh; they are left alone: ${foreign[*]}"
  fi
  if [[ ${#found[@]} -eq 0 ]]; then
    info "Nothing of $COGNEE is there. Nothing to remove."
    exit 0
  fi
  info "To be removed:"
  printf '  %s\n' "${found[@]}"
  if [[ "$WITH_DATA" == yes ]]; then
    info "WITH THE VOLUME $COGNEE-data AND THE SECRET $COGNEE: everything remembered is lost, for good."
  else
    info "The volume $COGNEE-data (the memory) and the Secret $COGNEE (its login) are kept: the next --apply finds them again."
  fi
  ask_context "remove them"
  failed=0
  for object in "${found[@]}"; do
    if k delete "${object%%/*}" "${object#*/}" --ignore-not-found --wait=false >/dev/null; then
      info "  removed $object"
    else
      failed=$((failed + 1))
      info "  NOT removed $object (the words of kubectl are above)"
    fi
  done
  [[ "$failed" -eq 0 ]] || die "$failed object(s) could not be removed. Run the script again."
  if [[ "$WITH_DATA" == yes && -e "$UI_PASSWORD_FILE" ]]; then
    rm -f "$UI_PASSWORD_FILE"
    info "  removed $UI_PASSWORD_FILE (the copy of the UI password on this laptop)"
  fi
  info "Done."
  exit 0
fi

# ---- --apply: before the question it only reads ----------------------------------------

need_tools docker envsubst python3 curl sha256sum base64

ns_found="$(k get namespace "$K8S_NAMESPACE" --ignore-not-found -o name 2>/dev/null)" \
  || die "kubectl cannot reach the cluster of \"$K8S_CONTEXT\". Check the connection (VPN) and run again."
[[ -n "$ns_found" ]] || die "The namespace \"$K8S_NAMESPACE\" does not exist in \"$K8S_CONTEXT\". This script never makes a namespace."
found="$(k get storageclass "$STORAGE_CLASS" --ignore-not-found -o name)" || could_not_look "the storage class $STORAGE_CLASS"
[[ -n "$found" ]] || die "The storage class \"$STORAGE_CLASS\" does not exist (the setting STORAGE_CLASS)."
if [[ -n "$PULL_SECRET" ]]; then
  found="$(k get secret "$PULL_SECRET" --ignore-not-found -o name)" || could_not_look "the pull secret $PULL_SECRET"
  [[ -n "$found" ]] || die "The pull secret \"$PULL_SECRET\" is not in the namespace \"$K8S_NAMESPACE\" (the setting PULL_SECRET)."
fi
# An object with one of our names that this script did not make is never changed.
foreign=()
for object in "${OBJECTS[@]}" "$VOLUME" "$SECRET"; do
  seen="$(owner_of "$object")" || could_not_look "$object"
  if [[ -n "$seen" && "${seen#*|}" != "$APP_NAME" ]]; then foreign+=("$object"); fi
done
[[ ${#foreign[@]} -eq 0 ]] || die "These objects are in the namespace \"$K8S_NAMESPACE\" and were not made by this script: ${foreign[*]}. Nothing was changed."
volume_seen="$(owner_of "$VOLUME")" || could_not_look "$VOLUME"
secret_seen="$(owner_of "$SECRET")" || could_not_look "$SECRET"
if [[ -n "$secret_seen" ]]; then
  # The names of its keys only, never a value.
  # shellcheck disable=SC2016  # a Go template, not a shell expression
  keys="$(k get secret "$COGNEE" -o go-template='{{range $key, $value := .data}}{{$key}}{{"\n"}}{{end}}')" || could_not_look "the keys of the Secret $COGNEE"
  missing=()
  for key in "${SECRET_TOKEN_KEYS[@]}" "${SECRET_HEX_KEYS[@]}"; do
    grep -qxF "$key" <<<"$keys" || missing+=("$key")
  done
  [[ ${#missing[@]} -eq 0 ]] || die "The Secret $COGNEE has no ${missing[*]}. Nothing was changed. To start again from nothing: bash k8s/cognee.sh --delete --with-data (the memory is lost)."
fi

# The registry: we must be logged in (names only are read), and it must answer.
REGISTRY_HOST="${REGISTRY%%/*}"
python3 -I - "${DOCKER_CONFIG:-$HOME/.docker}/config.json" "$REGISTRY_HOST" <<'PY' || die "Docker is not logged in to $REGISTRY_HOST. Run: docker login $REGISTRY_HOST"
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
registry_answer="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$REGISTRY_HOST/v2/" 2>/dev/null || true)"
[[ "$registry_answer" == 200 || "$registry_answer" == 401 ]] \
  || die "The registry https://$REGISTRY_HOST does not answer (got \"$registry_answer\"). Check the connection (VPN) and run again."
# The same files make the same tag: when the registry has it, it is used as it is.
registry_digest() {  # REF -> its digest, or nothing
  local digest
  digest="$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$1" 2>/dev/null || true)"
  if [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then printf '%s' "$digest"; fi
}
DIGEST="$(registry_digest "$IMAGE_REF")"
UI_DIGEST="$(registry_digest "$UI_IMAGE_REF")"

info "Cluster $K8S_CONTEXT, namespace $K8S_NAMESPACE: Cognee $COGNEE_VERSION as $COGNEE (api, mcp, ui)."
if [[ -n "$DIGEST" ]]; then
  info "Image: $IMAGE_REF is in the registry already ($DIGEST): no build."
else
  info "Image: $IMAGE_REF will be built and pushed. With the build cache of an earlier image only its last small layers are new; without it, the first build takes 5 to 15 minutes (torch and the models, about 1.5 GB to download) and the push is about 1.3 GB."
fi
if [[ -n "$UI_DIGEST" ]]; then
  info "Image: $UI_IMAGE_REF is in the registry already ($UI_DIGEST): no build."
else
  info "Image: $UI_IMAGE_REF will be built and pushed (about 3 minutes to build; the push is about 120 MB)."
fi
if [[ -n "$volume_seen" ]]; then
  info "Volume: $COGNEE-data is there already: the memory is kept."
else
  info "Volume: $COGNEE-data is made ($COGNEE_SIZE on $STORAGE_CLASS)."
fi
if [[ -n "$secret_seen" ]]; then
  info "Secret: $COGNEE is there already: kept (the UI password and the MCP's API key stay)."
else
  info "Secret: $COGNEE is made (a new UI password, a new API key for the MCP container, new token keys)."
fi
info "CPU/memory: api $COGNEE_REQ_CPU/$COGNEE_REQ_MEMORY up to $COGNEE_LIM_CPU/$COGNEE_LIM_MEMORY, mcp $COGNEE_MCP_REQ_CPU/$COGNEE_MCP_REQ_MEMORY up to $COGNEE_MCP_LIM_CPU/$COGNEE_MCP_LIM_MEMORY, ui $COGNEE_UI_REQ_CPU/$COGNEE_UI_REQ_MEMORY up to $COGNEE_UI_LIM_CPU/$COGNEE_UI_LIM_MEMORY (first guesses: see bash k8s/cognee.sh --top after the smoke test)."
ask_context "build and push the images (when needed) and put Cognee on that cluster"

BUILD_OPTIONS=()
if [[ -n "${BUILD_NETWORK:-}" ]]; then BUILD_OPTIONS+=(--network "$BUILD_NETWORK"); fi
build_and_push() {  # REF FOLDER WHAT -> the digest in the registry
  info "Image: building $1 ($3)." >&2
  docker build -q --platform linux/amd64 --provenance=false ${BUILD_OPTIONS[@]+"${BUILD_OPTIONS[@]}"} \
    -f "$2/Dockerfile" -t "$1" "$2" >/dev/null \
    || die "Could not build the image $1. If a step that downloads failed, run again with BUILD_NETWORK=host in front. Nothing was changed on the cluster."
  docker push -q "$1" >/dev/null || die "Could not push $1. Run: docker login $REGISTRY_HOST"
  local digest
  digest="$(registry_digest "$1")"
  [[ -n "$digest" ]] || die "Could not read the digest of $1 from the registry."
  info "Image: pushed, $digest" >&2
  printf '%s' "$digest"
}
if [[ -z "$DIGEST" ]]; then DIGEST="$(build_and_push "$IMAGE_REF" "$COGNEE_DIR" "the API server and cognee-mcp")"; fi
if [[ -z "$UI_DIGEST" ]]; then UI_DIGEST="$(build_and_push "$UI_IMAGE_REF" "$UI_DIR" "the web UI")"; fi
COGNEE_IMAGE="$IMAGE_REF@$DIGEST"
COGNEE_UI_IMAGE="$UI_IMAGE_REF@$UI_DIGEST"
export COGNEE_IMAGE COGNEE_UI_IMAGE

# The Secret: made once, never changed. Its values go to kubectl on standard input: never on a
# command line, never on screen, never on disk.
if [[ -z "$secret_seen" ]]; then
  new_secret() {
    python3 -I -c '
import json, secrets, sys
name, namespace, app, tokens, hexes = sys.argv[1:]
values = {key: secrets.token_urlsafe(32) for key in tokens.split()}
values.update({key: secrets.token_hex(32) for key in hexes.split()})
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": name, "namespace": namespace,
                 "labels": {"app.kubernetes.io/part-of": app, "app.kubernetes.io/component": "cognee"}},
    "type": "Opaque",
    "stringData": values,
}))' "$COGNEE" "$K8S_NAMESPACE" "$APP_NAME" "${SECRET_TOKEN_KEYS[*]}" "${SECRET_HEX_KEYS[*]}"
  }
  if ! new_secret | k create -f - >/dev/null; then
    # A create that timed out may still have been done.
    [[ -n "$(owner_of "$SECRET")" ]] || die "The Secret $COGNEE could not be made (the words of kubectl are above). Nothing else was changed on the cluster."
  fi
  info "Secret: $COGNEE is made."
fi
write_ui_password || die "The UI password could not be copied from the Secret $COGNEE to $UI_PASSWORD_FILE (the words of kubectl are above)."
info "UI password: in $UI_PASSWORD_FILE (mode 600)."

info "Apply:"
filled_yaml | k apply -f - | sed 's/^/  /' \
  || die "The files could not be applied (the words of kubectl are above). Run this again."

WAIT="${COGNEE_WAIT_SECONDS:-900}"
[[ "$WAIT" =~ ^[1-9][0-9]{0,4}$ ]] || die "COGNEE_WAIT_SECONDS must be a whole number of seconds."
info "Waiting for the pod (the first start on a node pulls the images): up to $((WAIT / 60)) minutes."
k rollout status "deployment/$COGNEE" --timeout="${WAIT}s" >/dev/null \
  || die "$COGNEE is not running after ${WAIT}s. Look at it: bash k8s/cognee.sh --status (or kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE logs deployment/$COGNEE -c api)"
info "  $COGNEE: running (api, mcp, ui)."
info ""
info "Done. Next:"
info "  bash k8s/cognee.sh --smoke          the API and the UI answer; through MCP made-up data in, recall, code_search, forget; PASS or FAIL"
info "  bash k8s/cognee.sh --top            the real CPU and memory (after the smoke test)"
info "  bash k8s/cognee.sh --port-forward   then open $UI_URL (log in as $UI_USER, password in $UI_PASSWORD_FILE)"
info "                                      and, once, for Claude Code: $CLAUDE_ADD"
