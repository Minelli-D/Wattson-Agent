#!/usr/bin/env bash
# Looks at Wattson Agent on the cluster and prints what it finds. It changes nothing and prints no
# secret. Use it after a deploy, and when the CLI cannot reach the agents.
#
#   bash k8s/doctor.sh
#
# It reads: the pods and their containers, the restarts, the recent events, the end of the
# model download log, the volumes; it asks each agent for /healthz from inside its own pod
# (kubectl exec); and it asks the orchestrator for /v1/health through a short
# kubectl port-forward, with the CLI's token (read from the token file, never shown).
# With MODEL_BACKEND=kubeai it reads the KubeAI Model (its engine: llama.cpp), the network rules
# of KubeAI's namespace, and the model pod
# (state, image) instead of the llama container, checks the commit and the SHA-256 of the model
# file there, and asks the KubeAI proxy for /models from inside the triage pod.
# With TELEMETRY=on it also looks at Tempo (its pod, its volume, /ready through a short
# port-forward), opens the OTLP port of Tempo from inside each agent pod, and asks Grafana
# (GRAFANA_NAMESPACE, with its admin login read from its Secret, never shown) whether the data
# source and the dashboard of Wattson Agent are there: only GET calls.
# The Guard: it checks the rules in k8s/guard/ (as deploy.sh does), reads the ConfigMap of the
# rules and the mode of the running orchestrator, and, when the Guard has a small model
# (GUARD_MODEL_URL, with KubeAI), asks the KubeAI proxy for /models from inside the orchestrator.
# With INJECTION_MODE warn or cut it also looks at the shield (the injection model): its pod, the
# end of its download log, /healthz inside it, the SHA-256 of its three files and the label in
# config.json, and whether the orchestrator can open its port (the network rules).
set -uo pipefail

# shellcheck source=k8s/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
read_settings
check_settings

PROBLEMS=0
problem() {
  PROBLEMS=$((PROBLEMS + 1))
  info "PROBLEM: $*"
}
section() {
  printf '\n== %s ==\n' "$*"
}

# A small Python program that asks one address inside a pod and prints the status and the
# start of the answer. /healthz needs no token.
# A small Python program that opens a TCP connection from inside a pod (DNS and the network
# rules on the way) and closes it again: nothing is sent.
# shellcheck disable=SC2016
CONNECT_INSIDE='import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()
    print("connected")
except Exception as error:
    print("no connection:", error)
    sys.exit(1)'

# A small Python program for the shield's pod: the SHA-256 of its three model files, and the
# label of INJECTION_LABEL in config.json. It prints one line; exit 1 when one is not right.
# shellcheck disable=SC2016
SHIELD_FILES='import hashlib, json, sys
try:
    for name, want in zip(("model.onnx", "tokenizer.json", "config.json"), sys.argv[1:4]):
        digest = hashlib.sha256()
        with open("/models/" + name, "rb") as file:
            for block in iter(lambda: file.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != want:
            print("/models/%s has another SHA-256 than settings.env" % name)
            sys.exit(1)
    labels = [str(v) for v in json.load(open("/models/config.json"))["id2label"].values()]
    if sys.argv[4].lower() not in [v.lower() for v in labels]:
        print("INJECTION_LABEL %s is not a label of config.json (%s)" % (sys.argv[4], ", ".join(labels)))
        sys.exit(1)
    print("the three files have the SHA-256 of settings.env; config.json has the label %s" % sys.argv[4])
except (OSError, ValueError, KeyError, AttributeError) as error:
    print("the model files could not be read:", error)
    sys.exit(1)'

# shellcheck disable=SC2016
ASK_INSIDE='import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as answer:
        print(answer.status, answer.read(300).decode("utf-8", "replace"))
except Exception as error:
    print("no answer:", error)
    sys.exit(1)'

section "Settings"
info "app $APP_NAME, context $K8S_CONTEXT, namespace $K8S_NAMESPACE"
if [[ "$MODEL_BACKEND" == kubeai ]]; then
  info "model served by KubeAI: Model $KUBEAI_MODEL in the namespace $KUBEAI_NAMESPACE, $KUBEAI_BASE_URL, engine llama.cpp, image ${LLAMA_IMAGE%%@*}, file $MODEL_FILE of $MODEL_HF_REPO"
else
  info "model $MODEL_FILE, image of the model server ${LLAMA_IMAGE%%@*}"
fi
if [[ "$TELEMETRY" == on ]]; then
  info "traces on: Tempo $TEMPO (${TEMPO_IMAGE%%@*}), kept $TEMPO_RETENTION; Grafana: $GRAFANA_SERVICE in $GRAFANA_NAMESPACE"
else
  info "traces off (TELEMETRY=off)"
fi
if [[ -n "$GUARD_MODEL_URL" ]]; then
  info "Guard: mode $GUARD_MODE; fixed rules, then the small model $GUARD_MODEL ($GUARD_MODEL_URL), at most ${GUARD_TIMEOUT_S}s"
else
  info "Guard: mode $GUARD_MODE; fixed rules only (GUARD_MODEL_URL is empty)"
fi
if [[ "$INJECTION_MODE" != off ]]; then
  info "injection model: $INJECTION_MODE; $INJECTION_MODEL_REPO ($INJECTION_MODEL_FILE at ${INJECTION_MODEL_COMMIT:0:12}), threshold $INJECTION_THRESHOLD, in the pod $SHIELD"
else
  info "injection model: off (INJECTION_MODE=off): the evidence is checked by the rules of the Guard only"
fi

section "Cluster"
seen_context="$(kubectl --context "$K8S_CONTEXT" config view --minify -o 'jsonpath={.contexts[0].name}' 2>/dev/null || true)"
if [[ "$seen_context" != "$K8S_CONTEXT" ]]; then
  problem "kubectl has no context \"$K8S_CONTEXT\". See: kubectl config get-contexts"
  exit 1
fi
if ! k get namespace "$K8S_NAMESPACE" -o name >/dev/null 2>&1; then
  problem "the cluster of \"$K8S_CONTEXT\" does not answer, or has no namespace \"$K8S_NAMESPACE\". Check the connection (VPN)."
  exit 1
fi
info "ok: the cluster answers and the namespace is there."

section "Pods"
DEPLOYMENTS=("$ORCHESTRATOR" "$TRIAGE" "$KNOWLEDGE")
if [[ "$TELEMETRY" == on ]]; then DEPLOYMENTS+=("$TEMPO"); fi
if [[ "$INJECTION_MODE" != off ]]; then DEPLOYMENTS+=("$SHIELD"); fi
for name in "${DEPLOYMENTS[@]}"; do
  if ! k get deployment "$name" -o name >/dev/null 2>&1; then
    problem "there is no Deployment $name. Run: bash k8s/deploy.sh"
    continue
  fi
  pods="$(k get pods -l "app=$name" --no-headers 2>/dev/null || true)"
  if [[ -z "$pods" ]]; then
    problem "$name has no pod."
    continue
  fi
  printf '%s\n' "$pods"
  # Each container (the init container too): ready or not, restarts, why it waits or stopped.
  k get pods -l "app=$name" -o jsonpath='{range .items[*]}{range .status.initContainerStatuses[*]}{"  init "}{.name}{": restarts="}{.restartCount}{" waiting="}{.state.waiting.reason}{" ended="}{.state.terminated.reason}{"\n"}{end}{range .status.containerStatuses[*]}{"  "}{.name}{": ready="}{.ready}{" restarts="}{.restartCount}{" waiting="}{.state.waiting.reason}{" last="}{.lastState.terminated.reason}{"\n"}{end}{end}' 2>/dev/null || true
  k get deployment "$name" -o jsonpath='{range .spec.template.spec.containers[*]}{"  image "}{.name}{": "}{.image}{"\n"}{end}' 2>/dev/null || true
done

section "Volumes"
CLAIMS=("$APP_NAME-data" "$APP_NAME-knowledge-data")
if [[ "$MODEL_BACKEND" == sidecar ]]; then
  CLAIMS+=("$APP_NAME-models")
elif k get pvc "$APP_NAME-models" -o name >/dev/null 2>&1; then
  info "note: $APP_NAME-models (the model file of the sidecar) is not used with KubeAI. bash k8s/remove.sh --delete-data removes it with the other volumes."
fi
if [[ "$TELEMETRY" == on ]]; then
  CLAIMS+=("$APP_NAME-tempo-data")
elif k get pvc "$APP_NAME-tempo-data" -o name >/dev/null 2>&1; then
  info "note: $APP_NAME-tempo-data (the traces) is not used with TELEMETRY=off. bash k8s/remove.sh --delete-data removes it with the other volumes."
fi
for claim in "${CLAIMS[@]}"; do
  phase="$(k get pvc "$claim" -o 'jsonpath={.status.phase} {.status.capacity.storage} {.spec.storageClassName}' 2>/dev/null || true)"
  case "$phase" in
    Bound*) info "ok: $claim: $phase" ;;
    "") problem "there is no volume claim $claim. Run: bash k8s/deploy.sh" ;;
    *) problem "$claim is not bound: $phase. Look at: kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE describe pvc $claim" ;;
  esac
done

if [[ "$MODEL_BACKEND" == sidecar ]]; then
  section "Model download (the init container of $TRIAGE, last lines)"
  k logs "deployment/$TRIAGE" -c fetch-model --tail=10 2>&1 || info "(no log yet)"
fi
if [[ "$INJECTION_MODE" != off ]]; then
  section "The injection model's download (the init container of $SHIELD, last lines)"
  k logs "deployment/$SHIELD" -c fetch-model --tail=6 2>&1 || info "(no log yet)"
fi

section "Recent events of Wattson Agent"
events="$(k get events --sort-by=.lastTimestamp \
  -o jsonpath='{range .items[*]}{.lastTimestamp}{" "}{.type}{" "}{.involvedObject.kind}{"/"}{.involvedObject.name}{": "}{.reason}{": "}{.message}{"\n"}{end}' 2>/dev/null \
  | grep -E "/$APP_NAME(-[a-z0-9-]+)?: " | tail -n 15 || true)"
if [[ -n "$events" ]]; then printf '%s\n' "$events" | cut -c1-220; else info "(none)"; fi

section "Health, asked inside each pod"
# The orchestrator listens on 127.0.0.1: only a program inside its pod can ask it.
inside() {  # DEPLOYMENT CONTAINER URL
  local answer
  if answer="$(k exec "deployment/$1" -c "$2" -- /app/.venv/bin/python -c "$ASK_INSIDE" "$3" 2>&1)"; then
    info "ok: $1 ($2) $3 -> $answer"
  else
    problem "$1 ($2) $3 -> $(tail -n 1 <<<"$answer")"
  fi
}
inside "$ORCHESTRATOR" orchestrator "http://127.0.0.1:$ORCHESTRATOR_PORT/healthz"
inside "$TRIAGE" triage "http://127.0.0.1:$TRIAGE_PORT/healthz"
inside "$KNOWLEDGE" knowledge "http://127.0.0.1:$KNOWLEDGE_PORT/healthz"
if [[ "$MODEL_BACKEND" == sidecar ]]; then
  # The model server has curl.
  if answer="$(k exec "deployment/$TRIAGE" -c llama -- curl -sS -m 5 "http://127.0.0.1:$LLAMA_PORT/health" 2>&1)"; then
    info "ok: $TRIAGE (llama) /health -> $answer"
  else
    problem "$TRIAGE (llama) /health -> $(tail -n 1 <<<"$answer"). The triage agent then answers with its keyword rules only."
  fi
else
  # The KubeAI proxy, asked from inside the triage pod: this also proves the network rule.
  inside "$TRIAGE" triage "$KUBEAI_BASE_URL/models"
fi
if [[ -n "$GUARD_MODEL_URL" ]]; then
  # The small model of the Guard: the orchestrator asks the KubeAI proxy too. This proves its own
  # network rule and the rule of KubeAI's namespace that lets it in (k8s/kubeai/networkpolicy.yaml).
  if answer="$(k exec "deployment/$ORCHESTRATOR" -c orchestrator -- /app/.venv/bin/python -c "$ASK_INSIDE" "$GUARD_MODEL_URL/models" 2>&1)"; then
    info "ok: $ORCHESTRATOR reaches KubeAI for the Guard: $GUARD_MODEL_URL/models -> $answer"
  else
    problem "$ORCHESTRATOR cannot reach KubeAI for the Guard: $GUARD_MODEL_URL/models -> $(tail -n 1 <<<"$answer"). The Guard then uses its rules only. Run: bash k8s/kubeai.sh --apply (its network rule lets the orchestrator in), then bash k8s/deploy.sh"
  fi
fi

if [[ "$INJECTION_MODE" != off ]]; then
  # The shield answers /healthz once its model is loaded; the orchestrator must reach its port.
  inside "$SHIELD" shield "http://127.0.0.1:$SHIELD_PORT/healthz"
  if answer="$(k exec "deployment/$ORCHESTRATOR" -c orchestrator -- /app/.venv/bin/python -c "$CONNECT_INSIDE" "$SHIELD" "$SHIELD_PORT" 2>&1)"; then
    info "ok: $ORCHESTRATOR reaches $SHIELD:$SHIELD_PORT (the injection model)"
  else
    problem "$ORCHESTRATOR cannot reach $SHIELD:$SHIELD_PORT: $(tail -n 1 <<<"$answer"). The Guard then checks the evidence with its rules only. Run: bash k8s/deploy.sh"
  fi
fi

section "The Guard"
info "mode in settings.env: $GUARD_MODE (enforce: it can stop tasks; monitor: it only records; off)"
# The rules on this laptop: the checks of deploy.sh, in a subshell (a wrong file is a PROBLEM here).
if guard_checked="$(check_guard_files 2>&1 && printf '%s\n' "$GUARD_CHECK_NOTES")"; then
  info "ok: the rules in $GUARD_DIR pass the checks: $(sed -n 's/^NOTE: //p' <<<"$guard_checked" | paste -sd ';' - | sed 's/;/; /g')"
  local_rules="$(guard_config_sha256)"
else
  reasons="$(sed -n 's/^PROBLEM: //p' <<<"$guard_checked" | head -n 3 | paste -sd ';' - | sed 's/;/; /g')"
  [[ -n "$reasons" ]] || reasons="$(sed -n 's/^ERROR: //p' <<<"$guard_checked" | head -n 1)"
  problem "the rules in $GUARD_DIR are not right: $reasons. Fix them, then: bash k8s/guard.sh --apply"
  local_rules=""
fi
if k get configmap "$GUARD_CONFIGMAP" -o name >/dev/null 2>&1; then
  info "ok: the ConfigMap $GUARD_CONFIGMAP (the rules of the Guard) is there."
else
  problem "there is no ConfigMap $GUARD_CONFIGMAP: the orchestrator has no rules. Run: bash k8s/deploy.sh"
fi
running_rules="$(k get deployment "$ORCHESTRATOR" -o 'jsonpath={.spec.template.metadata.annotations.wattson/guard-config}' 2>/dev/null || true)"
if [[ -n "$local_rules" && "$running_rules" == "$local_rules" ]]; then
  info "ok: the orchestrator runs the rules of $GUARD_DIR."
elif [[ -n "$local_rules" ]]; then
  info "note: the orchestrator does not run the rules of $GUARD_DIR as they are now (they changed after the last deploy). To put them there: bash k8s/guard.sh --apply"
fi
running_mode="$(k get deployment "$ORCHESTRATOR" -o 'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="GUARD_MODE")].value}' 2>/dev/null || true)"
if [[ -z "$running_mode" ]]; then
  problem "the orchestrator on the cluster has no GUARD_MODE: it was deployed before the Guard. Run: bash k8s/deploy.sh"
elif [[ "$running_mode" == "$GUARD_MODE" ]]; then
  info "ok: the orchestrator runs in mode $running_mode."
else
  info "note: the orchestrator runs in mode $running_mode, and settings.env says $GUARD_MODE. To change it: bash k8s/deploy.sh"
fi
running_injection="$(k get deployment "$ORCHESTRATOR" -o 'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="INJECTION_MODE")].value}' 2>/dev/null || true)"
if [[ "${running_injection:-off}" == "$INJECTION_MODE" ]]; then
  info "ok: the injection model runs in mode $INJECTION_MODE."
else
  info "note: the orchestrator runs the injection model in mode ${running_injection:-off}, and settings.env says $INJECTION_MODE. To change it: bash k8s/deploy.sh"
fi
if [[ "$INJECTION_MODE" != off ]]; then
  # The files the shield reads: the init container checked them; checked again here, in the pod.
  if answer="$(k exec "deployment/$SHIELD" -c shield -- /app/.venv/bin/python -c "$SHIELD_FILES" \
    "$INJECTION_MODEL_SHA256" "$INJECTION_TOKENIZER_SHA256" "$INJECTION_CONFIG_SHA256" "$INJECTION_LABEL" 2>&1)"; then
    info "ok: $SHIELD: $(tail -n 1 <<<"$answer")"
  else
    problem "$SHIELD: $(tail -n 1 <<<"$answer"). A new pod downloads the files of settings.env again: kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE rollout restart deployment/$SHIELD"
  fi
fi

if [[ "$MODEL_BACKEND" == kubeai ]]; then
  section "KubeAI (namespace $KUBEAI_NAMESPACE)"
  if kubeai_k get service "$KUBEAI_SERVICE" -o name >/dev/null 2>&1; then
    info "ok: the proxy Service $KUBEAI_SERVICE is there."
  else
    problem "there is no Service $KUBEAI_SERVICE in $KUBEAI_NAMESPACE: KubeAI is not installed. Run: bash k8s/kubeai.sh --apply"
  fi
  # The network rules of k8s/kubeai/networkpolicy.yaml: without them any pod may ask the model.
  missing_policies=()
  for policy in "${KUBEAI_POLICIES[@]}"; do
    kubeai_k get networkpolicy "$policy" -o name >/dev/null 2>&1 || missing_policies+=("$policy")
  done
  if [[ ${#missing_policies[@]} -eq 0 ]]; then
    info "ok: the network rules ${KUBEAI_POLICIES[*]} are there (only the triage and orchestrator pods may ask KubeAI)."
  else
    problem "the network rules ${missing_policies[*]} are not in $KUBEAI_NAMESPACE: any pod of the cluster may ask the model. Run: bash k8s/kubeai.sh --apply"
  fi
  model="$(kubeai_k get models.kubeai.org "$KUBEAI_MODEL" \
    -o 'jsonpath=engine={.spec.engine} {.spec.url} {.spec.resourceProfile} minReplicas={.spec.minReplicas} scaleDownDelaySeconds={.spec.scaleDownDelaySeconds}' 2>&1)"
  if [[ "$model" == *"minReplicas="* ]]; then
    info "Model $KUBEAI_MODEL: $model"
    # The engine of k8s/kubeai/granite-model.yaml: the llama.cpp server, with the flags measured.
    if [[ "$model" != "engine=LlamaCpp "* ]]; then
      problem "the Model $KUBEAI_MODEL does not run on KubeAI's llama.cpp engine (${model%% *}). Run: bash k8s/kubeai.sh --apply"
    fi
    # At 0 (no pod, no CPU reserved) or running: the replicas KubeAI wants, and those ready.
    replicas="$(kubeai_k get models.kubeai.org "$KUBEAI_MODEL" -o 'jsonpath={.spec.replicas}/{.status.replicas.ready}' 2>/dev/null || true)"
    wanted="${replicas%%/*}" ready="${replicas#*/}"
    if [[ "${wanted:-0}" == 0 ]]; then
      info "model: at 0 (idle, no pod, no CPU reserved). The next triage starts it: about a minute (the model download), the first time on a node also the llama.cpp image."
    elif [[ "${ready:-0}" -ge 1 ]]; then
      info "model: running ($ready pod ready). It goes to 0 about ${KUBEAI_SCALE_DOWN_DELAY_SECONDS}s after the last request."
    else
      info "model: starting (the pod downloads the model; 0 of $wanted ready)."
    fi
  else
    problem "the KubeAI Model $KUBEAI_MODEL cannot be read: $(tail -n 1 <<<"$model"). Run: bash k8s/kubeai.sh --apply"
  fi
  # The model pods carry the label model=<Model name>.
  model_pods="$(kubeai_k get pods -l "model=$KUBEAI_MODEL" --no-headers 2>/dev/null || true)"
  if [[ -z "$model_pods" ]]; then
    if [[ "$KUBEAI_MIN_REPLICAS" -ge 1 ]]; then
      problem "the Model $KUBEAI_MODEL has no pod, and KUBEAI_MIN_REPLICAS is $KUBEAI_MIN_REPLICAS. Look at: kubectl --context $K8S_CONTEXT -n $KUBEAI_NAMESPACE describe model $KUBEAI_MODEL"
    fi
  else
    printf '%s\n' "$model_pods"
    kubeai_k get pods -l "model=$KUBEAI_MODEL" -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{"  "}{.name}{": ready="}{.ready}{" restarts="}{.restartCount}{" waiting="}{.state.waiting.reason}{" last="}{.lastState.terminated.reason}{"\n"}{end}{end}' 2>/dev/null || true
    model_pod="$(kubeai_k get pods -l "model=$KUBEAI_MODEL" -o 'jsonpath={.items[0].metadata.name}' 2>/dev/null || true)"
    # The image of its llama.cpp server: LLAMA_IMAGE (the Helm values of k8s/kubeai.sh).
    pod_image="$(kubeai_k get pod "$model_pod" -o 'jsonpath={.spec.containers[0].image}' 2>/dev/null || true)"
    if [[ "$pod_image" == "$LLAMA_IMAGE" ]]; then
      info "ok: $model_pod runs the pinned llama.cpp image (${LLAMA_IMAGE%%@*})."
    else
      problem "$model_pod runs the image \"$pod_image\", not LLAMA_IMAGE. Run: bash k8s/kubeai.sh --apply"
    fi
    # The model file in the pod: llama.cpp keeps it as <cache>/models--<user>--<repo>/snapshots/
    # <commit>/<file>. It must be at the commit of MODEL_URL and have the SHA-256 of MODEL_SHA256.
    # llama.cpp downloads the repo's "main" branch: when IBM moves it, the commit is another.
    repo_dir="/llama-cache/models--${MODEL_HF_REPO//\//--}"
    pod_ready="$(kubeai_k get pod "$model_pod" -o 'jsonpath={.status.containerStatuses[0].ready}' 2>/dev/null || true)"
    if sum="$(kubeai_k exec "$model_pod" -c server -- sha256sum "$repo_dir/snapshots/$MODEL_HF_COMMIT/$MODEL_FILE" 2>&1)" \
      && [[ "${sum:0:64}" =~ ^[0-9a-f]{64}$ ]]; then
      if [[ "${sum:0:64}" == "$MODEL_SHA256" ]]; then
        info "ok: the model file in $model_pod is $MODEL_FILE at the commit of MODEL_URL, with the SHA-256 of MODEL_SHA256."
      else
        problem "the model file in $model_pod has the SHA-256 ${sum:0:64}, not MODEL_SHA256. Do not trust its answers; ask why the file changed."
      fi
    elif [[ "$pod_ready" == true ]]; then
      commits="$(kubeai_k exec "$model_pod" -c server -- ls "$repo_dir/snapshots" 2>&1 | tr '\n' ' ')"
      problem "the model pod is ready, but has no $MODEL_FILE of the commit $MODEL_HF_COMMIT (MODEL_URL); it has: ${commits:-nothing}. The \"main\" branch of $MODEL_HF_REPO has moved: check the new file, then set MODEL_URL and MODEL_SHA256."
    else
      info "(the model file in $model_pod is not there yet: the pod is still downloading it)"
    fi
  fi
fi

PORT_FORWARD_PID=""
PORT_FORWARD_LOG="$(mktemp)"
GRAFANA_FORWARD_LOG="$(mktemp)"
cleanup() {
  if [[ -n "$PORT_FORWARD_PID" ]]; then kill "$PORT_FORWARD_PID" 2>/dev/null || true; fi
  grafana_stop
  GRAFANA_LOGIN=""
  rm -f "$PORT_FORWARD_LOG" "$GRAFANA_FORWARD_LOG" "${GRAFANA_ANSWER:-}"
}
trap cleanup EXIT

# A short kubectl port-forward to a Service or Deployment of the namespace, on a free port of
# 127.0.0.1, in the background. Sets PORT_FORWARD_PID and LOCAL_PORT; stop_forward ends it.
start_forward() {  # TARGET REMOTE_PORT
  local _
  LOCAL_PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  kubectl --context "$K8S_CONTEXT" -n "$K8S_NAMESPACE" port-forward --address 127.0.0.1 "$1" "$LOCAL_PORT:$2" >"$PORT_FORWARD_LOG" 2>&1 &
  PORT_FORWARD_PID=$!
  for _ in {1..50}; do
    grep -q '^Forwarding from' "$PORT_FORWARD_LOG" 2>/dev/null && return 0
    kill -0 "$PORT_FORWARD_PID" 2>/dev/null || return 1
    sleep 0.3
  done
  return 1
}
stop_forward() {
  if [[ -n "$PORT_FORWARD_PID" ]]; then kill "$PORT_FORWARD_PID" 2>/dev/null || true; fi
  PORT_FORWARD_PID=""
}

if [[ "$TELEMETRY" == on ]]; then
  section "Traces: Tempo"
  # Tempo's image has no shell: /ready is asked through a short port-forward.
  if ! k get service "$TEMPO" -o name >/dev/null 2>&1; then
    problem "there is no Service $TEMPO. Run: bash k8s/deploy.sh"
  elif ! start_forward "service/$TEMPO" "$TEMPO_HTTP_PORT"; then
    problem "kubectl port-forward to service/$TEMPO did not start: $(tail -n 2 "$PORT_FORWARD_LOG" | tr '\n' ' ')"
  else
    ready="$(curl -sS -m 10 "http://127.0.0.1:$LOCAL_PORT/ready" 2>&1 || true)"
    if [[ "$ready" == ready* ]]; then
      info "ok: $TEMPO /ready -> ready"
    else
      problem "$TEMPO /ready -> $(tail -n 1 <<<"$ready"). Look at: kubectl --context $K8S_CONTEXT -n $K8S_NAMESPACE logs deployment/$TEMPO"
    fi
  fi
  stop_forward
  # Each agent opens the OTLP port of Tempo: its address and the network rules on the way.
  for pair in "$ORCHESTRATOR:orchestrator" "$TRIAGE:triage" "$KNOWLEDGE:knowledge"; do
    name="${pair%%:*}" container="${pair##*:}"
    if answer="$(k exec "deployment/$name" -c "$container" -- /app/.venv/bin/python -c "$CONNECT_INSIDE" "$TEMPO" "$TEMPO_OTLP_PORT" 2>&1)"; then
      info "ok: $name reaches $TEMPO:$TEMPO_OTLP_PORT (OTLP)"
    else
      problem "$name cannot reach $TEMPO:$TEMPO_OTLP_PORT: $(tail -n 1 <<<"$answer"). Its traces are lost; the tasks still work."
    fi
  done

  section "Traces: Grafana ($GRAFANA_SERVICE in $GRAFANA_NAMESPACE)"
  # Only GET calls. Grafana keeps what k8s/grafana.sh added in an emptyDir: a restart loses it.
  login_status=0
  grafana_read_login || login_status=$?
  GRAFANA_ANSWER="$(mktemp)"
  if [[ "$login_status" -eq 2 ]]; then
    problem "the Deployment $GRAFANA_SERVICE does not take its admin login from a Secret: the data source and the dashboard cannot be checked."
  elif [[ "$login_status" -ne 0 ]]; then
    problem "the admin login of Grafana could not be read from its Secret in \"$GRAFANA_NAMESPACE\": the data source and the dashboard cannot be checked."
  elif ! grafana_forward "$GRAFANA_FORWARD_LOG"; then
    problem "kubectl port-forward to service/$GRAFANA_SERVICE in $GRAFANA_NAMESPACE did not start: $(tail -n 2 "$GRAFANA_FORWARD_LOG" | tr '\n' ' ')"
  else
    status="$(grafana_call GET "/api/datasources/uid/$GRAFANA_DATASOURCE_UID" "$GRAFANA_ANSWER")"
    case "$status" in
      200)
        info "ok: the data source $GRAFANA_DATASOURCE_NAME ($GRAFANA_DATASOURCE_UID) is there."
        status="$(grafana_call GET "/api/datasources/uid/$GRAFANA_DATASOURCE_UID/health" "$GRAFANA_ANSWER")"
        if [[ "$status" == 200 ]]; then
          info "ok: Grafana reaches Tempo ($(grafana_message "$GRAFANA_ANSWER"))."
        else
          problem "Grafana cannot reach Tempo (status $status: $(grafana_message "$GRAFANA_ANSWER")). Check the pod $TEMPO above, and its network rule (namespace $GRAFANA_NAMESPACE, port $TEMPO_HTTP_PORT)."
        fi
        ;;
      404) problem "the data source $GRAFANA_DATASOURCE_UID is not in Grafana (it keeps its data in an emptyDir: a restart loses it). Run: bash k8s/grafana.sh --apply" ;;
      *) problem "Grafana answered $status for the data source: $(grafana_message "$GRAFANA_ANSWER")" ;;
    esac
    status="$(grafana_call GET "/api/dashboards/uid/$GRAFANA_DASHBOARD_UID" "$GRAFANA_ANSWER")"
    case "$status" in
      200) info "ok: the dashboard $GRAFANA_DASHBOARD_TITLE ($GRAFANA_DASHBOARD_UID) is there." ;;
      404) problem "the dashboard $GRAFANA_DASHBOARD_UID is not in Grafana (it keeps its data in an emptyDir: a restart loses it). Run: bash k8s/grafana.sh --apply" ;;
      *) problem "Grafana answered $status for the dashboard: $(grafana_message "$GRAFANA_ANSWER")" ;;
    esac
  fi
  grafana_stop
fi

section "Network"
NETWORK_RULES=("$ORCHESTRATOR" "$TRIAGE" "$KNOWLEDGE")
if [[ "$TELEMETRY" == on ]]; then NETWORK_RULES+=("$TEMPO"); fi
if [[ "$INJECTION_MODE" != off ]]; then NETWORK_RULES+=("$SHIELD"); fi
for name in "${NETWORK_RULES[@]}"; do
  if k get networkpolicy "$name" -o name >/dev/null 2>&1; then info "ok: network rule $name"; else problem "there is no NetworkPolicy $name. Run: bash k8s/deploy.sh"; fi
done
if k get service "$ORCHESTRATOR" -o name >/dev/null 2>&1; then
  problem "there is a Service $ORCHESTRATOR: the orchestrator must have none (docs/design.md 10.3)."
else
  info "ok: the orchestrator has no Service."
fi
ingresses="$(k get ingress -l "app.kubernetes.io/part-of=$APP_NAME" -o name 2>/dev/null || true)"
if [[ -n "$ingresses" ]]; then problem "Wattson Agent must have no Ingress, and has: $ingresses"; else info "ok: no Ingress."; fi

section "The CLI on this laptop"
CONFIG_DIR="$(cli_config_dir)"
TOKEN_FILE="${WATTSON_TOKEN_FILE:-$CONFIG_DIR/token}"
if [[ -f "$CONFIG_DIR/config.json" ]]; then
  info "$CONFIG_DIR/config.json: $(tr -d '\n' <"$CONFIG_DIR/config.json" | tr -s ' ')"
else
  problem "$CONFIG_DIR/config.json is missing. Run: bash k8s/deploy.sh"
fi
token_mode="$(stat -c '%a' "$TOKEN_FILE" 2>/dev/null || true)"
if [[ -z "$token_mode" ]]; then
  problem "the token file $TOKEN_FILE is missing. Run: bash k8s/deploy.sh"
elif [[ "$token_mode" != 600 && "$token_mode" != 400 ]]; then
  problem "the token file $TOKEN_FILE has mode $token_mode: the CLI refuses it. Fix: chmod 600 $TOKEN_FILE"
else
  info "ok: the token file $TOKEN_FILE has mode $token_mode."
fi

section "The orchestrator through port-forward (/v1/health, with the CLI's token)"
if [[ -z "$token_mode" ]]; then
  info "(skipped: no token file)"
else
  local_port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  kubectl --context "$K8S_CONTEXT" -n "$K8S_NAMESPACE" port-forward --address 127.0.0.1 "deployment/$ORCHESTRATOR" "$local_port:$ORCHESTRATOR_PORT" >"$PORT_FORWARD_LOG" 2>&1 &
  PORT_FORWARD_PID=$!
  # The program reads the token from its file and sends it; only the answer is printed.
  if answer="$(python3 - "$local_port" "$TOKEN_FILE" <<'PY' 2>&1
import json, socket, sys, time, urllib.error, urllib.request
port, token_file = int(sys.argv[1]), sys.argv[2]
deadline = time.time() + 15
while True:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        break
    except OSError:
        if time.time() > deadline:
            print("the port-forward did not open in 15 seconds")
            sys.exit(1)
        time.sleep(0.3)
with open(token_file, encoding="utf-8") as file:
    token = file.read().strip()
request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/health", headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(request, timeout=20) as answer:
        body = answer.read(4000).decode("utf-8", "replace")
        try:
            body = json.dumps(json.loads(body), indent=2)
        except ValueError:
            pass
        print(answer.status, body)
except urllib.error.HTTPError as error:
    print(error.code, "the token was refused" if error.code == 401 else error.reason)
    sys.exit(1)
except Exception as error:
    print("no answer:", error)
    sys.exit(1)
PY
  )"; then
    info "ok: $answer"
  else
    problem "/v1/health -> $answer"
    if [[ "$answer" == 401* ]]; then info "  The token file does not match the Secret $APP_NAME: run bash k8s/deploy.sh (it writes the file again)."; fi
    if [[ -s "$PORT_FORWARD_LOG" ]]; then info "  kubectl port-forward said: $(tail -n 2 "$PORT_FORWARD_LOG" | tr '\n' ' ')"; fi
  fi
fi

section "Result"
if [[ "$PROBLEMS" -eq 0 ]]; then
  info "No problem found."
else
  info "$PROBLEMS problem(s) found: see the lines that start with PROBLEM."
  exit 1
fi
