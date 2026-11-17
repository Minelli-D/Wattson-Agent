#!/usr/bin/env bash
# The Kubernetes files of wattson (k8s/, settings.env, Dockerfile): the scripts can be read,
# the settings are checked, and k8s/wattson.yaml, filled in the way k8s/deploy.sh fills it, is
# what docs/design.md asks for (no Service and no Ingress for the orchestrator, no token in any
# pod, locked containers with limits, images pinned by digest, the network rules).
#
# Then k8s/deploy.sh, k8s/doctor.sh, k8s/remove.sh, k8s/kubeai.sh and k8s/grafana.sh run from
# their first line to their last against stand-ins for kubectl, docker, curl and helm that keep
# a made-up namespace (and a made-up Grafana) and write down every call. The deploy runs in a
# copy of the repository that is a fresh git repo. The Grafana dashboard
# (k8s/grafana/wattson-dashboard.json) is checked against the span names and attributes the
# code sends.
#
# At the end (unless SKIP_DOCKER=1) the real image is built once, without pushing, and checked
# inside: user 10001, read-only root, the imports. The model download script runs in its
# pinned image, and Tempo in its pinned image with the config of the manifests: one span goes in
# over OTLP and comes back through TraceQL. They need Docker; the download of a small file
# needs the internet (skipped when it is not there).
#
# This test talks to no cluster and writes no file outside a temp folder.
#
#   bash tests/k8s/test-k8s-files.sh
#   SKIP_DOCKER=1 bash tests/k8s/test-k8s-files.sh     (no Docker part)
# shellcheck disable=SC2016  # the many bash -c '...' checks expand their own $1, $2 inside
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 2

PASSED=0
FAILED=0
SKIPPED=0
ok() { PASSED=$((PASSED + 1)); printf '  ok   %s\n' "$1"; }
fail() { FAILED=$((FAILED + 1)); printf '  FAIL %s\n' "$1"; }
skip() { SKIPPED=$((SKIPPED + 1)); printf '  skip %s\n' "$1"; }
check() {  # DESCRIPTION COMMAND...
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then ok "$name"; else fail "$name"; fi
}
not() { ! "$@"; }
section() { printf '\n%s\n' "$*"; }

for tool in python3 envsubst git awk base64; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool is needed for this test." >&2; exit 2; }
done
python3 -c 'import yaml' 2>/dev/null || { echo "The Python module yaml (PyYAML) is needed for this test." >&2; exit 2; }

WORK="$(mktemp -d)"
TEMPO_CONTAINER=""
cleanup() {
  # Port-forward stand-ins that a failed doctor run may have left, and the Tempo container.
  pkill -f "$WORK/bin/kubectl" 2>/dev/null || true
  if [[ -n "$TEMPO_CONTAINER" ]]; then docker rm -f "$TEMPO_CONTAINER" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

SCRIPTS=(k8s/lib.sh k8s/deploy.sh k8s/doctor.sh k8s/remove.sh k8s/remove-old-myagent.sh k8s/kubeai.sh k8s/grafana.sh k8s/guard.sh tests/k8s/test-k8s-files.sh)

# The rules of the Guard for the whole test: two small files of our own in $WORK/guard (GUARD_DIR),
# checked with python3 (GUARD_PYTHON): the results do not hang on k8s/guard/ or on the code of the
# Guard. Their own checks are below ("The rules of the Guard").
export GUARD_DIR="$WORK/guard" GUARD_PYTHON=python3
mkdir -p "$GUARD_DIR"
cat >"$GUARD_DIR/rules.yaml" <<'RULES'
# The rules of the Guard, for the test. A ${APP_NAME} here stays as it is (no envsubst).
version: 1
rules:
  - name: delete-things
    label: risky
    applies_to: request
    match: ["delete", "drop table"]
    regex: ['\brm\s+-rf\b']
    unless: ["why did"]
    reason: It asks to delete something.
    enabled: true
    examples: ["delete the pods in web", "rm -rf /tmp/x"]
    not_examples: ["why did delete fail", "the pod was deleted"]
  - name: ignore-instructions
    label: injection
    applies_to: evidence
    regex: ['ignore (all|previous) instructions']
    reason: The text tries to give the agent new orders.
    enabled: false
RULES
cat >"$GUARD_DIR/policies.cedar" <<'CEDAR'
// The Cedar policies of the Guard, for the test.
forbid(principal, action == Action::"run", resource) when { context.label == "malicious" };
CEDAR

# ---- the scripts ----------------------------------------------------------------------
section "The scripts"

for script in "${SCRIPTS[@]}"; do
  check "$script is valid bash" bash -n "$script"
done
if command -v shellcheck >/dev/null 2>&1; then
  for script in "${SCRIPTS[@]}"; do
    check "$script passes shellcheck" shellcheck -x "$script"
  done
else
  skip "shellcheck is not installed"
fi

# A deploy to the wrong cluster must not be possible by accident: every kubectl call names the
# context of the settings.
check "every kubectl call in k8s/ names the context of the settings" \
  bash -c '! grep -hE "(^|[^A-Za-z\"])kubectl " k8s/*.sh \
    | grep -Ev -- "--context|^[[:space:]]*#|for tool in|command -v kubectl|(die|info|problem) \"|printf " | grep -q .'
check "the scripts read settings.env, they do not run it" not grep -Eq '^\s*(\.|source) .*settings\.env' k8s/*.sh
check "no script makes a namespace" not grep -Eq 'create (namespace|ns)\b' k8s/*.sh
check "doctor.sh changes nothing (no apply, create, delete, patch, edit, scale, rollout)" \
  not grep -Eq '(^|[[:space:]])k (apply|create|delete|patch|edit|scale|replace|label|annotate|rollout)|kubectl .*(apply|create|delete|patch) ' k8s/doctor.sh
check "no script prints a value of the Secret" not grep -Eq '(info|printf|echo)[^|]*secret_value' k8s/*.sh
check "the manifests have no Ingress at all" not grep -Eq '^kind: Ingress' k8s/wattson.yaml
check "the manifests hold no Secret (deploy.sh makes it once, from random bytes)" not grep -Eq '^kind: Secret' k8s/wattson.yaml
check "the image tag comes from the commit" grep -q 'rev-parse --short=12 HEAD' k8s/deploy.sh

# The names filled in: each ${NAME} of wattson.yaml is in YAML_NAMES of deploy.sh, and back.
check "deploy.sh fills in exactly the names that wattson.yaml uses" bash -c '
  used="$(grep -v "^ *#" k8s/wattson.yaml | grep -oE "\\\$\{[A-Z0-9_]+\}" | sort -u)"
  listed="$(sed -n "s/^YAML_NAMES=\x27\(.*\)\x27\$/\1/p" k8s/deploy.sh | tr " " "\n" | sort -u)"
  test -n "$used" && test "$used" = "$listed"'
check "kubeai.sh fills in exactly the names that k8s/kubeai/*.yaml use" bash -c '
  used="$(cat k8s/kubeai/*.yaml | grep -v "^ *#" | grep -oE "\\\$\{[A-Z0-9_]+\}" | sort -u)"
  listed="$(sed -n "s/^KUBEAI_YAML_NAMES=\x27\(.*\)\x27\$/\1/p" k8s/kubeai.sh | tr " " "\n" | sort -u)"
  test -n "$used" && test "$used" = "$listed"'
check "wattson.yaml: every \"# begin: sidecar|kubeai|telemetry|shield|cognee\" has its \"# end:\", none inside another" python3 -c '
import re, sys
open_block = None
for number, line in enumerate(open("k8s/wattson.yaml"), 1):
    mark = re.match(r"^ *# (begin|end): (\S+)", line)
    if not mark:
        continue
    kind, backend = mark.groups()
    if backend not in ("sidecar", "kubeai", "telemetry", "shield", "cognee"):
        sys.exit("line %d: unknown block %s" % (number, backend))
    if kind == "begin":
        if open_block:
            sys.exit("line %d: a block inside another" % number)
        open_block = backend
    else:
        if open_block != backend:
            sys.exit("line %d: an end without its begin" % number)
        open_block = None
if open_block:
    sys.exit("a block is not closed")'
check "deploy.sh never installs KubeAI (no helm, no apply in the KubeAI namespace)" not grep -Eq '(^|[[:space:]])helm |kubeai_k (apply|create|delete|patch)' k8s/deploy.sh
check "kubeai.sh: every helm call names the context of the settings" bash -c '
  ! grep -hE "(^|[^A-Za-z\"])helm (upgrade|install|uninstall|rollback)" k8s/kubeai.sh | grep -Ev "^[[:space:]]*#|info \"" | grep -v -- "--kube-context \"\$K8S_CONTEXT\"" | grep -q .'
check "kubeai.sh pins the chart: a version and a SHA-256" bash -c 'grep -q "^CHART_VERSION=[0-9]" k8s/kubeai.sh && grep -Eq "^CHART_SHA256=.*[0-9a-f]{64}" k8s/kubeai.sh'
# Grafana's admin login: never on a command line. Every curl call to Grafana takes it on its
# standard input (--config -); no curl call anywhere has -u/--user or an Authorization header.
check "the scripts call Grafana only through grafana_call (curl --config -, the login on standard input)" bash -c '
  grep -q "curl --config - " k8s/lib.sh && ! grep -hE "(^|[^a-z_])curl " k8s/*.sh | grep -Ev "^[[:space:]]*#|info \"" | grep -Eq -- "(^|[[:space:]])(-u|--user)[[:space:]]|Authorization" &&
  ! grep -nE "GRAFANA_URL" k8s/grafana.sh k8s/doctor.sh | grep -q curl'
check "doctor.sh only reads Grafana (GET calls)" bash -c '
  calls="$(grep -oE "grafana_call [A-Z]+" k8s/doctor.sh | sort -u)"; [[ "$calls" == "grafana_call GET" ]]'
check "grafana.sh changes nothing in Kubernetes (no apply, create, delete, patch, rollout, helm)" bash -c '
  ! grep -v "^[[:space:]]*#" k8s/grafana.sh | grep -Eq "(^|[[:space:]])(k|grafana_k) (apply|create|delete|patch|edit|scale|replace|label|annotate|rollout)|kubectl .*(apply|create|delete|patch|rollout) |(^|[[:space:]])helm "'
check "grafana.sh asks for the typed context before it reads the login or calls Grafana" python3 -c '
text = open("k8s/grafana.sh").read()
question = text.index("IFS= read -r answer")
assert question < text.index("grafana_read_login ||") and question < text.index("grafana_forward \"") and question < text.index("api datasource GET")'

# ---- the settings ------------------------------------------------------------------
section "The settings"

# settings KEY=VALUE...  -> a copy of settings.env with these changes.
settings() {
  local file="$WORK/settings-$RANDOM-$RANDOM.env" change
  cp settings.env "$file"
  for change in "$@"; do
    sed -i "/^${change%%=*}=/d" "$file"
    printf '%s\n' "$change" >>"$file"
  done
  printf '%s' "$file"
}
checked() {  # FILE -> the settings pass the checks of lib.sh
  SETTINGS_FILE="$1" bash -c '. k8s/lib.sh && read_settings && check_settings'
}
value_of() {  # FILE NAME -> the value after the checks
  SETTINGS_FILE="$1" bash -c '. k8s/lib.sh && read_settings && check_settings && printf "%s" "${!1}"' _ "$2"
}
export -f value_of

check "the settings of this project pass" checked settings.env
check "every line of settings.env has a comment above it (or is one)" python3 -c '
import re, sys
lines = open("settings.env").read().splitlines()
for i, line in enumerate(lines):
    if re.match(r"^[A-Z][A-Z0-9_]*=", line):
        j = i - 1
        while j >= 0 and re.match(r"^[A-Z][A-Z0-9_]*=", lines[j]):
            j -= 1  # a group of settings under one comment
        if j < 0 or not lines[j].startswith("#"):
            sys.exit("no comment above: " + line)'
check "the model file is pinned: a URL at one commit, a SHA-256" bash -c '
  [[ "$(value_of settings.env MODEL_URL)" =~ /resolve/[0-9a-f]{40}/ ]] && [[ "$(value_of settings.env MODEL_SHA256)" =~ ^[0-9a-f]{64}$ ]]'
check "the settings name the cluster, namespace, registry and storage class of the task" bash -c '
  [[ "$(value_of settings.env K8S_CONTEXT)" == my-cluster && "$(value_of settings.env K8S_NAMESPACE)" == grimoire ]] &&
  [[ "$(value_of settings.env REGISTRY)" == registry.example.com/grimoire && "$(value_of settings.env STORAGE_CLASS)" == my-storage-class ]]'
check "\"cpu/memory\" is split in two" bash -c '[[ "$(value_of "$1" LLAMA_LIM_CPU)/$(value_of "$1" LLAMA_LIM_MEMORY)" == 7000m/2Gi ]]' _ "$(settings LLAMA_LIMITS=7000m/2Gi)"
check "a value in quotes is taken without them" bash -c '[[ "$(value_of "$1" MIN_CONFIDENCE)" == 0.7 ]]' _ "$(settings 'MIN_CONFIDENCE="0.7"')"
check "an empty PULL_SECRET and NODE_LOCAL_DNS pass" checked "$(settings PULL_SECRET= NODE_LOCAL_DNS=)"
check "the model backend of this project is KubeAI (since 23 Oct 2026)" bash -c '[[ "$(value_of settings.env MODEL_BACKEND)" == kubeai ]]'
check "with the sidecar: LLM_BASE_URL is llama.cpp on 127.0.0.1:8081/v1, LLM_MODEL the model name" bash -c '
  [[ "$(value_of "$1" LLM_BASE_URL)" == http://127.0.0.1:8081/v1 && "$(value_of "$1" LLM_MODEL)" == granite-4.2-3b-Q4_K_M ]]' _ "$(settings MODEL_BACKEND=sidecar)"
check "MODEL_BACKEND=kubeai passes with the KubeAI settings of this project" checked "$(settings MODEL_BACKEND=kubeai)"
check "with kubeai: LLM_BASE_URL is KUBEAI_BASE_URL, LLM_MODEL is KUBEAI_MODEL, MODEL_NAME stays" bash -c '
  [[ "$(value_of "$1" LLM_BASE_URL)" == http://kubeai.kubeai.svc.cluster.local/openai/v1 && "$(value_of "$1" LLM_MODEL)" == granite-4-2-3b ]] &&
  [[ "$(value_of "$1" MODEL_NAME)" == granite-4.2-3b-Q4_K_M && "$(value_of "$1" KUBEAI_SERVICE)" == kubeai ]]' _ "$(settings MODEL_BACKEND=kubeai)"
check "the KubeAI settings of this project: namespace kubeai, 0 pods when idle, 1 hour warm" bash -c '
  [[ "$(value_of settings.env KUBEAI_NAMESPACE)" == kubeai && "$(value_of settings.env KUBEAI_MIN_REPLICAS)" == 0 ]] &&
  [[ "$(value_of settings.env KUBEAI_SCALE_DOWN_DELAY_SECONDS)" == 3600 ]]'
check "KubeAI downloads the file of MODEL_URL: its Hugging Face repo and commit" bash -c '
  [[ "$(value_of settings.env MODEL_HF_REPO)" == ibm-granite/granite-4.2-3b-GGUF ]] &&
  [[ "$(value_of settings.env MODEL_HF_COMMIT)" == c40945d71cd90f249a56985e8155551a9188dc30 ]]'
check "no Ollama any more: OLLAMA_IMAGE is not a setting" bash -c '
  ! grep -q "^OLLAMA_IMAGE=" settings.env && ! grep -q OLLAMA_IMAGE k8s/lib.sh && ! checked "$(settings OLLAMA_IMAGE=ollama/ollama:0.40.9@sha256:5c6707fcf2e1a30cffe2ecb7e315584fc87747c389327e4bc711c4959aa1eecc)"'
check "with the sidecar, empty KubeAI settings pass" checked "$(settings MODEL_BACKEND=sidecar KUBEAI_BASE_URL=)"
check "with the sidecar, a MODEL_URL that is not on Hugging Face passes" checked "$(settings MODEL_BACKEND=sidecar MODEL_URL=https://models.example.com/granite-4.2-3b-Q4_K_M.gguf)"
check "the traces of this project: on, Tempo 3.1.1 pinned by digest, 5Gi, 14 days, Grafana monitoring/monitoring-grafana" bash -c '
  [[ "$(value_of settings.env TELEMETRY)" == on && "$(value_of settings.env TEMPO_IMAGE)" =~ ^grafana/tempo:3\.1\.1@sha256:[0-9a-f]{64}$ ]] &&
  [[ "$(value_of settings.env TEMPO_SIZE)" == 5Gi && "$(value_of settings.env TEMPO_RETENTION)" == 336h ]] &&
  [[ "$(value_of settings.env GRAFANA_NAMESPACE)" == monitoring && "$(value_of settings.env GRAFANA_SERVICE)" == monitoring-grafana ]] &&
  [[ "$(value_of settings.env TEMPO_LIM_CPU)/$(value_of settings.env TEMPO_LIM_MEMORY)" == 200m/512Mi ]]'
check "the agents send to http://wattson-tempo:4318; Grafana reads http://wattson-tempo.grimoire.svc.cluster.local:3200" bash -c '
  [[ "$(value_of settings.env OTLP_ENDPOINT)" == http://wattson-tempo:4318 && "$(value_of settings.env TEMPO_URL)" == http://wattson-tempo.grimoire.svc.cluster.local:3200 ]]'
check "with TELEMETRY=off, empty Tempo and Grafana settings pass" checked "$(settings TELEMETRY=off TEMPO_IMAGE= GRAFANA_NAMESPACE= GRAFANA_SERVICE=)"
check "with TELEMETRY=on, an empty TEMPO_IMAGE is refused" not checked "$(settings TEMPO_IMAGE=)"
check "with TELEMETRY=on, an empty GRAFANA_NAMESPACE is refused" not checked "$(settings GRAFANA_NAMESPACE=)"
check "with TELEMETRY=on, an empty GRAFANA_SERVICE is refused" not checked "$(settings GRAFANA_SERVICE=)"
check "with kubeai, an empty KUBEAI_BASE_URL is refused" not checked "$(settings MODEL_BACKEND=kubeai KUBEAI_BASE_URL=)"
check "with kubeai, a MODEL_URL that is not on Hugging Face is refused" not checked "$(settings MODEL_BACKEND=kubeai MODEL_URL=https://models.example.com/granite-4.2-3b-Q4_K_M.gguf)"
check "with kubeai, a MODEL_URL at a branch (not a commit) is refused" \
  not checked "$(settings MODEL_BACKEND=kubeai MODEL_URL=https://huggingface.co/ibm-granite/granite-4.2-3b-GGUF/resolve/main/granite-4.2-3b-Q4_K_M.gguf)"
check "with kubeai, a MODEL_URL of another file than MODEL_FILE is refused" \
  not checked "$(settings MODEL_BACKEND=kubeai MODEL_URL=https://huggingface.co/ibm-granite/granite-4.2-3b-GGUF/resolve/c40945d71cd90f249a56985e8155551a9188dc30/granite-4.2-3b-Q8_0.gguf)"
check "a KUBEAI_BASE_URL outside KUBEAI_NAMESPACE is refused" not checked "$(settings KUBEAI_NAMESPACE=other)"
check "the Guard of this project: mode enforce (since 27 Oct 2026), 20 s for its model" bash -c '
  [[ "$(value_of settings.env GUARD_MODE)" == enforce && "$(value_of settings.env GUARD_TIMEOUT_S)" == 20 ]]'
check "with kubeai: the model of the Guard is the KubeAI Model of triage, through the proxy" bash -c '
  [[ "$(value_of "$1" GUARD_MODEL_URL)" == http://kubeai.kubeai.svc.cluster.local/openai/v1 && "$(value_of "$1" GUARD_MODEL)" == granite-4-2-3b ]]' _ "$(settings MODEL_BACKEND=kubeai)"
check "with the sidecar: the Guard has no model (the rules only)" bash -c '
  [[ -z "$(value_of "$1" GUARD_MODEL_URL)" && -z "$(value_of "$1" GUARD_MODEL)" ]]' _ "$(settings MODEL_BACKEND=sidecar)"
check "GUARD_MODE=enforce passes" checked "$(settings GUARD_MODE=enforce)"
check "GUARD_MODE=off passes (with INJECTION_MODE=off)" checked "$(settings GUARD_MODE=off INJECTION_MODE=off)"
check "GUARD_MODE=off with INJECTION_MODE=cut is refused: the injection model works inside the Guard" not checked "$(settings GUARD_MODE=off)"
check "the injection model of this project: cut, Horizon small at one commit, three files with their SHA-256, 0.95" bash -c '
  [[ "$(value_of settings.env INJECTION_MODE)" == cut && "$(value_of settings.env INJECTION_MODEL_REPO)" == Horizon-Labs/prompt-injection-guard-small ]] &&
  [[ "$(value_of settings.env INJECTION_MODEL_COMMIT)" =~ ^[0-9a-f]{40}$ && "$(value_of settings.env INJECTION_MODEL_FILE)" == onnx/model_quantized.onnx ]] &&
  [[ "$(value_of settings.env INJECTION_MODEL_SHA256)" =~ ^[0-9a-f]{64}$ && "$(value_of settings.env INJECTION_TOKENIZER_SHA256)" =~ ^[0-9a-f]{64}$ ]] &&
  [[ "$(value_of settings.env INJECTION_CONFIG_SHA256)" =~ ^[0-9a-f]{64}$ && "$(value_of settings.env INJECTION_LABEL)" == INJECTION ]] &&
  [[ "$(value_of settings.env INJECTION_THRESHOLD)" == 0.95 && "$(value_of settings.env INJECTION_WINDOW)" == 2048 ]]'
check "the shield downloads from https://huggingface.co/<repo>/resolve/<commit>, and gets as many threads as its CPU limit has cores" bash -c '
  [[ "$(value_of settings.env INJECTION_BASE_URL)" == https://huggingface.co/Horizon-Labs/prompt-injection-guard-small/resolve/0e3774536debbcd71dbb8534ef71451b583853b8 ]] &&
  [[ "$(value_of settings.env SHIELD_THREADS)" == 6 && "$(value_of "$1" SHIELD_THREADS)" == 1 ]]' _ "$(settings SHIELD_LIMITS=500m/1280Mi)"
check "the Wolf Defender block in the comments of settings.env passes the checks (the swap is a settings change)" bash -c '
  sed -n "s/^#   \(INJECTION_[A-Z0-9_]*=.*\)$/\1/p" settings.env >"$1.wolf" && [[ "$(wc -l <"$1.wolf")" -eq 10 ]] &&
  while IFS= read -r line; do sed -i "/^${line%%=*}=/d" "$1"; printf "%s\n" "$line" >>"$1"; done <"$1.wolf" &&
  SETTINGS_FILE="$1" bash -c ". k8s/lib.sh && read_settings && check_settings && [[ \$INJECTION_LABEL == injection && \$INJECTION_TOKENIZER_FILE == onnx/onnx_fp16/tokenizer.json ]]"' _ "$(settings)"
check "INJECTION_MODE=off passes with no file of the injection model" checked "$(settings INJECTION_MODE=off INJECTION_MODEL_REPO= INJECTION_MODEL_COMMIT= INJECTION_MODEL_FILE= INJECTION_MODEL_SHA256= INJECTION_TOKENIZER_SHA256= INJECTION_CONFIG_SHA256= INJECTION_LABEL=)"
for name in INJECTION_MODEL_REPO INJECTION_MODEL_COMMIT INJECTION_MODEL_FILE INJECTION_MODEL_SHA256 INJECTION_TOKENIZER_SHA256 INJECTION_CONFIG_SHA256 INJECTION_LABEL; do
  check "with INJECTION_MODE=warn, an empty $name is refused" not checked "$(settings "$name=")"
done
check "a line that is not NAME=value is refused" not checked "$(f="$(settings)"; echo 'echo hello' >>"$f"; printf '%s' "$f")"
while IFS= read -r bad; do
  check "a bad setting is refused: $bad" not checked "$(settings "$bad")"
done <<'BAD'
APP_NAME=Wattson
APP_NAME=my_agent
APP_NAME=agent-
K8S_CONTEXT=
K8S_CONTEXT=two words
K8S_NAMESPACE=Grimoire
REGISTRY=https://registry.example.com
STORAGE_CLASS=
DATA_SIZE=lots
MODELS_SIZE=3G
GRIMOIRE_MCP_URL=http://mcp.example.com/mcp
GRIMOIRE_MCP_URL=https://mcp.example.com/mcp?x=$(id)
LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server
LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp@sha256:abc
FETCH_IMAGE=curlimages/curl:latest
MODEL_FILE=../etc/passwd.gguf
MODEL_FILE=model.bin
MODEL_URL=http://example.com/model.gguf
MODEL_SHA256=6A1A2EB6D15622BF3C96857206351BA97E1AF16C30D7A74EE38970E434E9407E
MODEL_SHA256=abc
LLAMA_CTX=lots
LLAMA_THREADS=0
LLAMA_BATCH_THREADS=0
LLAMA_BATCH_THREADS=9
MIN_CONFIDENCE=1.5
MIN_CONFIDENCE=high
HISTORY_RETENTION_DAYS=0
TASK_TTL_MINUTES=-1
NODE_LOCAL_DNS=dns.local
ORCHESTRATOR_REQUESTS=50m
ORCHESTRATOR_LIMITS=1/512MB
LLAMA_LIMITS=2000m/2Gi;rm
PULL_SECRET=Bad_Name
MODEL_BACKEND=llama
MODEL_BACKEND=
KUBEAI_NAMESPACE=Kube_AI
KUBEAI_MODEL=Granite
KUBEAI_MODEL=granite-4-2-3b-with-a-name-that-is-far-too-long
KUBEAI_MIN_REPLICAS=2
KUBEAI_SCALE_DOWN_DELAY_SECONDS=0
KUBEAI_SCALE_DOWN_DELAY_SECONDS=15m
KUBEAI_BASE_URL=http://kubeai.other.svc.cluster.local/openai/v1
KUBEAI_BASE_URL=https://kubeai.example.com/openai/v1
KUBEAI_BASE_URL=http://kubeai.kubeai.svc.cluster.local/v1
KUBEAI_BASE_URL=http://kubeai.kubeai.svc.cluster.local/openai/v1?x=$(id)
TELEMETRY=yes
TELEMETRY=
TEMPO_IMAGE=grafana/tempo:3.1.1
TEMPO_IMAGE=grafana/tempo@sha256:abc
TEMPO_SIZE=5G
TEMPO_RETENTION=14d
TEMPO_RETENTION=0h
TEMPO_RETENTION=336h;rm
GRAFANA_NAMESPACE=Hex_fana
GRAFANA_SERVICE=monitoring_grafana
GRAFANA_SERVICE=$(id)
TEMPO_LIMITS=200m
GUARD_MODE=on
GUARD_MODE=
GUARD_MODE=Enforce
GUARD_TIMEOUT_S=0
GUARD_TIMEOUT_S=301
GUARD_TIMEOUT_S=20s
INJECTION_MODE=on
INJECTION_MODE=Warn
INJECTION_MODEL_REPO=just-a-name
INJECTION_MODEL_REPO=Horizon-Labs/x;rm
INJECTION_MODEL_COMMIT=main
INJECTION_MODEL_FILE=../model.onnx
INJECTION_MODEL_FILE=onnx/model.bin
INJECTION_MODEL_FILE=onnx/$(id).onnx
INJECTION_TOKENIZER_FILE=/etc/tokenizer.json
INJECTION_TOKENIZER_FILE=tokenizer.txt
INJECTION_CONFIG_FILE=config.yaml
INJECTION_MODEL_SHA256=abc
INJECTION_TOKENIZER_SHA256=47834A7DBBB0C4324FE9569FE47955241594A9EE539C9E478755D6FBC51E5F46
INJECTION_CONFIG_SHA256=xyz
INJECTION_LABEL=1abel
INJECTION_LABEL=INJECTION;rm
INJECTION_THRESHOLD=0
INJECTION_THRESHOLD=0.0
INJECTION_THRESHOLD=1.5
INJECTION_THRESHOLD=high
INJECTION_WINDOW=64
INJECTION_WINDOW=99999
INJECTION_TIMEOUT_S=0
INJECTION_TIMEOUT_S=301
SHIELD_LIMITS=2000m
SHIELD_REQUESTS=lots
NOT_A_SETTING=1
BAD

# ---- the rules of the Guard ----------------------------------------------------------
section "The rules of the Guard (the checks of lib.sh, for k8s/guard/)"
guard_checked() {  # DIR [PYTHON] -> the checks of deploy.sh and guard.sh pass for the files of DIR
  GUARD_DIR="$1" GUARD_PYTHON="${2:-python3}" bash -c '. k8s/lib.sh && read_settings && check_settings && check_guard_files && printf "%s\n" "$GUARD_CHECK_NOTES"'
}
guard_variant() {  # NAME RULES [POLICIES] -> a folder with these two files (the test ones by default)
  local dir="$WORK/guard-$1"
  mkdir -p "$dir"
  printf '%s\n' "$2" >"$dir/rules.yaml"
  if [[ $# -ge 3 ]]; then printf '%s' "$3" >"$dir/policies.cedar"; else cp "$GUARD_DIR/policies.cedar" "$dir/"; fi
  printf '%s' "$dir"
}
export -f guard_checked
check "the test rules pass (with their examples and not_examples), and the checks count them: 2 rules, 1 on" bash -c 'guard_checked "$1" | grep -qx "NOTE: 2 rules, 1 on (risky 1)"' _ "$GUARD_DIR"
check "other rules pass too: the smallest rule, a name, a label, one phrase and a reason" guard_checked "$(guard_variant other 'rules:
  - name: drop
    label: malicious
    match: [drop database]
    reason: It asks to destroy data.')"
while IFS='|' read -r what rules; do
  check "refused: $what" bash -c '! out="$(guard_checked "$1" 2>&1)" && grep -q "^PROBLEM: " <<<"$out" && grep -q "Nothing was changed" <<<"$out"' _ "$(guard_variant "bad-$RANDOM" "$(printf '%b' "$rules")")"
done <<'BAD_RULES'
not YAML|rules: [\n  - name: x
no list of rules|rules: {}
an empty list|rules: []
a plain list, without rules:|- {name: x, label: risky, match: [rm], reason: Risky.}
version 2|version: 2\nrules:\n  - {name: x, label: risky, match: [rm], reason: Risky.}
a label that is not one of malicious, risky, out_of_scope, injection|rules:\n  - {name: x, label: dangerous, match: [rm], reason: Risky.}
applies_to not request, evidence or both|rules:\n  - {name: x, label: risky, applies_to: all, match: [rm], reason: Risky.}
a regex Python cannot read|rules:\n  - {name: x, label: risky, regex: ["(unclosed"], reason: Risky.}
a rule that matches nothing|rules:\n  - {name: x, label: risky, reason: Risky.}
an empty phrase|rules:\n  - {name: x, label: risky, match: ["  "], reason: Risky.}
no reason|rules:\n  - {name: x, label: risky, match: [rm]}
a field the Guard does not know|rules:\n  - {name: x, label: risky, keywords: [rm], match: [rm], reason: Risky.}
two rules with one name|rules:\n  - {name: x, label: risky, match: [rm], reason: Risky.}\n  - {name: x, label: risky, match: [rm], reason: Risky.}
a name with spaces|rules:\n  - {name: Delete Pods, label: risky, match: [rm], reason: Risky.}
a name with _|rules:\n  - {name: delete_pods, label: risky, match: [rm], reason: Risky.}
enabled that is not true or false|rules:\n  - {name: x, label: risky, match: [rm], reason: Risky., enabled: maybe}
an example the rule does not match|rules:\n  - {name: x, label: risky, match: [rm], reason: Risky., examples: [remove the pod]}
a not_example the rule matches|rules:\n  - {name: x, label: risky, match: [rm], reason: Risky., not_examples: [rm the file]}
a control sign|rules:\n  - {name: x, label: risky, match: [rm], reason: "Risky\x07."}
BAD_RULES
check "refused: an empty policies.cedar" bash -c '! guard_checked "$1"' _ "$(guard_variant empty-cedar "rules: [{name: x, label: risky, match: [rm], reason: Risky.}]" "")"
check "refused: a file bigger than 256 KiB" bash -c '! guard_checked "$1"' _ "$(guard_variant big "rules: [{name: x, label: risky, match: [rm], reason: Risky.}]
# $(head -c 300000 /dev/zero | tr '\0' 'x')")"
check "refused: a folder without the two files" bash -c '! out="$(guard_checked "$1" 2>&1)" && grep -q "must hold rules.yaml and policies.cedar" <<<"$out"' _ "$WORK/guard-nowhere"
if [[ -x .venv/bin/python ]] && .venv/bin/python -c 'import cedarpy, yaml' 2>/dev/null; then
  check "with the project's Python: the Cedar policies are read (cedarpy)" bash -c 'guard_checked "$1" "$2" | grep -qx "NOTE: the Cedar policies can be read"' _ "$GUARD_DIR" "$PWD/.venv/bin/python"
  check "with the project's Python: a Cedar policy with a mistake is refused" bash -c '! out="$(guard_checked "$1" "$2" 2>&1)" && grep -q "is not a valid Cedar policy" <<<"$out"' \
    _ "$(guard_variant bad-cedar "rules: [{name: x, label: risky, match: [rm], reason: Risky.}]" 'forbid(principal, action resource);')" "$PWD/.venv/bin/python"
  check "with the project's Python: the checks are those of the code of the Guard, with every example" bash -c '
    guard_checked "$1" "$2" | grep -qx "NOTE: the code of the Guard reads them, and every example and not_example is right"' _ "$GUARD_DIR" "$PWD/.venv/bin/python"
  while IFS='|' read -r what rules; do
    check "the code of the Guard refuses it too: $what" bash -c '! guard_checked "$1" "$2" >/dev/null 2>&1' _ "$(guard_variant "guard-bad-$RANDOM" "$(printf '%b' "$rules")")" "$PWD/.venv/bin/python"
  done <<'GUARD_BAD'
a label that is not one of malicious, risky, out_of_scope, injection|rules:\n  - {name: x, label: dangerous, match: [rm], reason: Risky.}
a regex Python cannot read|rules:\n  - {name: x, label: risky, regex: ["(unclosed"], reason: Risky.}
a field the Guard does not know|rules:\n  - {name: x, label: risky, keywords: [rm], match: [rm], reason: Risky.}
an example the rule does not match|rules:\n  - {name: x, label: risky, match: [rm], reason: Risky., examples: [remove the pod]}
GUARD_BAD
else
  skip "the project's Python (.venv, with cedarpy) is not there: run uv sync"
fi
if [[ -f k8s/guard/rules.yaml && -f k8s/guard/policies.cedar ]]; then
  check "the rules of this project (k8s/guard/) pass the checks (with the project's Python and the code of the Guard when there)" \
    guard_checked "$PWD/k8s/guard" "$([[ -x .venv/bin/python ]] && printf '%s' "$PWD/.venv/bin/python" || printf python3)"
else
  skip "k8s/guard/rules.yaml and policies.cedar are not there yet"
fi

# ---- the manifests, filled in ------------------------------------------------------
section "The manifests (k8s/wattson.yaml, filled in)"

TEST_IMAGE="registry.example.test/p/wattson:abc123@sha256:$(printf 'ab%.0s' {1..32})"
TEST_SHIELD_IMAGE="registry.example.test/p/wattson-shield:abc123@sha256:$(printf 'ef%.0s' {1..32})"
render() {  # SETTINGS_FILE -> the filled-in manifests, the way deploy.sh fills them
  SETTINGS_FILE="$1" IMAGE="$TEST_IMAGE" SHIELD_IMAGE="$TEST_SHIELD_IMAGE" DEPLOY_FUNCTIONS_ONLY=yes bash -c '. k8s/deploy.sh && filled_yaml'
}

cat >"$WORK/check_manifests.py" <<'PY'
"""Checks the filled-in manifests. Prints "ok|..." or "FAIL|..." lines. The settings come
from the environment (lib.sh exports them); the rules of the Guard from GUARD_DIR."""
import hashlib
import os
import re
import sys

import yaml

env = os.environ
app = env["APP_NAME"]
orch, triage, know, tempo = f"{app}-orchestrator", f"{app}-triage", f"{app}-knowledge", f"{app}-tempo"
kubeai = env["MODEL_BACKEND"] == "kubeai"
telemetry = env["TELEMETRY"] == "on"
shield_on = env["INJECTION_MODE"] != "off"
shield = f"{app}-shield"
recall = env["COGNEE_RECALL"] == "on"
cognee = f"{app}-cognee"
text = open(sys.argv[1]).read()
docs = [d for d in yaml.safe_load_all(text) if d]
by = {(d["kind"], d["metadata"]["name"]): d for d in docs}


def report(ok, what, detail=""):
    print(("ok|" if ok else "FAIL|") + what + (f" ({detail})" if detail and not ok else ""))


def pod(name):
    return by[("Deployment", name)]["spec"]["template"]["spec"]


def containers(spec):
    return spec.get("initContainers", []) + spec["containers"]


def container(name, cname):
    return next(c for c in containers(pod(name)) if c["name"] == cname)


def env_of(c):
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in c.get("env", [])}


def secret_keys(c):
    return sorted(e["valueFrom"]["secretKeyRef"]["key"] for e in c.get("env", []) if "valueFrom" in e)


kinds = sorted(f"{d['kind']}/{d['metadata']['name']}" for d in docs)
want = sorted(
    [f"ServiceAccount/{n}" for n in (orch, triage, know)]
    + [f"PersistentVolumeClaim/{app}-{n}" for n in ("data", "knowledge-data") + (() if kubeai else ("models",))]
    + [f"Deployment/{n}" for n in (orch, triage, know)]
    + [f"Service/{n}" for n in (triage, know)]
    + [f"NetworkPolicy/{n}" for n in (orch, triage, know)]
    + [f"ConfigMap/{app}-guard"]
    + ([f"ServiceAccount/{tempo}", f"PersistentVolumeClaim/{app}-tempo-data", f"ConfigMap/{tempo}", f"Deployment/{tempo}",
        f"Service/{tempo}", f"NetworkPolicy/{tempo}"] if telemetry else [])
    + ([f"ServiceAccount/{shield}", f"Deployment/{shield}", f"Service/{shield}", f"NetworkPolicy/{shield}"] if shield_on else [])
)
report(kinds == want, f"exactly the {len(want)} objects: the three agents{' and Tempo' if telemetry else ''}{' and the shield' if shield_on else ''} with their volumes, services, network rules and the rules of the Guard", kinds)
report(shield_on or not re.search(r"shield|\b9003\b|SHIELD_|INJECTION_(?!MODE)", "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")), re.I),
       "INJECTION_MODE=off: nothing of the shield is left")
report(telemetry or not re.search(r"\btempo\b|OTEL_|\b4318\b|\b3200\b", "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")), re.I),
       "TELEMETRY=off: nothing of Tempo or OpenTelemetry is left")
report(not any(d["kind"] in ("Ingress", "Route", "Gateway", "HTTPRoute") for d in docs), "no Ingress at all")
report(not any(d["kind"] == "Secret" for d in docs), "no Secret in the files")
services = [d for d in docs if d["kind"] == "Service"]
report(
    all(d["metadata"]["name"] != orch and d["spec"].get("selector", {}).get("app") != orch for d in services),
    "no Service for the orchestrator",
)
report(all(d["spec"].get("type", "ClusterIP") == "ClusterIP" for d in services), "the Services are ClusterIP only")
report(not any(d["kind"] in ("Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding") for d in docs), "no Role and no binding")
report(all(d["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") == app for d in docs), "every object carries part-of=" + app)
report(not any("namespace" in d["metadata"] for d in docs), "no object names a namespace (kubectl -n does)")
# The ConfigMap of the Guard (a JSON document, from "{" to "}") holds your rules as they are.
lines, in_json = [], False
for line in text.splitlines():
    in_json = in_json or line == "{"
    if not in_json and not line.lstrip().startswith("#"):
        lines.append(line)
    in_json = in_json and line != "}"
report(not any("${" in line or "# optional" in line for line in lines), "every ${...} is filled in (but in the rules of the Guard)")
report(not re.search(r"^ *# (begin|end): ", text, re.M), "no \"# begin:\" or \"# end:\" mark is left")

for d in docs:
    if d["kind"] == "ServiceAccount":
        report(d.get("automountServiceAccountToken") is False, f"{d['metadata']['name']}: the service account mounts no token")
    if d["kind"] == "PersistentVolumeClaim":
        size = {f"{app}-data": env["DATA_SIZE"], f"{app}-knowledge-data": env["KNOWLEDGE_DATA_SIZE"], f"{app}-models": env["MODELS_SIZE"],
                f"{app}-tempo-data": env["TEMPO_SIZE"]}
        n = d["metadata"]["name"]
        report(
            d["spec"]["storageClassName"] == env["STORAGE_CLASS"] and d["spec"]["accessModes"] == ["ReadWriteOnce"]
            and d["spec"]["resources"]["requests"]["storage"] == size[n],
            f"{n}: storage class {env['STORAGE_CLASS']}, one writer, {size[n]}",
        )

pinned = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
for name in (orch, triage, know) + ((shield,) if shield_on else ()):
    dep = by[("Deployment", name)]
    spec = pod(name)
    sc = spec.get("securityContext", {})
    claims = [v["persistentVolumeClaim"]["claimName"] for v in spec.get("volumes", []) if "persistentVolumeClaim" in v]
    report(dep["spec"]["replicas"] == 1, f"{name}: one replica")
    report(not claims or dep["spec"].get("strategy", {}).get("type") == "Recreate", f"{name}: Recreate, because of its volume")
    report(spec.get("automountServiceAccountToken") is False and spec.get("serviceAccountName") == name, f"{name}: its own service account, no token mounted")
    report(spec.get("enableServiceLinks") is False, f"{name}: no Service addresses in the environment")
    report(
        sc.get("runAsNonRoot") is True and sc.get("runAsUser") == 10001 and sc.get("runAsGroup") == 10001
        and sc.get("fsGroup") == 10001 and sc.get("seccompProfile") == {"type": "RuntimeDefault"},
        f"{name}: non-root 10001, fsGroup, RuntimeDefault seccomp",
    )
    pulls = [p["name"] for p in spec.get("imagePullSecrets", [])]
    report(pulls == ([env["PULL_SECRET"]] if env.get("PULL_SECRET") else []), f"{name}: the pull secret of the settings", pulls)
    report(dep["spec"]["selector"]["matchLabels"] == {"app": name} and dep["spec"]["template"]["metadata"]["labels"]["app"] == name, f"{name}: label app={name}")
    tmp_volumes = {v["name"] for v in spec.get("volumes", []) if "emptyDir" in v}
    for c in containers(spec):
        cn = f"{name}/{c['name']}"
        r = c.get("resources", {})
        report(
            all(r.get(k, {}).get(x) for k in ("requests", "limits") for x in ("cpu", "memory")),
            f"{cn}: CPU and memory requests and limits",
        )
        csc = c.get("securityContext", {})
        report(
            csc.get("readOnlyRootFilesystem") is True and csc.get("allowPrivilegeEscalation") is False
            and csc.get("capabilities", {}).get("drop") == ["ALL"] and not csc.get("privileged")
            and not csc.get("capabilities", {}).get("add"),
            f"{cn}: read-only root, no privilege escalation, every capability dropped",
        )
        report(bool(pinned.match(c["image"])), f"{cn}: image pinned by digest", c["image"])
        report(any(m["mountPath"] == "/tmp" and m["name"] in tmp_volumes for m in c.get("volumeMounts", [])), f"{cn}: /tmp is an emptyDir")
        report(not any("hostPath" in v for v in spec.get("volumes", [])) and not spec.get("hostNetwork"), f"{cn}: no host path, no host network")
        for e in c.get("env", []):
            if re.search(r"TOKEN$", e["name"]) and "value" in e:
                report(False, f"{cn}: {e['name']} is a literal value; it must come from the Secret")

# The orchestrator: 127.0.0.1, no port, exec probes, all three tokens, the two URLs, /data.
o = container(orch, "orchestrator")
oe = env_of(o)
report(o["command"] == ["wattson-orchestrator"] and o["image"] == env["IMAGE"], "orchestrator: runs wattson-orchestrator from the built image")
report(oe.get("WATTSON_LISTEN_HOST") == "127.0.0.1" and oe.get("WATTSON_PORT") == "9000", "orchestrator: listens on 127.0.0.1:9000")
report(not o.get("ports"), "orchestrator: no container port")
report(oe.get("TRIAGE_URL") == f"http://{triage}:9001" and oe.get("KNOWLEDGE_URL") == f"http://{know}:9002", "orchestrator: TRIAGE_URL and KNOWLEDGE_URL")
report(secret_keys(o) == sorted(["KNOWLEDGE_TOKEN", "TRIAGE_TOKEN", "WATTSON_TOKEN"] + (["SHIELD_TOKEN"] if shield_on else [])),
       "orchestrator: the three tokens, from the Secret" + (", and the shield's" if shield_on else ""), secret_keys(o))
refs = {e["name"]: e["valueFrom"]["secretKeyRef"]["name"] for e in o["env"] if "valueFrom" in e}
report(all(v == (f"{app}-shield" if k == "SHIELD_TOKEN" else app) for k, v in refs.items()),
       f"orchestrator: the three tokens from the Secret {app}" + (f", SHIELD_TOKEN from the Secret {app}-shield" if shield_on else ""), refs)
injection = {k: v for k, v in oe.items() if k.startswith(("INJECTION_", "SHIELD_"))}
want_injection = {"INJECTION_MODE": env["INJECTION_MODE"]} | ({
    "SHIELD_URL": f"http://{shield}:9003", "SHIELD_TOKEN": {"secretKeyRef": {"name": f"{app}-shield", "key": "SHIELD_TOKEN"}},
    "INJECTION_MODEL": env["INJECTION_MODEL_REPO"], "INJECTION_THRESHOLD": env["INJECTION_THRESHOLD"], "INJECTION_TIMEOUT_S": env["INJECTION_TIMEOUT_S"],
} if shield_on else {})
report(injection == want_injection, "orchestrator: INJECTION_MODE of the settings" + (", the shield's address, token, model name, threshold and wait" if shield_on else ", and nothing else of the shield"), injection)
report(oe.get("WATTSON_DATA_DIR") == "/data" and any(m["mountPath"] == "/data" for m in o["volumeMounts"]) and f"{app}-data" in str(pod(orch)["volumes"]), "orchestrator: its volume on /data, and WATTSON_DATA_DIR says so")
report(oe.get("HISTORY_RETENTION_DAYS") == env["HISTORY_RETENTION_DAYS"] and oe.get("TASK_TTL_MINUTES") == env["TASK_TTL_MINUTES"], "orchestrator: retention and task TTL")
report(oe.get("TRIAGE_TIMEOUT_S") == ("180" if kubeai else None), "orchestrator: waits 180 s for triage with KubeAI (a cold start), its default 60 s with the sidecar", oe.get("TRIAGE_TIMEOUT_S"))
probes = [o.get(p) for p in ("startupProbe", "readinessProbe", "livenessProbe")]
report(
    all(p and "exec" in p and "httpGet" not in p and "tcpSocket" not in p and "http://127.0.0.1:9000/healthz" in " ".join(p["exec"]["command"]) for p in probes),
    "orchestrator: exec probes on 127.0.0.1:9000/healthz (the kubelet cannot reach 127.0.0.1)",
)

# The Guard: your rules in the ConfigMap <app>-guard (the exact text of the two files), read-only
# at /etc/wattson/guard; its mode and wait from the settings; its small model: the KubeAI Model of
# triage, or none with the sidecar (the rules only).
names = ("rules.yaml", "policies.cedar")
files = {n: open(os.path.join(env["GUARD_DIR"], n), encoding="utf-8", newline="").read() for n in names}
guard_cm = by.get(("ConfigMap", f"{app}-guard"), {})
report(guard_cm.get("data") == files, "the Guard: its ConfigMap holds the exact text of rules.yaml and policies.cedar (a ${...} in them stays)", sorted(guard_cm.get("data") or {}))
order = [(d["kind"], d["metadata"]["name"]) for d in docs]
report(("ConfigMap", f"{app}-guard") in order and order.index(("ConfigMap", f"{app}-guard")) < order.index(("Deployment", orch)), "the Guard: its ConfigMap comes before the orchestrator")
mounts = {m["mountPath"]: m for m in o["volumeMounts"]}
volumes = {v["name"]: v for v in pod(orch)["volumes"]}
guard_mount = mounts.get("/etc/wattson/guard", {})
report(guard_mount.get("readOnly") is True and volumes.get(guard_mount.get("name"), {}).get("configMap") == {"name": f"{app}-guard"},
       "orchestrator: the rules of the Guard at /etc/wattson/guard, read-only, from the ConfigMap", guard_mount)
report(oe.get("GUARD_RULES_FILE") == "/etc/wattson/guard/rules.yaml" and oe.get("GUARD_POLICIES_FILE") == "/etc/wattson/guard/policies.cedar",
       "orchestrator: GUARD_RULES_FILE and GUARD_POLICIES_FILE name the two mounted files")
report(oe.get("GUARD_MODE") == env["GUARD_MODE"] and oe.get("GUARD_TIMEOUT_S") == env["GUARD_TIMEOUT_S"], "orchestrator: GUARD_MODE and GUARD_TIMEOUT_S of the settings", oe)
model = (env["KUBEAI_BASE_URL"], env["KUBEAI_MODEL"]) if kubeai else ("", "")
report((oe.get("GUARD_MODEL_URL"), oe.get("GUARD_MODEL")) == model,
       "orchestrator: the model of the Guard is the KubeAI Model of triage" if kubeai else "orchestrator: the Guard has no model with the sidecar (GUARD_MODEL_URL empty: the rules only)", oe)
listing = "".join("%s  %s\n" % (hashlib.sha256(files[n].encode("utf-8")).hexdigest(), n) for n in names)
annotations = by[("Deployment", orch)]["spec"]["template"]["metadata"].get("annotations") or {}
report(annotations.get("wattson/guard-config") == hashlib.sha256(listing.encode()).hexdigest(),
       "orchestrator: its pod carries the SHA-256 of the two files (new rules start a new pod)", annotations)

# Triage: the model download, llama.cpp, the agent (sidecar); or the agent alone (kubeai).
t = container(triage, "triage")
te = env_of(t)
if kubeai:
    spec = pod(triage)
    report(not spec.get("initContainers") and [c["name"] for c in spec["containers"]] == ["triage"], "triage (kubeai): no model download, no llama container: the triage container only")
    report(not any("persistentVolumeClaim" in v for v in spec.get("volumes", [])) and [m["name"] for m in t["volumeMounts"]] == ["tmp-triage"], "triage (kubeai): no models volume")
    report(te.get("LLM_BASE_URL") == env["KUBEAI_BASE_URL"] and te.get("LLM_MODEL") == env["KUBEAI_MODEL"], "triage (kubeai): LLM_BASE_URL is the KubeAI proxy, LLM_MODEL the KubeAI Model", te)
    report("LLAMA_URL" not in te and "127.0.0.1:8081" not in text and "fetch-model" not in str(spec) and f"{app}-models" not in text, "triage (kubeai): nothing of llama.cpp is left")
else:
    f = container(triage, "fetch-model")
    fe = env_of(f)
    script = f["command"][-1]
    report(f["image"] == env["FETCH_IMAGE"], "triage: the download runs in FETCH_IMAGE")
    report(fe.get("FILE_NAME") == env["MODEL_FILE"] and fe.get("SOURCE_URL") == env["MODEL_URL"] and fe.get("SHA256") == env["MODEL_SHA256"], "triage: the download gets the file, URL and SHA-256 of the settings")
    report("sha256sum -c" in script and "mv -f" in script and ".partial-" in script and "--proto '=https'" in script, "triage: download to a temporary name, SHA-256 check, then rename; HTTPS only")
    report(any(m["mountPath"] == "/models" and not m.get("readOnly") for m in f["volumeMounts"]), "triage: the download writes the models volume")
    llama = container(triage, "llama")
    args = " ".join(llama["args"])
    report(llama["image"] == env["LLAMA_IMAGE"], "triage: llama runs LLAMA_IMAGE")
    report(
        f"-m /models/{env['MODEL_FILE']}" in args and "--host 127.0.0.1" in args and "--port 8081" in args
        and f"-c {env['LLAMA_CTX']}" in args and f"-t {env['LLAMA_THREADS']}" in args and f"-tb {env['LLAMA_BATCH_THREADS']}" in args
        and "--cache-ram 0" in args,
        "triage: llama on 127.0.0.1:8081 with the model, context and threads of the settings, no RAM cache",
        args,
    )
    report(any(m["mountPath"] == "/models" and m.get("readOnly") is True for m in llama["volumeMounts"]), "triage: llama reads the models volume read-only")
    report(not llama.get("ports") and "readinessProbe" not in llama, "triage: llama has no port and no readiness probe")
    report(all("exec" in llama[p] and "curl" in llama[p]["exec"]["command"] for p in ("startupProbe", "livenessProbe")), "triage: llama has curl probes")
    report(llama["startupProbe"]["periodSeconds"] * llama["startupProbe"]["failureThreshold"] >= 300, "triage: llama gets at least 5 minutes to load the model")
    report(te.get("LLAMA_URL") == "http://127.0.0.1:8081" and te.get("LLM_BASE_URL") == "http://127.0.0.1:8081/v1", "triage: asks llama on 127.0.0.1:8081 (LLM_BASE_URL .../v1)")
    report(te.get("LLM_MODEL") == env["MODEL_FILE"][: -len(".gguf")], "triage: LLM_MODEL is the model name")
report(t["command"] == ["wattson-triage"] and t["image"] == env["IMAGE"], "triage: runs wattson-triage from the built image")
report(te.get("TRIAGE_TIMEOUT_S") == ("150" if kubeai else None), "triage: 150 s for the model with KubeAI (a cold start), its default 50 s with the sidecar", te.get("TRIAGE_TIMEOUT_S"))
report(te.get("TRIAGE_PORT") == "9001" and te.get("TRIAGE_LISTEN_HOST") == "0.0.0.0", "triage: listens on 9001")
report(te.get("MIN_CONFIDENCE") == env["MIN_CONFIDENCE"] and te.get("MODEL_NAME") == env["MODEL_FILE"][: -len(".gguf")], "triage: MIN_CONFIDENCE and MODEL_NAME")
report(secret_keys(t) == ["TRIAGE_TOKEN"] and all(not secret_keys(c) for c in containers(pod(triage)) if c["name"] != "triage"), "triage: only its own token")
report(all(t[p]["httpGet"]["path"] == "/healthz" for p in ("startupProbe", "readinessProbe", "livenessProbe")), "triage: httpGet /healthz probes")

# Knowledge.
k = container(know, "knowledge")
ke = env_of(k)
report(k["command"] == ["wattson-knowledge"] and k["image"] == env["IMAGE"], "knowledge: runs wattson-knowledge from the built image")
report(ke.get("GRIMOIRE_MCP_URL") == env["GRIMOIRE_MCP_URL"] and ke.get("KNOWLEDGE_PORT") == "9002", "knowledge: GRIMOIRE_MCP_URL, port 9002")
report(secret_keys(k) == ["KNOWLEDGE_TOKEN"], "knowledge: only its own token")
report(ke.get("KNOWLEDGE_DATA_DIR") == "/data" and any(m["mountPath"] == "/data" for m in k["volumeMounts"]) and f"{app}-knowledge-data" in str(pod(know)["volumes"]), "knowledge: its volume on /data, and KNOWLEDGE_DATA_DIR says so")
report(all(k[p]["httpGet"]["path"] == "/healthz" for p in ("startupProbe", "readinessProbe", "livenessProbe")), "knowledge: httpGet /healthz probes")
# "Seen before": the lessons of Cognee memory, read-only; the pod of k8s/cognee.sh is not in these files.
want_recall = {"COGNEE_RECALL": env["COGNEE_RECALL"]} | ({
    "COGNEE_RECALL_URL": env["COGNEE_RECALL_URL"], "COGNEE_RECALL_DATASET": env["COGNEE_RECALL_DATASET"],
    "COGNEE_RECALL_LESSONS": env["COGNEE_RECALL_LESSONS"], "COGNEE_RECALL_TIMEOUT_S": env["COGNEE_RECALL_TIMEOUT_S"],
} if recall else {})
got_recall = {n: v for n, v in ke.items() if n.startswith("COGNEE_")}
report(got_recall == want_recall, "knowledge: COGNEE_RECALL " + env["COGNEE_RECALL"] + (", and the URL, dataset, lessons and wait of the settings" if recall else ", and nothing else of Cognee"), got_recall)
report(not any(name == cognee or name.startswith(cognee + "-") for _, name in by), f"no object of {cognee} (k8s/cognee.sh makes them, deploy.sh never)")

# The traces: each agent gets the address of Tempo and its own service name, and nothing else
# of OpenTelemetry; with TELEMETRY=off none of it.
for name, cname in ((orch, "orchestrator"), (triage, "triage"), (know, "knowledge")):
    otel = {k: v for k, v in env_of(container(name, cname)).items() if k.startswith("OTEL_")}
    want_otel = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{tempo}:4318", "OTEL_SERVICE_NAME": name} if telemetry else {}
    report(otel == want_otel, f"{cname}: " + ("OTEL_EXPORTER_OTLP_ENDPOINT http://" + tempo + ":4318 and OTEL_SERVICE_NAME " + name if telemetry else "no OTEL_ setting"), otel)

if telemetry:
    dep = by[("Deployment", tempo)]
    spec = pod(tempo)
    sc = spec.get("securityContext", {})
    c = container(tempo, "tempo")
    csc = c.get("securityContext", {})
    mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
    volumes = {v["name"]: v for v in spec["volumes"]}
    report(dep["spec"]["replicas"] == 1 and dep["spec"]["strategy"] == {"type": "Recreate"}, "tempo: one replica, Recreate (one volume, one writer)")
    report(spec.get("automountServiceAccountToken") is False and spec.get("serviceAccountName") == tempo and spec.get("enableServiceLinks") is False,
           "tempo: its own service account, no token, no Service addresses")
    report(sc.get("runAsNonRoot") is True and sc.get("runAsUser") == 10001 and sc.get("runAsGroup") == 10001 and sc.get("fsGroup") == 10001
           and sc.get("seccompProfile") == {"type": "RuntimeDefault"}, "tempo: non-root 10001, fsGroup, RuntimeDefault seccomp")
    report(csc.get("readOnlyRootFilesystem") is True and csc.get("allowPrivilegeEscalation") is False and csc.get("capabilities", {}).get("drop") == ["ALL"]
           and not csc.get("privileged") and not csc.get("capabilities", {}).get("add"), "tempo: read-only root, no privilege escalation, every capability dropped")
    report(c["image"] == env["TEMPO_IMAGE"] and bool(pinned.match(c["image"])) and spec["containers"] == [c] and not spec.get("initContainers"),
           "tempo: one container, TEMPO_IMAGE, pinned by digest", c["image"])
    report(c["resources"] == {"requests": {"cpu": env["TEMPO_REQ_CPU"], "memory": env["TEMPO_REQ_MEMORY"]},
                              "limits": {"cpu": env["TEMPO_LIM_CPU"], "memory": env["TEMPO_LIM_MEMORY"]}}, "tempo: the requests and limits of the settings", c["resources"])
    report(c.get("args") == ["-config.file=/etc/tempo/tempo.yaml"] and mounts.get("/etc/tempo", {}).get("readOnly") is True
           and volumes[mounts["/etc/tempo"]["name"]].get("configMap") == {"name": tempo}, "tempo: its config from the ConfigMap, read-only")
    report(volumes[mounts["/var/tempo"]["name"]].get("persistentVolumeClaim") == {"claimName": f"{app}-tempo-data"}
           and "emptyDir" in volumes[mounts["/tmp"]["name"]] and set(mounts) == {"/etc/tempo", "/var/tempo", "/tmp"},
           "tempo: /var/tempo on its volume, /tmp an emptyDir, nothing else writable")
    report(not any("hostPath" in v for v in spec["volumes"]) and not spec.get("hostNetwork"), "tempo: no host path, no host network")
    report(sorted((p["name"], p["containerPort"]) for p in c["ports"]) == [("http", 3200), ("otlp-http", 4318)], "tempo: ports 4318 (OTLP over HTTP) and 3200 (TraceQL)")
    report(all(c[p]["httpGet"] == {"path": "/ready", "port": "http"} for p in ("startupProbe", "readinessProbe", "livenessProbe")), "tempo: /ready probes")
    config_text = by[("ConfigMap", tempo)]["data"]["tempo.yaml"]
    import hashlib
    report(dep["spec"]["template"]["metadata"]["annotations"].get("wattson/tempo-config") == hashlib.sha256(config_text.encode()).hexdigest(),
           "tempo: the pod carries the SHA-256 of its config (a new config, a new pod)")
    config = yaml.safe_load(config_text)
    report(config.get("target") == "all", "tempo config: one process (target all, no Kafka)")
    report(config["distributor"]["receivers"] == {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}}, "tempo config: OTLP over HTTP on 4318 only (no gRPC, no other receiver)")
    report(config["server"]["http_listen_port"] == 3200, "tempo config: TraceQL on 3200")
    report(config["storage"]["trace"]["backend"] == "local" and all(v.startswith("/var/tempo/") for v in (
        config["storage"]["trace"]["wal"]["path"], config["storage"]["trace"]["local"]["path"])), "tempo config: local storage on its volume")
    retention = env["TEMPO_RETENTION"]
    report(config["backend_scheduler"]["provider"]["compaction"]["compaction"]["block_retention"] == retention
           and config["backend_worker"]["compaction"]["block_retention"] == retention, f"tempo config: traces kept {retention}")
    report(config["query_frontend"]["search"]["max_duration"] == retention and config["query_frontend"]["metrics"]["max_duration"] == retention,
           "tempo config: search and TraceQL metrics over all that is kept (not 7 days / 24 hours)")
    report(config["usage_report"] == {"reporting_enabled": False}, "tempo config: no usage report")
    report("metrics_generator" not in config and "local-blocks" not in config_text and "local_blocks" not in config_text,
           "tempo config: no metrics-generator (Tempo 3 computes TraceQL metrics itself; local-blocks is gone)")
    s = by[("Service", tempo)]["spec"]
    report(s["selector"] == {"app": tempo} and sorted((p["port"], p["targetPort"]) for p in s["ports"]) == [(3200, "http"), (4318, "otlp-http")],
           "Service tempo: 4318 and 3200")

if shield_on:
    spec = pod(shield)
    dep = by[("Deployment", shield)]
    f = container(shield, "fetch-model")
    fe = env_of(f)
    script = f["command"][-1]
    c = container(shield, "shield")
    ce = env_of(c)
    mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
    volumes = {v["name"]: v for v in spec["volumes"]}
    report([x["name"] for x in spec["initContainers"]] == ["fetch-model"] and [x["name"] for x in spec["containers"]] == ["shield"],
           "shield: one download (init container), one server")
    report(f["image"] == env["FETCH_IMAGE"], "shield: the download runs in FETCH_IMAGE")
    report(fe.get("BASE_URL") == f"https://huggingface.co/{env['INJECTION_MODEL_REPO']}/resolve/{env['INJECTION_MODEL_COMMIT']}"
           and (fe.get("ONNX_FILE"), fe.get("ONNX_SHA256")) == (env["INJECTION_MODEL_FILE"], env["INJECTION_MODEL_SHA256"])
           and (fe.get("TOKENIZER_FILE"), fe.get("TOKENIZER_SHA256")) == (env["INJECTION_TOKENIZER_FILE"], env["INJECTION_TOKENIZER_SHA256"])
           and (fe.get("CONFIG_FILE"), fe.get("CONFIG_SHA256")) == (env["INJECTION_CONFIG_FILE"], env["INJECTION_CONFIG_SHA256"]),
           "shield: the download gets the repo at its commit, and the three files with their SHA-256, of the settings", fe)
    report("sha256sum -c" in script and "mv -f" in script and ".partial-" in script and "--proto '=https'" in script and "exit 1" in script
           and all(f"fetch {n} " in script for n in ("model.onnx", "tokenizer.json", "config.json")),
           "shield: each file to a temporary name, SHA-256 check (a wrong one stops the pod), then rename; HTTPS only")
    # The deploy fills in the settings it knows in this file: a name of the script that is also
    # a setting (MODEL_FILE) would become the file of another model.
    report('fetch model.onnx "$ONNX_FILE" "$ONNX_SHA256"' in script
           and set(re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)", script)) <= set(fe),
           "shield: the download script reads its own variables, as written (none was filled in by the deploy)", script)
    report(volumes[mounts["/models"]["name"]].get("emptyDir", {}).get("sizeLimit") == "1Gi" and mounts["/models"].get("readOnly") is True
           and any(m["mountPath"] == "/models" and not m.get("readOnly") for m in f["volumeMounts"]),
           "shield: the files on an emptyDir of 1Gi (no volume claim), written by the download, read-only for the server")
    report(c["image"] == env["SHIELD_IMAGE"] and c["image"] != env["IMAGE"] and c["command"] == ["python", "-I", "/app/shield.py"],
           "shield: runs /app/shield.py from its own image (SHIELD_IMAGE)", c["image"])
    report([(p["name"], p["containerPort"]) for p in c["ports"]] == [("http", 9003)] and ce.get("SHIELD_LISTEN_HOST") == "0.0.0.0" and ce.get("SHIELD_PORT") == "9003",
           "shield: listens on 9003")
    report((ce.get("SHIELD_MODEL_PATH"), ce.get("SHIELD_TOKENIZER_PATH"), ce.get("SHIELD_CONFIG_PATH")) == ("/models/model.onnx", "/models/tokenizer.json", "/models/config.json")
           and ce.get("INJECTION_LABEL") == env["INJECTION_LABEL"] and ce.get("INJECTION_WINDOW") == env["INJECTION_WINDOW"] and ce.get("SHIELD_THREADS") == env["SHIELD_THREADS"],
           "shield: the three files, the label, the window and the threads of the settings", ce)
    report(secret_keys(c) == ["SHIELD_TOKEN"] and ce["SHIELD_TOKEN"] == {"secretKeyRef": {"name": f"{app}-shield", "key": "SHIELD_TOKEN"}} and not secret_keys(f),
           f"shield: only its token, from the Secret {app}-shield")
    report(c["resources"] == {"requests": {"cpu": env["SHIELD_REQ_CPU"], "memory": env["SHIELD_REQ_MEMORY"]},
                              "limits": {"cpu": env["SHIELD_LIM_CPU"], "memory": env["SHIELD_LIM_MEMORY"]}}, "shield: the requests and limits of the settings", c["resources"])
    report(all(c[p]["httpGet"] == {"path": "/healthz", "port": "http"} for p in ("startupProbe", "readinessProbe", "livenessProbe")), "shield: httpGet /healthz probes")
    report(dep["spec"]["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
           "shield: no volume, so the new pod starts before the old one stops")
    given = [e["name"] for e in c["env"] if e["name"] not in ("HOME", "TMPDIR")]
    server_code = open(os.path.join(env.get("WATTSON_SRC", "src/wattson"), "shield", "server.py"), encoding="utf-8").read()
    report(all(f'"{n}"' in server_code for n in given), "shield: the code reads every setting the manifests give it", [n for n in given if f'"{n}"' not in server_code])
    sv = by[("Service", shield)]["spec"]
    report(sv["selector"] == {"app": shield} and [(p["port"], p["targetPort"]) for p in sv["ports"]] == [(9003, "http")], f"Service {shield}: port 9003")
    report(not any("SHIELD_TOKEN" in secret_keys(x) for n in (triage, know) + ((tempo,) if telemetry else ()) for x in containers(pod(n))),
           "only the orchestrator and the shield get the shield's token")

# Every setting the manifests give an agent is one its code reads (a wrong name would be
# ignored without a word: the data would end up in /tmp). Checked when the code is there.
# OTEL_* are read by the OpenTelemetry SDK.
src = env.get("WATTSON_SRC", "src/wattson")
for name, cname in ((orch, "orchestrator"), (triage, "triage"), (know, "knowledge")):
    folder = os.path.join(src, "agents", cname)
    if not os.path.isdir(folder):
        continue
    code = ""
    for base in (folder, os.path.join(src, "shared")):
        for root, _, files in os.walk(base):
            code += "".join(open(os.path.join(root, f), encoding="utf-8").read() for f in files if f.endswith(".py"))
    given = [e["name"] for e in container(name, cname).get("env", []) if e["name"] not in ("HOME", "TMPDIR") and not e["name"].startswith("OTEL_")]
    unread = [n for n in given if f'"{n}"' not in code]
    report(not unread, f"{cname}: the code reads every setting the manifests give it", unread)

for name, port in ((triage, 9001), (know, 9002)):
    s = by[("Service", name)]["spec"]
    report(s["selector"] == {"app": name} and [(p["port"], p["targetPort"]) for p in s["ports"]] == [(port, "a2a")], f"Service {name}: port {port}")
    named = [p for p in container(name, name.rsplit("-", 1)[1])["ports"] if p["name"] == "a2a"]
    report(named and named[0]["containerPort"] == port, f"{name}: the container port a2a is {port}")

# The network rules.
dns_to = [
    {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}
] + ([{"ipBlock": {"cidr": env["NODE_LOCAL_DNS"] + "/32"}}] if env.get("NODE_LOCAL_DNS") else [])
dns = {"to": dns_to, "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}
https = {"ports": [{"protocol": "TCP", "port": 443}]}


def to_pods(app_label, port):
    return {"to": [{"podSelector": {"matchLabels": {"app": app_label}}}], "ports": [{"protocol": "TCP", "port": port}]}


def from_orch(port):
    return [{"from": [{"podSelector": {"matchLabels": {"app": orch}}}], "ports": [{"protocol": "TCP", "port": port}]}]


def sort_rules(rules):
    return sorted(rules, key=lambda r: repr(r))


to_tempo = [to_pods(tempo, 4318)] if telemetry else []


for name in (orch, triage, know):
    np = by[("NetworkPolicy", name)]["spec"]
    report(np["podSelector"] == {"matchLabels": {"app": name}} and sorted(np["policyTypes"]) == ["Egress", "Ingress"], f"NetworkPolicy {name}: covers its pods, in and out")
proxy = {
    "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": env["KUBEAI_NAMESPACE"]}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "kubeai"}}}],
    "ports": [{"protocol": "TCP", "port": 8000}],
}
on = by[("NetworkPolicy", orch)]["spec"]
report(on.get("ingress") == [], "NetworkPolicy orchestrator: nothing comes in")
report(sort_rules(on["egress"]) == sort_rules([to_pods(triage, 9001), to_pods(know, 9002), dns] + ([proxy] if kubeai else []) + to_tempo
                                             + ([to_pods(shield, 9003)] if shield_on else [])),
       "NetworkPolicy orchestrator: out only to triage:9001, knowledge:9002, DNS" + (", the KubeAI proxy pods :8000 (the model of the Guard)" if kubeai else "")
       + (", the shield:9003" if shield_on else "") + (" and tempo:4318" if telemetry else ""), on["egress"])
if shield_on:
    np = by[("NetworkPolicy", shield)]["spec"]
    report(np["podSelector"] == {"matchLabels": {"app": shield}} and sorted(np["policyTypes"]) == ["Egress", "Ingress"], f"NetworkPolicy {shield}: covers its pods, in and out")
    report(np["ingress"] == from_orch(9003), f"NetworkPolicy {shield}: only the orchestrator comes in, on 9003", np["ingress"])
    report(sort_rules(np["egress"]) == sort_rules([dns, https]), f"NetworkPolicy {shield}: out only to DNS and 443 (the download), no traces", np["egress"])
for name, port in ((triage, 9001), (know, 9002)):
    np = by[("NetworkPolicy", name)]["spec"]
    report(np["ingress"] == from_orch(port), f"NetworkPolicy {name}: only the orchestrator comes in, on {port}", np["ingress"])
    if name == triage and kubeai:
        report(sort_rules(np["egress"]) == sort_rules([dns, proxy] + to_tempo),
               f"NetworkPolicy {name}: out only to DNS and the KubeAI proxy pods (:8000, no 443)" + (" and tempo:4318" if telemetry else ""), np["egress"])
    elif name == know and recall:
        report(sort_rules(np["egress"]) == sort_rules([dns, https, to_pods(cognee, 8000)] + to_tempo),
               f"NetworkPolicy {name}: out only to DNS, 443 and {cognee}:8000 (cognee-mcp)" + (" and tempo:4318" if telemetry else ""), np["egress"])
    else:
        report(sort_rules(np["egress"]) == sort_rules([dns, https] + to_tempo),
               f"NetworkPolicy {name}: out only to DNS and 443" + (" and tempo:4318" if telemetry else ""), np["egress"])
if telemetry:
    np = by[("NetworkPolicy", tempo)]["spec"]
    report(np["podSelector"] == {"matchLabels": {"app": tempo}} and sorted(np["policyTypes"]) == ["Egress", "Ingress"], "NetworkPolicy tempo: covers its pods, in and out")
    report(sort_rules(np["ingress"]) == sort_rules([
        {"from": [{"podSelector": {"matchLabels": {"app": n}}} for n in (orch, triage, know)], "ports": [{"protocol": "TCP", "port": 4318}]},
        {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": env["GRAFANA_NAMESPACE"]}}}], "ports": [{"protocol": "TCP", "port": 3200}]},
    ]), f"NetworkPolicy tempo: in, OTLP :4318 from the three agents only, TraceQL :3200 from the namespace {env['GRAFANA_NAMESPACE']} only", np["ingress"])
    report(np["egress"] == [dns], "NetworkPolicy tempo: out, DNS only", np["egress"])
PY

# manifests_pass SETTINGS_FILE RENDERED_FILE -> runs the checks, one line per check
manifests_pass() {
  local line
  while IFS= read -r line; do
    case "$line" in
      ok\|*) ok "${line#ok|}" ;;
      FAIL\|*) fail "${line#FAIL|}" ;;
      *) fail "the manifest check said: $line" ;;
    esac
  done < <(SETTINGS_FILE="$1" IMAGE="$TEST_IMAGE" SHIELD_IMAGE="$TEST_SHIELD_IMAGE" bash -c '. k8s/lib.sh && read_settings && check_settings && export IMAGE SHIELD_IMAGE && python3 "$1" "$2" 2>&1' _ "$WORK/check_manifests.py" "$2")
}

# The settings of this project with the sidecar (the model in the triage pod): the backend the
# checks below and the stand-in runs start from. The project itself runs KubeAI (rendered below).
SIDECAR_SETTINGS="$(settings MODEL_BACKEND=sidecar)"
render "$SIDECAR_SETTINGS" >"$WORK/rendered.yaml" 2>"$WORK/rendered.err"
check "the settings of this project fill in the whole file" test -s "$WORK/rendered.yaml"
manifests_pass "$SIDECAR_SETTINGS" "$WORK/rendered.yaml"

section "The manifests without the optional settings (no pull secret, no node-local DNS)"
BARE="$(settings MODEL_BACKEND=sidecar PULL_SECRET= NODE_LOCAL_DNS=)"
render "$BARE" >"$WORK/rendered-bare.yaml"
check "without PULL_SECRET: no imagePullSecrets" not grep -q imagePullSecrets "$WORK/rendered-bare.yaml"
check "without NODE_LOCAL_DNS: no ipBlock" not grep -q ipBlock "$WORK/rendered-bare.yaml"
check "without them the manifests still pass every check" bash -c '! SETTINGS_FILE="$1" IMAGE="$2" SHIELD_IMAGE="$5" bash -c ". k8s/lib.sh && read_settings && check_settings && export IMAGE SHIELD_IMAGE && python3 \"\$0\" \"\$1\"" "$3" "$4" | grep -q "^FAIL"' \
  _ "$BARE" "$TEST_IMAGE" "$WORK/check_manifests.py" "$WORK/rendered-bare.yaml" "$TEST_SHIELD_IMAGE"

section "The manifests with MODEL_BACKEND=kubeai (KubeAI serves the model)"
KUBEAI_SETTINGS="$(settings MODEL_BACKEND=kubeai)"
render "$KUBEAI_SETTINGS" >"$WORK/rendered-kubeai.yaml" 2>"$WORK/rendered-kubeai.err"
check "the kubeai settings fill in the whole file" test -s "$WORK/rendered-kubeai.yaml"
manifests_pass "$KUBEAI_SETTINGS" "$WORK/rendered-kubeai.yaml"
check "the two backends differ only in the triage pod, its network rule, the models volume, and the triage timeout, the model of the Guard and the way out to KubeAI of the orchestrator" python3 -c '
import sys, yaml
def objects(path):
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(open(path)) if d}
sidecar, kubeai = objects(sys.argv[1]), objects(sys.argv[2])
allowed = {("Deployment", "wattson-triage"), ("NetworkPolicy", "wattson-triage"), ("PersistentVolumeClaim", "wattson-models"), ("Deployment", "wattson-orchestrator"), ("NetworkPolicy", "wattson-orchestrator")}
differ = {key for key in set(sidecar) | set(kubeai) if sidecar.get(key) != kubeai.get(key)}
assert differ == allowed, differ' "$WORK/rendered.yaml" "$WORK/rendered-kubeai.yaml"
# 8000 is also the MCP port of cognee-mcp (COGNEE_RECALL=on): those lines name cognee.
check "the sidecar backend has no KubeAI line" bash -c '! grep -vi cognee "$1" | grep -qE "kubeai|/openai/v1|8000([^m]|$)"' _ "$WORK/rendered.yaml"
check "the pod of the orchestrator carries the SHA-256 of the rules: the same files, the same SHA-256; other rules, another" bash -c '
  sha_of() { grep -m1 "wattson/guard-config:" "$1" | tr -d " \""; }
  render_with() { GUARD_DIR="$1" SETTINGS_FILE="$2" IMAGE="$3" DEPLOY_FUNCTIONS_ONLY=yes bash -c ". k8s/deploy.sh && filled_yaml"; }
  render_with "$GUARD_DIR" "$1" "$2" >"$4/rendered-again.yaml" && render_with "$5" "$1" "$2" >"$4/rendered-other-rules.yaml" &&
  [[ "$(sha_of "$4/rendered-again.yaml")" == "$(sha_of "$3")" && "$(sha_of "$4/rendered-other-rules.yaml")" != "$(sha_of "$3")" ]]' \
  _ "$SIDECAR_SETTINGS" "$TEST_IMAGE" "$WORK/rendered.yaml" "$WORK" "$WORK/guard-other"
check "wrong rules: deploy.sh --dry-run refuses them, and prints no file" bash -c '
  ! out="$(GUARD_DIR="$1" SETTINGS_FILE="$2" bash k8s/deploy.sh --dry-run 2>&1)" && grep -q "^PROBLEM: " <<<"$out" && ! grep -q "^kind:" <<<"$out"' \
  _ "$(guard_variant dry-bad "rules: [{name: x, label: nope, match: [rm], reason: Risky.}]")" "$SIDECAR_SETTINGS"

section "The KubeAI Model and Helm values: KubeAI's llama.cpp engine, as the measured sidecar"
# kubeai.sh without --apply prints them filled in from settings.env; it calls no kubectl, no helm.
SETTINGS_FILE=settings.env bash k8s/kubeai.sh >"$WORK/kubeai-plan.txt" 2>&1
check "kubeai.sh prints the plan for the settings of this project" grep -q "Nothing was run" "$WORK/kubeai-plan.txt"
cat >"$WORK/check_kubeai.py" <<'CHECK'
import os, re, sys, yaml
plan, sidecar_file = sys.argv[1:]
env = os.environ
parts = open(plan).read().split("\n---\n")
values = yaml.safe_load(parts[1])
model = yaml.safe_load(parts[2])
spec = model["spec"]
def report(ok, what, seen=None):
    print(("ok|" if ok else "FAIL|") + what + ("" if ok or seen is None else ": %s" % (seen,)))
report(model["kind"] == "Model" and model["metadata"]["name"] == env["KUBEAI_MODEL"], "the Model is named KUBEAI_MODEL (the triage agent sends it as \"model\")", model["metadata"])
report(spec["engine"] == "LlamaCpp", "the Model runs on KubeAI's llama.cpp engine", spec["engine"])
# KubeAI 0.24.0: its CRD takes hf://, pvc:// or oci:// for LlamaCpp; its URL parser, this regex.
report(re.fullmatch(r"([a-z0-9]+)://([a-zA-Z0-9._:/-]+)(\?.*)?", spec["url"]) is not None and spec["url"].startswith("hf://"),
       "its url is one KubeAI's llama.cpp engine takes (hf://)", spec["url"])
report(spec["url"] == "hf://" + env["MODEL_HF_REPO"] and env["MODEL_URL"].startswith("https://huggingface.co/%s/resolve/" % env["MODEL_HF_REPO"]),
       "its url is the Hugging Face repo of MODEL_URL", spec["url"])
args = [str(a) for a in spec["args"]]
report(args[:2] == ["--hf-file", env["MODEL_FILE"]], "the file is MODEL_FILE (--hf-file), the same file as the sidecar", args[:2])
measured = ["-c", env["LLAMA_CTX"], "-t", env["LLAMA_THREADS"], "-tb", env["LLAMA_BATCH_THREADS"], "-np", "2", "-b", "512", "-ub", "128",
            "-lm", "none", "--reasoning", "on", "--cache-ram", "0", "--no-webui", "--no-slots"]
report(args[2:] == measured,
       "the flags are the measured ones: -c 4096 -t 4 -tb 6 -np 2 -b 512 -ub 128 -lm none --reasoning on --cache-ram 0 --no-webui --no-slots", args[2:])
# The same as the llama container of the sidecar (k8s/wattson.yaml), but for the model path, the
# host and the port, which KubeAI sets itself (-hf, --host 0.0.0.0, --port 8000), and --reasoning:
# on for Granite-4.2 with KubeAI (26 Oct 2026), still off in the sidecar.
def pairs(words):
    out, i = set(), 0
    while i < len(words):
        if words[i].startswith("--no-"):
            out.add((words[i],))
            i += 1
        else:
            out.add((words[i], words[i + 1]))
            i += 2
    return out
llama = next(c for d in yaml.safe_load_all(open(sidecar_file)) if d and d["kind"] == "Deployment"
             for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == "llama")
side = {pair for pair in pairs([str(a) for a in llama["args"]]) if pair[0] not in ("-m", "--host", "--port", "--reasoning")}
kube = {pair for pair in pairs(args[2:]) if pair[0] != "--reasoning"}
report(side == kube, "the flags are those of the sidecar's llama container (k8s/wattson.yaml), but -m, --host, --port and --reasoning", sorted(side ^ kube))
report(not any(a in args for a in ("-m", "--model", "-hf", "--hf-repo", "--host", "--port", "--alias")),
       "no flag that KubeAI sets itself (-hf, --host, --port, --alias)", args)
report(not spec.get("env"), "no env: nothing of Ollama (OLLAMA_*, HOME) and no LLAMA_ARG_*", spec.get("env"))
report(spec["minReplicas"] == int(env["KUBEAI_MIN_REPLICAS"]) and spec["maxReplicas"] == 1
       and spec["scaleDownDelaySeconds"] == int(env["KUBEAI_SCALE_DOWN_DELAY_SECONDS"]),
       "scale to zero is kept: minReplicas KUBEAI_MIN_REPLICAS, maxReplicas 1, scaleDownDelaySeconds", spec)
report(spec["resourceProfile"] == "cpu-burst:4", "the resource profile cpu-burst:4 (the 3B model)", spec["resourceProfile"])
servers = values["modelServers"]
report(list(servers) == ["LlamaCpp"] and servers["LlamaCpp"]["images"] == {"default": env["LLAMA_IMAGE"], "cpu": env["LLAMA_IMAGE"]},
       "the Helm values: the llama.cpp image is LLAMA_IMAGE (pinned), and no Ollama image", servers)
# KubeAI multiplies the profile by its count (cpu-burst:4): 3 CPU and 3 GiB reserved, 8 CPU and 6 GiB at most.
def cpu(v):
    v = str(v)
    return int(v[:-1]) if v.endswith("m") else int(float(v) * 1000)
def mib(v):
    return int(v[:-2]) * (1024 if v.endswith("Gi") else 1)
name, count = spec["resourceProfile"].split(":")
profile = values["resourceProfiles"][name]
report(profile["imageName"] == "cpu", "the profile takes the \"cpu\" image", profile)
n = int(count)
seen = (cpu(profile["requests"]["cpu"]) * n, mib(profile["requests"]["memory"]) * n, cpu(profile["limits"]["cpu"]) * n, mib(profile["limits"]["memory"]) * n)
report(seen == (3000, 3072, 8000, 6144), "4 x the profile: 3 CPU and 3 GiB reserved, 8 CPU and 6 GiB at most", seen)
report(int(env["LLAMA_BATCH_THREADS"]) * 1000 <= seen[2] and int(env["LLAMA_THREADS"]) * 1000 <= seen[2], "the threads fit the CPU limit of the model pod", seen[2])
sc = values["modelServerPods"]["securityContext"]
report(sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False and sc["capabilities"]["drop"] == ["ALL"]
       and values["modelServerPods"]["podSecurityContext"]["runAsNonRoot"] is True,
       "the model pod: not root, read-only root filesystem, no capabilities", values["modelServerPods"])
CHECK
while IFS= read -r line; do
  case "$line" in
    ok\|*) ok "${line#ok|}" ;;
    FAIL\|*) fail "${line#FAIL|}" ;;
    *) fail "the KubeAI check said: $line" ;;
  esac
done < <(SETTINGS_FILE=settings.env bash -c '. k8s/lib.sh && read_settings && check_settings && python3 "$1" "$2" "$3" 2>&1' _ "$WORK/check_kubeai.py" "$WORK/kubeai-plan.txt" "$WORK/rendered.yaml")
check "no Ollama is left in the KubeAI files and scripts (only comments about the past)" bash -c '
  ! grep -v "^ *#" k8s/kubeai/*.yaml | grep -iqE "ollama|LLAMA_ARG_" &&
  ! grep -hv "^[[:space:]]*#" k8s/kubeai.sh k8s/doctor.sh k8s/lib.sh settings.env | grep -iqE "ollama|LLAMA_ARG_"'

section "The manifests with TELEMETRY=off (no Tempo, no traces)"
OFF_SETTINGS="$(settings MODEL_BACKEND=sidecar TELEMETRY=off)"
render "$OFF_SETTINGS" >"$WORK/rendered-off.yaml" 2>"$WORK/rendered-off.err"
check "the off settings fill in the whole file" test -s "$WORK/rendered-off.yaml"
manifests_pass "$OFF_SETTINGS" "$WORK/rendered-off.yaml"
check "on and off differ only in Tempo's objects, the OTEL_ settings of the three agents and their way out to Tempo" python3 -c '
import sys, yaml
def objects(path):
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(open(path)) if d}
on, off = objects(sys.argv[1]), objects(sys.argv[2])
tempo = {key for key in on if key[1] in ("wattson-tempo", "wattson-tempo-data")}
assert set(on) - set(off) == tempo and len(tempo) == 6 and set(off) <= set(on), set(on) ^ set(off)
for key in off:
    doc = on[key]
    if key[0] == "Deployment":
        for c in doc["spec"]["template"]["spec"]["containers"]:
            c["env"] = [e for e in c.get("env", []) if not e["name"].startswith("OTEL_")]
    if key[0] == "NetworkPolicy":
        doc["spec"]["egress"] = [r for r in doc["spec"]["egress"] if r.get("to") != [{"podSelector": {"matchLabels": {"app": "wattson-tempo"}}}]]
    assert doc == off[key], key' "$WORK/rendered.yaml" "$WORK/rendered-off.yaml"
check "the pod of Tempo carries the SHA-256 of its config: the same settings, the same SHA-256; another retention, another" bash -c '
  sha() { grep -o "wattson/tempo-config: \"[0-9a-f]*\"" "$1"; }
  render2="$(mktemp)"; render3="$(mktemp)"
  SETTINGS_FILE="$2" IMAGE="$3" DEPLOY_FUNCTIONS_ONLY=yes bash -c ". k8s/deploy.sh && filled_yaml" >"$render2"
  SETTINGS_FILE="$4" IMAGE="$3" DEPLOY_FUNCTIONS_ONLY=yes bash -c ". k8s/deploy.sh && filled_yaml" >"$render3"
  [[ -n "$(sha "$1")" && "$(sha "$1")" == "$(sha "$render2")" && "$(sha "$1")" != "$(sha "$render3")" ]]' \
  _ "$WORK/rendered.yaml" "$SIDECAR_SETTINGS" "$TEST_IMAGE" "$(settings MODEL_BACKEND=sidecar TEMPO_RETENTION=720h)"

section "The manifests with INJECTION_MODE=off (no shield)"
NO_SHIELD_SETTINGS="$(settings MODEL_BACKEND=sidecar INJECTION_MODE=off)"
render "$NO_SHIELD_SETTINGS" >"$WORK/rendered-no-shield.yaml" 2>"$WORK/rendered-no-shield.err"
check "the settings without the shield fill in the whole file" test -s "$WORK/rendered-no-shield.yaml"
manifests_pass "$NO_SHIELD_SETTINGS" "$WORK/rendered-no-shield.yaml"
check "with and without differ only in the shield's objects, the orchestrator's settings for it and its way out to it" python3 -c '
import sys, yaml
def objects(path):
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(open(path)) if d}
on, off = objects(sys.argv[1]), objects(sys.argv[2])
shield = {key for key in on if key[1] == "wattson-shield"}
assert set(on) - set(off) == shield and len(shield) == 4 and set(off) <= set(on), set(on) ^ set(off)
for key in off:
    doc = on[key]
    if key == ("Deployment", "wattson-orchestrator"):
        c = doc["spec"]["template"]["spec"]["containers"][0]
        c["env"] = [e for e in c["env"] if e["name"] not in ("SHIELD_URL", "SHIELD_TOKEN", "INJECTION_MODEL", "INJECTION_THRESHOLD", "INJECTION_TIMEOUT_S")]
        next(e for e in c["env"] if e["name"] == "INJECTION_MODE")["value"] = "off"
    if key == ("NetworkPolicy", "wattson-orchestrator"):
        doc["spec"]["egress"] = [r for r in doc["spec"]["egress"] if r.get("to") != [{"podSelector": {"matchLabels": {"app": "wattson-shield"}}}]]
    assert doc == off[key], key' "$WORK/rendered.yaml" "$WORK/rendered-no-shield.yaml"

section "The manifests with COGNEE_RECALL=off (no \"seen before\")"
check "this project reads the lessons of Cognee memory (COGNEE_RECALL=on, since 29 Oct 2026), at most 3, waiting 5 s" bash -c '
  [[ "$(value_of settings.env COGNEE_RECALL)" == on && "$(value_of settings.env COGNEE_RECALL_URL)" == http://wattson-cognee:8000/mcp ]] &&
  [[ "$(value_of settings.env COGNEE_RECALL_DATASET)" == claude_code_memory && "$(value_of settings.env COGNEE_RECALL_LESSONS)" == 3 ]] &&
  [[ "$(value_of settings.env COGNEE_RECALL_TIMEOUT_S)" == 5 ]]'
check "an empty COGNEE_RECALL_URL is the Service of the pod <APP_NAME>-cognee" bash -c '[[ "$(value_of "$1" COGNEE_RECALL_URL)" == http://wattson-cognee:8000/mcp ]]' _ "$(settings COGNEE_RECALL_URL=)"
for bad in COGNEE_RECALL=yes COGNEE_RECALL_URL=https://wattson-cognee:8000/mcp COGNEE_RECALL_URL=http://evil:8000/mcp \
  COGNEE_RECALL_URL=http://wattson-cognee:8001/mcp COGNEE_RECALL_URL=http://wattson-cognee.other-namespace:8000/mcp \
  'COGNEE_RECALL_URL=http://wattson-cognee:8000/mcp;id' COGNEE_RECALL_DATASET=a,b 'COGNEE_RECALL_DATASET=a b' \
  COGNEE_RECALL_LESSONS=0 COGNEE_RECALL_LESSONS=4 COGNEE_RECALL_TIMEOUT_S=0 COGNEE_RECALL_TIMEOUT_S=11 COGNEE_RECALL_TIMEOUT_S=2.5; do
  check "the settings refuse $bad" not checked "$(settings "$bad")"
done
NO_RECALL_SETTINGS="$(settings MODEL_BACKEND=sidecar COGNEE_RECALL=off)"
render "$NO_RECALL_SETTINGS" >"$WORK/rendered-no-recall.yaml" 2>"$WORK/rendered-no-recall.err"
check "the settings without \"seen before\" fill in the whole file" test -s "$WORK/rendered-no-recall.yaml"
manifests_pass "$NO_RECALL_SETTINGS" "$WORK/rendered-no-recall.yaml"
check "with and without differ only in the knowledge agent's settings for Cognee and its way out to it" python3 -c '
import sys, yaml
def objects(path):
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(open(path)) if d}
on, off = objects(sys.argv[1]), objects(sys.argv[2])
assert set(on) == set(off), set(on) ^ set(off)
for key in off:
    doc = on[key]
    if key == ("Deployment", "wattson-knowledge"):
        c = doc["spec"]["template"]["spec"]["containers"][0]
        c["env"] = [e for e in c["env"] if e["name"] in ("COGNEE_RECALL",) or not e["name"].startswith("COGNEE_")]
        next(e for e in c["env"] if e["name"] == "COGNEE_RECALL")["value"] = "off"
    if key == ("NetworkPolicy", "wattson-knowledge"):
        doc["spec"]["egress"] = [r for r in doc["spec"]["egress"] if r.get("to") != [{"podSelector": {"matchLabels": {"app": "wattson-cognee"}}}]]
    assert doc == off[key], key' "$WORK/rendered.yaml" "$WORK/rendered-no-recall.yaml"
check "without \"seen before\" no line but comments names cognee, but the knowledge agent's COGNEE_RECALL off" bash -c '
  [[ "$(grep -v "^ *#" "$1" | grep -ci cognee)" == 1 ]] && grep -q "name: COGNEE_RECALL, value: \"off\"" "$1"' _ "$WORK/rendered-no-recall.yaml"

# ---- the Grafana dashboard ------------------------------------------------------------
section "The Grafana dashboard (k8s/grafana/wattson-dashboard.json)"

# The spans the code sends, and the attributes of each (the contract with src/wattson). The
# whole numbers among them (Tempo 3.1 computes quantiles only of durations and whole numbers):
# wait_ms, triage.confidence_pct, knowledge.docs, guard.rule_hits, guard.latency_ms, guard.evidence_removed.
cat >"$WORK/contract.json" <<'JSON'
{
  "wattson.step": ["wattson.task_id", "wattson.step", "wattson.workflow", "wattson.status", "wattson.interrupt", "wattson.wait_s", "wattson.wait_ms"],
  "wattson.task.end": ["wattson.task_id", "wattson.outcome", "wattson.workflow", "wattson.triage.category", "wattson.triage.confidence",
                       "wattson.triage.confidence_pct", "wattson.triage.method", "wattson.task_duration_s"],
  "wattson.model.classify": ["wattson.model.name", "wattson.model.ok", "wattson.triage.confidence_pct"],
  "wattson.model.hints": ["wattson.model.name", "wattson.model.ok"],
  "wattson.grimoire.call": ["wattson.grimoire.tool", "wattson.grimoire.ok"],
  "wattson.knowledge.retrieve": ["wattson.knowledge.docs"],
  "wattson.guard.request": ["wattson.task_id", "wattson.guard.label", "wattson.guard.method", "wattson.guard.mode", "wattson.guard.rule_hits",
                            "wattson.guard.latency_ms", "wattson.guard.injection_model", "wattson.guard.injection_ok",
                            "wattson.guard.injection_flagged", "wattson.guard.injection_score_pct", "wattson.guard.injection_latency_ms"],
  "wattson.guard.evidence": ["wattson.task_id", "wattson.guard.evidence_removed", "wattson.guard.evidence_rules", "wattson.guard.injection_model",
                             "wattson.guard.injection_mode", "wattson.guard.injection_ok", "wattson.guard.injection_parts",
                             "wattson.guard.injection_flagged", "wattson.guard.injection_latency_ms"]
}
JSON
check "it is valid JSON: uid wattson, title Wattson Agent, a title and a plain description on every panel" python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
assert d["uid"] == "wattson" and d["title"] == "Wattson Agent" and d["panels"], d.get("uid")
assert all(p.get("title") and len(p.get("description", "")) > 40 for p in d["panels"]), [p.get("title") for p in d["panels"]]
assert len({p["id"] for p in d["panels"]}) == len(d["panels"])' k8s/grafana/wattson-dashboard.json
check "every panel and every query uses the Tempo data source wattson-tempo, and nothing else" python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
tempo = {"type": "tempo", "uid": "wattson-tempo"}
assert all(p["datasource"] == tempo and p["targets"] and all(t["datasource"] == tempo for t in p["targets"]) for p in d["panels"])
assert "${" not in json.dumps(d).replace("${__", "")' k8s/grafana/wattson-dashboard.json
check "it has the panels asked for: outcomes per hour, triage method and confidence, waits (p50/p95), model calls, Grimoire, recent tasks" python3 -c '
import json, sys
queries = [t["query"] for p in json.load(open(sys.argv[1]))["panels"] for t in p["targets"]]
def has(*words):
    return any(all(w in q for w in words) for q in queries)
assert has("wattson.task.end", "count_over_time()", "span.wattson.outcome")
assert has("wattson.task.end", "span.wattson.triage.method")
# Confidence: average and lowest of the whole percent (quantiles of a 0..100 value in power-of-two buckets read up to 128 %).
assert has("wattson.task.end", "avg_over_time(span.wattson.triage.confidence_pct)") and has("wattson.task.end", "min_over_time(span.wattson.triage.confidence_pct)")
assert has("wattson.step", "quantile_over_time(span.wattson.wait_ms, .5, .95)", "span.wattson.interrupt")
assert has("wattson.model.classify", "wattson.model.hints", "quantile_over_time(duration, .5, .95)")
assert has("wattson.grimoire.call", "span.wattson.grimoire.ok")
# You removed the panel of the time of each step (Grafana, 27 Oct 2026): it stays out.
assert not has("wattson.step", "quantile_over_time(duration")
assert has("wattson.task.end", "select(span.wattson.task_id, span.wattson.workflow, span.wattson.outcome, span.wattson.task_duration_s)")' k8s/grafana/wattson-dashboard.json
check "it has the panels of the Guard: decisions by label per 15 minutes, blocked and confirmed tasks, evidence parts removed, the injection model" python3 -c '
import json, sys
panels = {p["title"]: p for p in json.load(open(sys.argv[1]))["panels"]}
labels = panels["Guard decisions, by label"]
assert labels["type"] == "timeseries" and labels["targets"][0]["step"] == "15m" and labels["targets"][0]["metricsQueryType"] == "range"
assert labels["targets"][0]["query"] == "{name=\"wattson.guard.request\"} | count_over_time() by (span.wattson.guard.label)"
assert {o["matcher"]["options"] for o in labels["fieldConfig"]["overrides"]} == {"work", "out_of_scope", "risky", "malicious"}
stopped = [t["query"] for t in panels["Blocked and confirmed tasks"]["targets"]]
assert "{name=\"wattson.task.end\" && span.wattson.outcome=\"blocked\"} | count_over_time()" in stopped, stopped
assert "{name=\"wattson.step\" && span.wattson.interrupt=\"guard_confirm\"} | count_over_time()" in stopped, stopped
removed = panels["Evidence parts removed (possible prompt injection)"]["targets"][0]
assert removed["query"] == "{name=\"wattson.guard.evidence\"} | sum_over_time(span.wattson.guard.evidence_removed)" and removed["step"] == "15m"
for title in ("Tasks in this time range, by outcome", "Tasks per 15 minutes, by outcome"):
    assert "blocked" in {o["matcher"]["options"] for o in panels[title]["fieldConfig"]["overrides"]}, title
flagged = [t["query"] for t in panels["Injection model: parts flagged"]["targets"]]
assert flagged == ["{name=\"wattson.guard.evidence\"} | sum_over_time(span.wattson.guard.injection_flagged)",
                   "{name=\"wattson.guard.evidence\" && span.wattson.guard.injection_ok=false} | count_over_time()",
                   "{name=\"wattson.guard.request\"} | sum_over_time(span.wattson.guard.injection_flagged)"], flagged' k8s/grafana/wattson-dashboard.json
check "the dashboard still shows the last 24 hours by default, and its panels do not overlap" python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
assert d["time"] == {"from": "now-24h", "to": "now"}, d["time"]
cells = set()
for p in d["panels"]:
    g = p["gridPos"]
    assert g["x"] + g["w"] <= 24, p["title"]
    mine = {(x, y) for x in range(g["x"], g["x"] + g["w"]) for y in range(g["y"], g["y"] + g["h"])}
    assert not cells & mine, p["title"]
    cells |= mine' k8s/grafana/wattson-dashboard.json
check "every query names only spans and attributes the code sends (each attribute on its own span)" python3 -c '
import json, re, sys
d = json.load(open(sys.argv[1]))
contract = json.load(open(sys.argv[2]))
for p in d["panels"]:
    for t in p["targets"]:
        q = t["query"]
        names = re.findall(r"\bname\s*=\s*\"([^\"]+)\"", q)
        attributes = set(re.findall(r"\bspan\.([a-z_.]+[a-z_])", q))
        assert names and all(n in contract for n in names), (p["title"], names)
        for a in attributes:
            assert all(a in contract[n] for n in names), (p["title"], a, names)
        assert not re.search(r"\b(resource|event|link|instrumentation)\.", q), (p["title"], q)
        assert not re.findall(r"\b(?<!span\.)wattson\.[a-z_.]+", re.sub(r"\"[^\"]*\"|span\.[a-z_.]+", "", q)), (p["title"], q)' k8s/grafana/wattson-dashboard.json "$WORK/contract.json"
check "quantiles only of durations and whole numbers: Tempo 3.1 has none of a float (wait_s and confidence are floats)" python3 -c '
import json, re, sys
d = json.load(open(sys.argv[1]))
whole = {"duration", "span.wattson.wait_ms", "span.wattson.triage.confidence_pct", "span.wattson.knowledge.docs",
         "span.wattson.guard.rule_hits", "span.wattson.guard.latency_ms", "span.wattson.guard.evidence_removed",
         "span.wattson.guard.injection_parts", "span.wattson.guard.injection_flagged", "span.wattson.guard.injection_latency_ms",
         "span.wattson.guard.injection_score_pct"}
for q in (t["query"] for p in d["panels"] for t in p["targets"]):
    for args in re.findall(r"(?:quantile|histogram)_over_time\(([^,)]*)", q):
        assert args.strip() in whole, q
    assert not re.search(r"(wait_s|confidence|task_duration_s)\s*[<>=!]+\s*[0-9]+(?![.0-9])", q), q' k8s/grafana/wattson-dashboard.json
check "the recent tasks table is a TraceQL search of the end of each task, with a link to its trace" python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
table = next(p for p in d["panels"] if p["type"] == "table")
t = table["targets"][0]
assert t["queryType"] == "traceql" and "metricsQueryType" not in t and t["tableType"] == "spans" and t["limit"] >= 20
names = table["transformations"][0]["options"]["renameByName"]
assert names["Span ID"] == "Open the trace" and "traceIdHidden" not in table["transformations"][0]["options"]["excludeByName"]' k8s/grafana/wattson-dashboard.json

# ---- the stand-ins for kubectl, docker and curl ------------------------------------
#
# kubectl keeps a made-up namespace (a folder of small JSON files) and writes down every
# call. It checks itself that each call names the context and namespace of the test
# settings, and that no value of a Secret is ever on a command line.

mkdir -p "$WORK/bin"
cat >"$WORK/bin/kubectl" <<'STUB'
#!/usr/bin/env python3
import base64, json, os, re, sys
import yaml

STATE, LOG = os.environ["STUB_STATE"], os.environ["STUB_LOG"]
KINDS = {"deploy": "deployment", "deployments": "deployment", "svc": "service", "services": "service", "pvc": "persistentvolumeclaim",
         "sa": "serviceaccount", "netpol": "networkpolicy", "ns": "namespace", "sc": "storageclass", "pod": "pods",
         "models.kubeai.org": "model", "models": "model"}
# The namespaces of KubeAI and of Grafana in the test settings. Their calls are written down
# with " in <it>".
KUBEAI_NS = "test-kubeai"
GRAFANA_NS = "test-grafana"

def say(line):
    with open(LOG, "a") as log:
        log.write(line + "\n")

def kind_of(word):
    word = word.lower()
    return KINDS.get(word, word)

def path(kind, name):
    return os.path.join(STATE, "%s__%s.json" % (kind_of(kind), name))

def load(kind, name):
    with open(path(kind, name)) as file:
        return json.load(file)

def store(kind, name, label="", values=None, doc=None):
    with open(path(kind, name), "w") as file:
        json.dump({"label": label, "values": values or {}, "doc": doc}, file)

def objects():
    for file in sorted(os.listdir(STATE)):
        if file.endswith(".json"):
            kind, name = file[:-5].split("__", 1)
            yield kind, name, load(kind, name)

args = sys.argv[1:]
# No value of a Secret is ever on a command line.
for _, _, thing in objects():
    for value in thing.get("values", {}).values():
        if len(value) >= 16 and any(value in arg for arg in args):
            say("A-SECRET-ON-A-COMMAND-LINE")

context = namespace = None
rest = []
while args:
    word = args.pop(0)
    if word == "--context":
        context = args.pop(0)
    elif word in ("-n", "--namespace"):
        namespace = args.pop(0)
    else:
        rest.append(word)
if rest[:2] == ["config", "view"]:
    say("config view --context %s" % context)
    if context == "test-context":
        sys.stdout.write("test-context")
        sys.exit(0)
    sys.stderr.write("error: cannot locate context %s\n" % context)
    sys.exit(1)
# Objects of the whole cluster: only get and delete, and never with a namespace.
CLUSTER_KINDS = ("crd", "clusterrole", "clusterrolebinding")
cluster_wide = len(rest) > 1 and rest[0] in ("get", "delete") and kind_of(rest[1]) in CLUSTER_KINDS
if cluster_wide and namespace is not None:
    say("A-CLUSTER-OBJECT-WITH-A-NAMESPACE: %s" % " ".join(sys.argv[1:5]))
    sys.exit(1)
if context != "test-context" or (namespace not in ("test-ns", KUBEAI_NS, GRAFANA_NS) and not (cluster_wide and namespace is None)):
    say("THE-WRONG-CLUSTER-OR-NAMESPACE: %s" % " ".join(sys.argv[1:5]))
    sys.exit(1)
verb, rest = rest[0], rest[1:]
where = " in %s" % namespace if namespace in (KUBEAI_NS, GRAFANA_NS) else ""
_say = say
def say(line):
    _say(line + where)

def option(name, default=""):
    return rest[rest.index(name) + 1] if name in rest else default

def positional():
    out, skip = [], False
    for word in rest:
        if skip:
            skip = False
            continue
        if word in ("-o", "-l", "-c", "--sort-by", "--tail", "--address", "--timeout", "-p", "--type"):
            skip = True
            continue
        if word.startswith("-"):
            continue
        out.append(word)
    return out

if verb == "get":
    words = positional()
    kind = kind_of(words[0])
    name = words[1] if len(words) > 1 else None
    output = option("-o")
    say("get %s%s%s" % (kind, "/" + name if name else "", " -l " + option("-l") if "-l" in rest else ""))
    if os.environ.get("STUB_BROKEN") == "%s/%s" % (kind, name):
        sys.stderr.write("Unable to connect to the server: the stand-in was told to fail here\n")
        sys.exit(1)
    if kind == "model" and os.environ.get("STUB_NO_KUBEAI_CRD"):
        sys.stderr.write('error: the server doesn\'t have a resource type "models"\n')
        sys.exit(1)
    if name is None:
        # Lists: pods of a Deployment, or of a KubeAI Model (STUB_MODEL_PODS=1), events, ingresses.
        if kind == "pods" and option("-l").startswith("model="):
            model = option("-l").split("=", 1)[1]
            if not (os.path.exists(path("model", model)) and os.environ.get("STUB_MODEL_PODS") == "1"):
                sys.exit(0)
            if output == "jsonpath={.items[0].metadata.name}":
                sys.stdout.write("model-%s-7f9c6d5b4-xyz12" % model)
            elif output.startswith("jsonpath="):
                print("  server: ready=true restarts=0 waiting= last=")
            else:
                print("model-%s-7f9c6d5b4-xyz12   1/1   Running   0   1m" % model)
            sys.exit(0)
        if kind == "pods":
            app = option("-l").split("=", 1)[1]
            if not os.path.exists(path("deployment", app)):
                sys.exit(0)
            if output.startswith("jsonpath="):
                print("  %s: ready=true restarts=0 waiting= last=" % app.rsplit("-", 1)[1])
            else:
                print("%s-5d4f8b9c7-abcde   1/1   Running   0   1m" % app)
            sys.exit(0)
        if kind == "ingress" and namespace == GRAFANA_NS:
            # The Ingress of Grafana: its host and the Service behind it.
            print("grafana.example.test monitoring-grafana ")
            sys.exit(0)
        if kind in ("events", "ingress"):
            sys.exit(0)
        say("A-LIST-THE-STAND-IN-DOES-NOT-KNOW: %s" % kind)
        sys.exit(1)
    if kind == "pods" and name.startswith("model-") and os.environ.get("STUB_MODEL_PODS") == "1":
        # The model pod of a KubeAI Model: its image (STUB_MODEL_IMAGE) and whether its server is
        # ready (STUB_MODEL_READY, default true).
        if output == "jsonpath={.spec.containers[0].image}":
            sys.stdout.write(os.environ.get("STUB_MODEL_IMAGE", "the-image"))
        elif output == "jsonpath={.status.containerStatuses[0].ready}":
            sys.stdout.write(os.environ.get("STUB_MODEL_READY", "true"))
        else:
            say("AN-OUTPUT-THE-STAND-IN-DOES-NOT-KNOW: %s %s" % (kind, output))
            sys.exit(1)
        sys.exit(0)
    if not os.path.exists(path(kind, name)):
        if "--ignore-not-found" in rest:
            sys.exit(0)
        sys.stderr.write('Error from server (NotFound): %s "%s" not found\n' % (kind, name))
        sys.exit(1)
    thing = load(kind, name)
    if output == "name":
        print("%s/%s" % (kind, name))
    elif output.startswith("go-template="):
        print("\n".join(sorted(thing["values"])))
    elif output == 'jsonpath={.metadata.name}{"|"}{.metadata.labels.app\\.kubernetes\\.io/part-of}':
        sys.stdout.write("%s|%s" % (name, thing["label"]))
    elif output.startswith("jsonpath={.data."):
        sys.stdout.write(base64.b64encode(thing["values"][output[len("jsonpath={.data."):-1]].encode()).decode())
    elif kind == "persistentvolumeclaim" and output.startswith("jsonpath="):
        sys.stdout.write("Bound 1Gi my-storage-class")
    elif kind == "deployment" and ".env[*]" in output:
        # Where the Grafana Deployment takes its admin login from (names only).
        # STUB_GRAFANA_LITERAL_LOGIN: from plain values instead of a Secret.
        if os.environ.get("STUB_GRAFANA_LITERAL_LOGIN"):
            print("GF_SECURITY_ADMIN_USER  \nGF_SECURITY_ADMIN_PASSWORD  ")
        else:
            print("POD_IP  \nGF_SECURITY_ADMIN_USER %s admin-user\nGF_SECURITY_ADMIN_PASSWORD %s admin-password\nGF_PATHS_DATA  " % (name, name))
    elif kind == "service" and output == "jsonpath={.spec.ports[0].port}":
        sys.stdout.write("80")
    elif kind == "deployment" and output == "jsonpath={.spec.template.spec.volumes[*].configMap.name}":
        # The ConfigMaps the pod mounts, from the applied Deployment (guard.sh).
        spec = ((thing.get("doc") or {}).get("spec") or {}).get("template", {}).get("spec", {})
        sys.stdout.write(" ".join(v["configMap"]["name"] for v in spec.get("volumes", []) if "configMap" in v))
    elif kind == "deployment" and output == "jsonpath={.spec.template.metadata.annotations.wattson/guard-config}":
        # The SHA-256 of the rules of the Guard that the pod carries (guard.sh, doctor.sh).
        meta = ((thing.get("doc") or {}).get("spec") or {}).get("template", {}).get("metadata", {})
        sys.stdout.write((meta.get("annotations") or {}).get("wattson/guard-config", ""))
    elif kind == "deployment" and re.fullmatch(r'jsonpath=\{\.spec\.template\.spec\.containers\[0\]\.env\[\?\(@\.name=="[A-Z_]+"\)\]\.value\}', output):
        # One setting of the first container (GUARD_MODE, INJECTION_MODE), from the applied Deployment.
        wanted = output.split('"')[1]
        spec = ((thing.get("doc") or {}).get("spec") or {}).get("template", {}).get("spec", {})
        containers = spec.get("containers") or [{}]
        sys.stdout.write(next((e.get("value", "") for e in containers[0].get("env", []) if e["name"] == wanted), ""))
    elif kind == "deployment" and output.startswith("jsonpath="):
        print("  image %s: %s" % (name, "the-image"))
    elif kind == "crd" and output.startswith("jsonpath="):
        # Who installed the CRD (namespace/release): STUB_CRD_OWNER.
        sys.stdout.write(os.environ.get("STUB_CRD_OWNER", "%s/kubeai" % KUBEAI_NS))
    elif kind == "model" and output == "jsonpath={.spec.replicas}/{.status.replicas.ready}":
        sys.stdout.write(os.environ.get("STUB_MODEL_REPLICAS", "0/"))
    elif kind == "model" and output == "jsonpath={.spec.engine}":
        # The engine of the Model: the applied one, or STUB_MODEL_ENGINE (default LlamaCpp).
        sys.stdout.write((thing.get("doc") or {}).get("spec", {}).get("engine") or os.environ.get("STUB_MODEL_ENGINE", "LlamaCpp"))
    elif kind == "model" and output.startswith("jsonpath="):
        sys.stdout.write("engine=%s hf://ibm-granite/granite-4.2-3b-GGUF cpu-burst:4 minReplicas=0 scaleDownDelaySeconds=3600" % os.environ.get("STUB_MODEL_ENGINE", "LlamaCpp"))
    else:
        say("AN-OUTPUT-THE-STAND-IN-DOES-NOT-KNOW: %s %s" % (kind, output))
        sys.exit(1)
    sys.exit(0)

if verb in ("apply", "create") and rest[:2] == ["-f", "-"]:
    text = sys.stdin.read()
    if verb == "apply":
        # STUB_APPLY_FAILS=N: the first N applies time out, the way etcd does. STUB_APPLY_ERROR: one that is no timeout.
        count_file = os.path.join(STATE, "apply-count")
        count = int(open(count_file).read()) + 1 if os.path.exists(count_file) else 1
        open(count_file, "w").write(str(count))
        say("apply try %d" % count)
        if count <= int(os.environ.get("STUB_APPLY_FAILS", "0")):
            sys.stderr.write("Error from server: etcdserver: request timed out\n")
            sys.exit(1)
        if os.environ.get("STUB_APPLY_ERROR"):
            sys.stderr.write('The Deployment "x" is invalid: spec.template: Invalid value\n')
            sys.exit(1)
    docs = [doc for doc in yaml.safe_load_all(text) if doc]
    for doc in docs:
        kind, name = doc["kind"].lower(), doc["metadata"]["name"]
        if doc["metadata"].get("namespace", namespace) != namespace:
            say("AN-OBJECT-FOR-ANOTHER-NAMESPACE: %s/%s" % (kind, name))
            sys.exit(1)
        if verb == "create" and os.path.exists(path(kind, name)):
            sys.stderr.write('Error from server (AlreadyExists): %s "%s" already exists\n' % (kind, name))
            sys.exit(1)
        say("%s %s/%s" % (verb, kind, name))
        store(kind, name, doc["metadata"].get("labels", {}).get("app.kubernetes.io/part-of", ""), doc.get("stringData"), doc)
        print("%s/%s %s" % (kind, name, "created" if verb == "create" else "configured"))
    sys.exit(0)

if verb == "wait":
    target = positional()[0]
    say("wait for %s %s" % (target, [w for w in rest if w.startswith("--timeout=")][0]))
    kind, name = target.split("/")
    sys.exit(0 if os.path.exists(path(kind, name)) else 1)

if verb == "rollout" and rest[0] == "status":
    kind, name = rest[1].split("/")
    say("wait for %s/%s %s" % (kind_of(kind), name, [w for w in rest if w.startswith("--timeout=")][0]))
    sys.exit(0 if os.path.exists(path(kind, name)) else 1)

if verb == "patch":
    # Only a merge patch of the annotations of a pod template (guard.sh).
    words = positional()
    kind, name = kind_of(words[0]), words[1]
    body = json.loads(option("-p"))
    say("patch %s/%s %s %s" % (kind, name, option("--type"), json.dumps(body, sort_keys=True)))
    if not os.path.exists(path(kind, name)):
        sys.stderr.write('Error from server (NotFound): %s "%s" not found\n' % (kind, name))
        sys.exit(1)
    thing = load(kind, name)
    meta = thing["doc"]["spec"]["template"]["metadata"]
    meta.setdefault("annotations", {}).update(body["spec"]["template"]["metadata"]["annotations"])
    store(kind, name, thing["label"], thing["values"], thing["doc"])
    print("deployment.apps/%s patched" % name)
    sys.exit(0)

if verb == "delete":
    words = positional()
    kind, name = kind_of(words[0]), words[1]
    say("delete %s/%s" % (kind, name))
    if os.path.exists(path(kind, name)):
        os.remove(path(kind, name))
    sys.exit(0)

if verb == "exec":
    target, cname = positional()[0], option("-c")
    command = rest[rest.index("--") + 1:]
    say("exec %s -c %s: %s" % (target, cname, os.path.basename(command[0])))
    if command[0] == "sha256sum":
        # The model file of a KubeAI llama.cpp pod: only at the path STUB_MODEL_PATH (its cache, the
        # commit, the file), with the SHA-256 STUB_MODEL_SHA256.
        if command[1] != os.environ.get("STUB_MODEL_PATH"):
            sys.stderr.write("sha256sum: %s: No such file or directory\n" % command[1])
            sys.exit(1)
        print("%s  %s" % (os.environ.get("STUB_MODEL_SHA256", "0" * 64), command[1]))
        sys.exit(0)
    if command[0] == "ls":
        # The commits in the llama.cpp cache of the model pod.
        print(os.environ.get("STUB_MODEL_COMMITS", "0" * 40))
        sys.exit(0)
    print('{"status": "ok"}' if command[0] == "curl" else '200 {"ok": true}')
    sys.exit(0)

if verb == "logs":
    say("logs %s -c %s" % (positional()[0], option("-c")))
    print("fetch-model: the-model.gguf is there and its SHA-256 is right.")
    sys.exit(0)

if verb == "port-forward":
    target = positional()[0]
    local = int(positional()[1].split(":")[0])
    say("port-forward %s" % target)
    if target.startswith("service/"):
        # Tempo or Grafana: the stand-in curl answers for them. It waits until it is stopped.
        import time
        print("Forwarding from 127.0.0.1:%d -> %s" % (local, positional()[1].split(":")[1]), flush=True)
        time.sleep(600)
        sys.exit(0)
    token = next(t["values"]["WATTSON_TOKEN"] for k, _, t in objects() if k == "secret" and "WATTSON_TOKEN" in t["values"])
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class Orchestrator(BaseHTTPRequestHandler):
        def do_GET(self):
            good = self.headers.get("Authorization") == "Bearer " + token and self.path == "/v1/health"
            body = json.dumps({"status": "ok", "agents": {"triage": "up", "knowledge": "up"}} if good else {"error": "unauthorized"}).encode()
            self.send_response(200 if good else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    print("Forwarding from 127.0.0.1:%d -> 9000" % local, flush=True)
    HTTPServer(("127.0.0.1", local), Orchestrator).serve_forever()

say("A-CALL-THE-STAND-IN-DOES-NOT-KNOW: %s %s" % (verb, " ".join(rest)))
sys.exit(1)
STUB

cat >"$WORK/bin/docker" <<'STUB'
#!/usr/bin/env python3
# A stand-in for docker: it writes down what was to be built and pushed.
import os, sys
args = sys.argv[1:]
def option(name):
    return args[args.index(name) + 1] if name in args else "-"
with open(os.environ["STUB_LOG"], "a") as log:
    if args[0] == "build":
        log.write("docker build tag=%s platform=%s network=%s from=%s\n" % (option("-t"), option("--platform"), option("--network"), os.path.basename(args[-1])))
        print("sha256:stand-in")
    elif args[0] == "push":
        log.write("docker push %s\n" % args[-1])
    elif args[:3] == ["buildx", "imagetools", "inspect"]:
        log.write("docker digest %s\n" % args[-1])
        sys.stdout.write("sha256:" + "cd" * 32)
    else:
        log.write("A-DOCKER-CALL-THE-STAND-IN-DOES-NOT-KNOW: %s\n" % " ".join(args))
        sys.exit(1)
STUB

cat >"$WORK/bin/curl" <<'STUB'
#!/usr/bin/env python3
# A stand-in for curl. The registry answers 401 (it is there, and wants a login), or
# STUB_REGISTRY. A call to 127.0.0.1 (a port-forward) is Tempo's /ready, or Grafana's HTTP API:
# a made-up Grafana in $STUB_STATE/grafana.state that wants the admin login of the Secret
# monitoring-grafana, as an Authorization header from a --config on standard input. Each call to
# Grafana is written down in $STUB_STATE/grafana-calls.jsonl. No value of a Secret may be on
# the command line. STUB_GRAFANA_TEMPO=down: Grafana cannot reach Tempo. STUB_GRAFANA_DELETE=fails:
# every DELETE fails.
import base64, json, os, sys
args = sys.argv[1:]
STATE, LOG = os.environ["STUB_STATE"], os.environ["STUB_LOG"]
def say(line):
    with open(LOG, "a") as log:
        log.write(line + "\n")
url = args[-1]
if not url.startswith("http://127.0.0.1:"):
    say("curl %s" % url)
    sys.stdout.write(os.environ.get("STUB_REGISTRY", "401"))
    sys.exit(0)

def secret(name):
    path = os.path.join(STATE, "secret__%s.json" % name)
    return json.load(open(path))["values"] if os.path.exists(path) else {}
login = secret("monitoring-grafana")
secrets = [v for name in os.listdir(STATE) if name.startswith("secret__") for v in secret(name[8:-5]).values() if len(v) >= 16]
if login:
    secrets.append(base64.b64encode(("%s:%s" % (login.get("admin-user"), login.get("admin-password"))).encode()).decode())
if any(value in arg for value in secrets for arg in args):
    say("A-SECRET-ON-A-COMMAND-LINE")

options, i = {"headers": []}, 0
while i < len(args) - 1:
    word = args[i]
    if word in ("--config", "-K"):
        options["config"] = sys.stdin.read() if args[i + 1] == "-" else open(args[i + 1]).read()
        i += 2
    elif word in ("--output", "-o", "--write-out", "-w", "--request", "-X", "--data-binary", "--max-time", "-m"):
        options[word.lstrip("-")[:5]] = args[i + 1]
        i += 2
    elif word in ("--header", "-H"):
        options["headers"].append(args[i + 1])
        i += 2
    else:
        i += 1
method = options.get("reque") or options.get("X") or "GET"
path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
query = dict(pair.partition("=")[::2] for pair in path.partition("?")[2].split("&") if pair)
config_headers = [line.split("=", 1)[1].strip().strip('"') for line in options.get("config", "").splitlines() if line.strip().startswith("header")]
body_file = options.get("data-")
body = json.load(open(body_file[1:])) if body_file else None

def answer(status, data):
    text = json.dumps(data) if not isinstance(data, str) else data
    if options.get("outpu"):
        open(options["outpu"], "w").write(text)
    else:
        sys.stdout.write(text)
    if options.get("write") == "%{http_code}":
        sys.stdout.write(str(status))
    sys.exit(0)

if path == "/ready":
    say("curl GET /ready")
    answer(200, "ready\n")

say("grafana %s %s" % (method, path))
state_file = os.path.join(STATE, "grafana.state")
grafana = json.load(open(state_file)) if os.path.exists(state_file) else {"datasources": {}, "folders": {}, "dashboards": {}}
wanted = "Authorization: Basic " + base64.b64encode(("%s:%s" % (login.get("admin-user"), login.get("admin-password"))).encode()).decode()
logged_in = bool(login) and config_headers == [wanted] and not any(h.startswith("Authorization") for h in options["headers"])
with open(os.path.join(STATE, "grafana-calls.jsonl"), "a") as calls:
    calls.write(json.dumps({"method": method, "path": path, "body": body, "logged_in": logged_in}) + "\n")
if path == "/api/health":
    answer(200, {"database": "ok", "version": "12.3.0"})
if not logged_in:
    answer(401, {"message": "Invalid username or password"})
if method == "DELETE" and os.environ.get("STUB_GRAFANA_DELETE") == "fails":
    answer(500, {"message": "database is locked"})
parts = path.partition("?")[0].strip("/").split("/")
def save():
    json.dump(grafana, open(state_file, "w"))
if path == "/api/user" and method == "GET":
    answer(200, {"login": login["admin-user"]})
if parts[:3] == ["api", "datasources", "uid"] and len(parts) == 4:
    uid = parts[3]
    if method == "GET":
        answer(200, grafana["datasources"][uid]) if uid in grafana["datasources"] else answer(404, {"message": "Data source not found"})
    if method == "PUT":
        if uid not in grafana["datasources"]:
            answer(404, {"message": "Data source not found"})
        grafana["datasources"][uid] = body
        save()
        answer(200, {"message": "Datasource updated"})
    if method == "DELETE":
        if grafana["datasources"].pop(uid, None) is None:
            answer(404, {"message": "Data source not found"})
        save()
        answer(200, {"message": "Data source deleted"})
if parts[:3] == ["api", "datasources", "uid"] and parts[4:] == ["health"] and method == "GET":
    if parts[3] not in grafana["datasources"]:
        answer(404, {"message": "Data source not found"})
    if os.environ.get("STUB_GRAFANA_TEMPO") == "down":
        answer(400, {"status": "ERROR", "message": "failed to connect to Tempo: connection refused"})
    answer(200, {"status": "OK", "message": "Data source is working"})
if path == "/api/datasources" and method == "POST":
    if body["uid"] in grafana["datasources"]:
        answer(409, {"message": "data source with the same uid already exists"})
    grafana["datasources"][body["uid"]] = body
    save()
    answer(200, {"message": "Datasource added"})
if parts[:2] == ["api", "folders"] and len(parts) == 3 and method == "GET":
    answer(200, grafana["folders"][parts[2]]) if parts[2] in grafana["folders"] else answer(404, {"message": "folder not found"})
if parts[:2] == ["api", "folders"] and len(parts) == 3 and method == "DELETE":
    # Grafana deletes the dashboards of a folder with it.
    if grafana["folders"].pop(parts[2], None) is None:
        answer(404, {"message": "folder not found"})
    grafana["dashboards"] = {uid: d for uid, d in grafana["dashboards"].items() if d.get("folderUid") != parts[2]}
    save()
    answer(200, {"message": "Folder deleted"})
if parts == ["api", "search"] and method == "GET" and "folderUIDs" in query:
    answer(200, [{"uid": uid, "type": "dash-db"} for uid, d in grafana["dashboards"].items() if d.get("folderUid") == query["folderUIDs"]])
if path == "/api/folders" and method == "POST":
    if body["uid"] in grafana["folders"] or body["uid"] in grafana["dashboards"]:
        answer(409, {"message": "already exists"})
    grafana["folders"][body["uid"]] = body
    save()
    answer(200, body)
if path == "/api/dashboards/db" and method == "POST":
    uid = body["dashboard"]["uid"]
    if body.get("folderUid") not in grafana["folders"]:
        answer(400, {"message": "folder not found"})
    if uid in grafana["folders"] or (uid in grafana["dashboards"] and not body.get("overwrite")):
        answer(409, {"message": "already exists"})
    grafana["dashboards"][uid] = body
    save()
    answer(200, {"status": "success", "uid": uid})
if parts[:3] == ["api", "dashboards", "uid"] and len(parts) == 4 and method == "GET":
    answer(200, grafana["dashboards"][parts[3]]) if parts[3] in grafana["dashboards"] else answer(404, {"message": "Dashboard not found"})
if parts[:3] == ["api", "dashboards", "uid"] and len(parts) == 4 and method == "DELETE":
    if grafana["dashboards"].pop(parts[3], None) is None:
        answer(404, {"message": "Dashboard not found"})
    save()
    answer(200, {"message": "Dashboard deleted"})
say("A-GRAFANA-CALL-THE-STAND-IN-DOES-NOT-KNOW: %s %s" % (method, path))
answer(400, {"message": "unknown"})
STUB
cat >"$WORK/bin/helm" <<'STUB'
#!/usr/bin/env python3
# A stand-in for helm: "pull" leaves a small file as the chart, "upgrade --install" writes
# down what it was asked to install, and where.
import os, sys
import yaml
args = sys.argv[1:]
def option(name):
    return args[args.index(name) + 1] if name in args else "-"
with open(os.environ["STUB_LOG"], "a") as log:
    if args[0] == "pull":
        with open(os.path.join(option("--destination"), "%s-%s.tgz" % (args[1], option("--version"))), "w") as chart:
            chart.write("a stand-in chart\n")
        log.write("helm pull %s repo=%s version=%s\n" % (args[1], option("--repo"), option("--version")))
    elif args[:2] == ["upgrade", "--install"]:
        if option("--kube-context") != "test-context" or option("--namespace") != "test-kubeai":
            log.write("THE-WRONG-CLUSTER-OR-NAMESPACE: helm %s\n" % " ".join(args))
            sys.exit(1)
        values = yaml.safe_load(open(option("--values")))
        images = sorted(set(values["modelServers"]["LlamaCpp"]["images"].values()))
        if "OLlama" in values["modelServers"]:
            log.write("AN-OLLAMA-IMAGE-IN-THE-VALUES\n")
        log.write("helm upgrade --install %s chart=%s context=%s namespace=%s create-namespace=%s wait=%s images=%s\n" % (
            args[2], os.path.basename(args[3]), option("--kube-context"), option("--namespace"),
            "--create-namespace" in args, "--wait" in args, ",".join(images)))
    else:
        log.write("A-HELM-CALL-THE-STAND-IN-DOES-NOT-KNOW: %s\n" % " ".join(args))
        sys.exit(1)
STUB
chmod +x "$WORK/bin/kubectl" "$WORK/bin/docker" "$WORK/bin/curl" "$WORK/bin/helm"

# A copy of the repository as a fresh git repo with one commit: deploy.sh tags the image with
# the commit and refuses changes that are not committed.
REPO="$WORK/repo"
mkdir -p "$REPO"
cp -r Dockerfile Dockerfile.shield .dockerignore .gitignore pyproject.toml uv.lock settings.env k8s src "$REPO/"
find "$REPO" -name __pycache__ -prune -exec rm -rf {} +
git -C "$REPO" init -q
git -C "$REPO" add -A
git -C "$REPO" -c user.name=test -c user.email=test@example.invalid -c commit.gpgsign=false commit -q -m "the test copy"
COMMIT="$(git -C "$REPO" rev-parse --short=12 HEAD)"

# The settings of the runs: another context, namespace, registry and namespace of Grafana, and
# the model in the triage pod (the KubeAI runs come later). Outside the copy, so the copy stays
# committed. Telemetry is on, as in settings.env.
FLOW_SETTINGS="$WORK/flow-settings.env"
sed -e 's/^K8S_CONTEXT=.*/K8S_CONTEXT=test-context/' -e 's/^K8S_NAMESPACE=.*/K8S_NAMESPACE=test-ns/' \
  -e 's#^REGISTRY=.*#REGISTRY=registry.example.test/p#' -e 's/^MODEL_BACKEND=.*/MODEL_BACKEND=sidecar/' \
  -e 's/^GRAFANA_NAMESPACE=.*/GRAFANA_NAMESPACE=test-grafana/' -e 's/^GUARD_MODE=.*/GUARD_MODE=monitor/' \
  settings.env >"$FLOW_SETTINGS"
# Grafana, in its own namespace: its Deployment, its Service, and the Secret with its admin login.
GRAFANA_THERE=(deployment/monitoring-grafana service/monitoring-grafana "secret/monitoring-grafana:admin-user,admin-password")
mkdir -p "$WORK/docker-in" "$WORK/docker-out"
printf '{"auths": {"registry.example.test": {}}}\n' >"$WORK/docker-in/config.json"
printf '{"auths": {"other.example.test": {}}}\n' >"$WORK/docker-out/config.json"

# namespace_with THING...  -> a new made-up namespace with these objects, and a new folder for
# the CLI settings. A THING is kind/name, kind/name=label (app.kubernetes.io/part-of), or
# secret/name:KEY,KEY (the keys of a Secret, with made-up values).
namespace_with() {
  STUB_STATE="$(mktemp -d "$WORK/namespace-XXXXXX")"
  STUB_LOG="$STUB_STATE.log"
  CLI_DIR="$STUB_STATE.cli"
  : >"$STUB_LOG"
  local thing
  for thing in namespace/test-ns storageclass/my-storage-class secret/my-pull-secret:.dockerconfigjson "$@"; do
    python3 -c '
import json, sys
state, thing = sys.argv[1:]
thing, _, keys = thing.partition(":")
thing, _, label = thing.partition("=")
kind, name = thing.split("/")
values = {key: "a-value-of-the-stand-in-%s" % key for key in keys.split(",") if key}
json.dump({"label": label, "values": values, "doc": None}, open("%s/%s__%s.json" % (state, kind, name), "w"))' "$STUB_STATE" "$thing"
  done
}
# run SCRIPT [ARGS...]  (env: NAME=value before)  -> runs a script of the copy on the made-up
# namespace. The exit code is in RUN_EXIT, what it printed in $STUB_STATE.out.
run() {
  local script="$1"
  shift
  # HOME is a temp folder too: deploy.sh copies the CLI folders of the old name from it.
  mkdir -p "$WORK/home"
  # Not BUILD_NETWORK of the shell that runs these tests: the checks name the build options.
  env -u BUILD_NETWORK PATH="$WORK/bin:$PATH" STUB_STATE="$STUB_STATE" STUB_LOG="$STUB_LOG" SETTINGS_FILE="$FLOW_SETTINGS" \
    HOME="$WORK/home" WATTSON_CONFIG_DIR="$CLI_DIR" DOCKER_CONFIG="${RUN_DOCKER_CONFIG:-$WORK/docker-in}" APPLY_RETRY_SECONDS=0 DELETE_RETRY_SECONDS=0 \
    ${RUN_ENV[@]+"${RUN_ENV[@]}"} bash "$REPO/k8s/$script" "$@" <"${RUN_INPUT:-/dev/null}" >"$STUB_STATE.out" 2>&1
  RUN_EXIT=$?
}
RUN_ENV=()
said() { grep -qF -- "$1" "$STUB_STATE.out"; }
called() { grep -qxF -- "$1" "$STUB_LOG"; }
calls_like() { grep -cE -- "$1" "$STUB_LOG"; }
# The made-up values of a Secret (by default the one deploy.sh made), one per line.
secret_values() {  # [SECRET]
  python3 -c '
import json, sys
thing = json.load(open(sys.argv[1]))
print("\n".join(thing["values"].values()))' "$STUB_STATE/secret__${1:-wattson}.json"
}
# Nothing went to another cluster, no value of a Secret (the tokens of wattson, the login of
# Grafana) was on a command line or on the screen, and the stand-ins knew every call.
clean_run() {
  ! grep -Eq 'WRONG-CLUSTER|A-SECRET-ON|DOES-NOT-KNOW|ANOTHER-NAMESPACE|A-CLUSTER-OBJECT-WITH' "$STUB_LOG" || return 1
  local value secret
  for secret in wattson wattson-shield monitoring-grafana wattson-cognee; do
    [[ -f "$STUB_STATE/secret__$secret.json" ]] || continue
    while IFS= read -r value; do
      [[ -n "$value" ]] || continue
      grep -qF -- "$value" "$STUB_STATE.out" && return 1
      grep -qF -- "$value" "$STUB_LOG" && return 1
    done < <(secret_values "$secret")
  done
  return 0
}
show_output_on_failure() {
  if [[ "$FAILED" -gt "$1" ]]; then
    sed 's/^/         | /' "$STUB_STATE.out" | tail -n 25
    sed 's/^/         log| /' "$STUB_LOG" | tail -n 25
  fi
}

# ---- deploy.sh on an empty namespace ------------------------------------------------
section "deploy.sh: the first deploy, on a namespace without wattson"
before=$FAILED
namespace_with "${GRAFANA_THERE[@]}"
run deploy.sh
check "it ends well" test "$RUN_EXIT" -eq 0
check "it builds for linux/amd64, tagged with the commit" called "docker build tag=registry.example.test/p/wattson:$COMMIT platform=linux/amd64 network=- from=repo"
check "it pushes that image and reads its digest" bash -c 'grep -qxF "docker push $1" "$2" && grep -qxF "docker digest $1" "$2"' _ "registry.example.test/p/wattson:$COMMIT" "$STUB_LOG"
check "it checks that the registry answers" called "curl https://registry.example.test/v2/"
SHIELD_TAG_OF_REPO="$(cd "$REPO" && sha256sum Dockerfile.shield .dockerignore pyproject.toml uv.lock src/wattson/shield/server.py | sha256sum | cut -c1-12)"
check "it builds the shield's own image (Dockerfile.shield), tagged with the hash of its files, pushes it and reads its digest" bash -c '
  grep -qxF "docker build tag=registry.example.test/p/wattson-shield:$1 platform=linux/amd64 network=- from=repo" "$2" &&
  grep -qxF "docker push registry.example.test/p/wattson-shield:$1" "$2" && grep -qxF "docker digest registry.example.test/p/wattson-shield:$1" "$2"' _ "$SHIELD_TAG_OF_REPO" "$STUB_LOG"
check "it makes the Secret once, with the three tokens" bash -c '[[ "$(grep -c "^create secret/wattson$" "$1")" -eq 1 ]]' _ "$STUB_LOG"
check "it makes the Secret wattson-shield once, with one token of its own (43 signs); the Secret wattson has the three keys only" python3 -c '
import json, re, sys
state, log = sys.argv[1:]
assert open(log).read().count("create secret/wattson-shield\n") == 1
shield = json.load(open(state + "/secret__wattson-shield.json"))
main = json.load(open(state + "/secret__wattson.json"))
assert list(shield["values"]) == ["SHIELD_TOKEN"] and re.fullmatch(r"[A-Za-z0-9_-]{43}", shield["values"]["SHIELD_TOKEN"]), list(shield["values"])
assert shield["label"] == "wattson" and sorted(main["values"]) == ["KNOWLEDGE_TOKEN", "TRIAGE_TOKEN", "WATTSON_TOKEN"]
assert shield["values"]["SHIELD_TOKEN"] not in main["values"].values()' "$STUB_STATE" "$STUB_LOG"
check "each token is 32 random bytes, base64url (43 signs), and all three differ" bash -c '
  values="$(python3 -c "import json,sys; print(\"\n\".join(json.load(open(sys.argv[1]))[\"values\"][k] for k in (\"WATTSON_TOKEN\",\"TRIAGE_TOKEN\",\"KNOWLEDGE_TOKEN\")))" "$1")"
  [[ "$(grep -cE "^[A-Za-z0-9_-]{43}$" <<<"$values")" -eq 3 && "$(sort -u <<<"$values" | grep -c .)" -eq 3 ]]' _ "$STUB_STATE/secret__wattson.json"
for object in serviceaccount/wattson-orchestrator serviceaccount/wattson-triage serviceaccount/wattson-knowledge \
  persistentvolumeclaim/wattson-data persistentvolumeclaim/wattson-knowledge-data persistentvolumeclaim/wattson-models \
  deployment/wattson-orchestrator deployment/wattson-triage deployment/wattson-knowledge service/wattson-triage service/wattson-knowledge \
  networkpolicy/wattson-orchestrator networkpolicy/wattson-triage networkpolicy/wattson-knowledge \
  serviceaccount/wattson-tempo persistentvolumeclaim/wattson-tempo-data configmap/wattson-tempo deployment/wattson-tempo \
  service/wattson-tempo networkpolicy/wattson-tempo configmap/wattson-guard \
  serviceaccount/wattson-shield deployment/wattson-shield service/wattson-shield networkpolicy/wattson-shield; do
  check "it applies $object" called "apply $object"
done
check "the applied shield runs its own pushed image, pinned by its digest; the orchestrator gets its address" python3 -c '
import json, sys
state, image = sys.argv[1:]
spec = json.load(open(state + "/deployment__wattson-shield.json"))["doc"]["spec"]["template"]["spec"]
assert [c["image"] for c in spec["containers"]] == [image], spec["containers"]
o = json.load(open(state + "/deployment__wattson-orchestrator.json"))["doc"]["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in o["env"]}
assert env["INJECTION_MODE"] == "cut" and env["SHIELD_URL"] == "http://wattson-shield:9003", env' "$STUB_STATE" "registry.example.test/p/wattson-shield:$SHIELD_TAG_OF_REPO@sha256:$(printf 'cd%.0s' {1..32})"
check "the ConfigMap wattson-guard holds the two files of the rules as they are, and the orchestrator mounts it" python3 -c '
import json, os, sys
state, guard = sys.argv[1:]
data = json.load(open(state + "/configmap__wattson-guard.json"))["doc"]["data"]
assert data == {n: open(os.path.join(guard, n), newline="").read() for n in ("rules.yaml", "policies.cedar")}, sorted(data)
spec = json.load(open(state + "/deployment__wattson-orchestrator.json"))["doc"]["spec"]["template"]["spec"]
assert {"name": "guard", "configMap": {"name": "wattson-guard"}} in spec["volumes"], spec["volumes"]' "$STUB_STATE" "$GUARD_DIR"
check "it says how the Guard runs: the mode, the rules only (the sidecar), the rules it counted" said "Guard: monitor; fixed rules only (no model the orchestrator can reach). Rules: 2 rules, 1 on (risky 1)"
check "it touches nothing in the namespace of Grafana" not grep -qE ' in test-grafana$' "$STUB_LOG"
check "it applies no Service and no Ingress for the orchestrator" not grep -Eq '^apply (service/wattson-orchestrator|ingress/)' "$STUB_LOG"
check "the applied Deployments run the pushed image, pinned by its digest" python3 -c '
import json, sys
for name in ("orchestrator", "triage", "knowledge"):
    doc = json.load(open("%s/deployment__wattson-%s.json" % (sys.argv[1], name)))["doc"]
    images = [c["image"] for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] != "llama"]
    assert images == [sys.argv[2]], images' "$STUB_STATE" "registry.example.test/p/wattson:$COMMIT@sha256:$(printf 'cd%.0s' {1..32})"
check "it waits for the three and Tempo, and 15 minutes for triage the first time (the model download)" bash -c '
  grep -qxF "wait for deployment/wattson-orchestrator --timeout=300s" "$1" && grep -qxF "wait for deployment/wattson-knowledge --timeout=300s" "$1" &&
  grep -qxF "wait for deployment/wattson-triage --timeout=900s" "$1" && grep -qxF "wait for deployment/wattson-tempo --timeout=300s" "$1" &&
  grep -qxF "wait for deployment/wattson-shield --timeout=300s" "$1"' _ "$STUB_LOG"
check "the applied agents send their traces to Tempo, each with its own name" python3 -c '
import json, sys
for name in ("orchestrator", "triage", "knowledge"):
    doc = json.load(open("%s/deployment__wattson-%s.json" % (sys.argv[1], name)))["doc"]
    c = next(c for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == name)
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://wattson-tempo:4318" and env["OTEL_SERVICE_NAME"] == "wattson-" + name, env' "$STUB_STATE"
check "it writes the CLI's config.json" python3 -c '
import json, sys
assert json.load(open(sys.argv[1] + "/config.json")) == {"k8s_context": "test-context", "namespace": "test-ns", "deployment": "wattson-orchestrator", "remote_port": 9000}' "$CLI_DIR"
check "it writes the CLI's token, mode 600, the WATTSON_TOKEN of the Secret" bash -c '
  [[ "$(stat -c %a "$1/token")" == 600 && "$(cat "$1/token")" == "$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[\"values\"][\"WATTSON_TOKEN\"])" "$2")" ]]' _ "$CLI_DIR" "$STUB_STATE/secret__wattson.json"
check "the CLI folder is mode 700" test "$(stat -c %a "$CLI_DIR")" = 700
check "no temporary token file is left" bash -c '! ls -A "$1" | grep -q "^\.token-"' _ "$CLI_DIR"
check "it never makes a namespace" not grep -Eq '(apply|create) namespace/' "$STUB_LOG"
check "every call went to the context and namespace of the settings; no secret on a command line or on screen" clean_run
check "it says what to do next (wattson k8s add, grimoire connect, status, grafana.sh)" bash -c 'grep -q "wattson k8s add test-context" "$1" && grep -q "wattson grimoire connect" "$1" && grep -q "wattson status" "$1" && grep -q "bash k8s/grafana.sh --apply" "$1"' _ "$STUB_STATE.out"
show_output_on_failure "$before"
FIRST_STATE="$STUB_STATE" FIRST_LOG="$STUB_LOG" FIRST_CLI="$CLI_DIR"

section "deploy.sh: a second deploy on the same namespace"
before=$FAILED
token_before="$(cat "$CLI_DIR/token")"
python3 -c '
import json, sys
path = sys.argv[1] + "/config.json"
data = json.load(open(path)); data["kept_by_the_cli"] = True
json.dump(data, open(path, "w"))' "$CLI_DIR"
: >"$STUB_LOG"
rm -f "$STUB_STATE/apply-count"
run deploy.sh
check "it ends well" test "$RUN_EXIT" -eq 0
check "the Secret is kept, not made again" bash -c '! grep -q "^create secret" "$1" && grep -q "Secret: wattson is already there, kept." "$2"' _ "$STUB_LOG" "$STUB_STATE.out"
check "the Secret wattson-shield is kept too" said "Secret: wattson-shield is already there, kept."
check "the token file is the same" test "$(cat "$CLI_DIR/token")" = "$token_before"
check "other keys of config.json are kept" python3 -c 'import json,sys; assert json.load(open(sys.argv[1] + "/config.json"))["kept_by_the_cli"] is True' "$CLI_DIR"
check "it waits 5 minutes for triage now (the model is there)" called "wait for deployment/wattson-triage --timeout=300s"
check "clean: context, namespace, no secret shown" clean_run
show_output_on_failure "$before"

section "deploy.sh: the CLI settings of the old name (~/.config/myagent) are copied first"
before=$FAILED
namespace_with "${GRAFANA_THERE[@]}"
OLD_HOME="$WORK/old-home"
mkdir -p "$OLD_HOME/.config/myagent/kube"
printf '{"k8s_context": "test-context", "namespace": "test-ns", "deployment": "myagent-orchestrator", "remote_port": 9000, "kept_by_the_cli": true}\n' >"$OLD_HOME/.config/myagent/config.json"
printf 'the-token-of-the-old-install-0123456789abcdef\n' >"$OLD_HOME/.config/myagent/token"
printf '{"current-context": "other-cluster"}\n' >"$OLD_HOME/.config/myagent/kube/other-cluster.yaml"
chmod 600 "$OLD_HOME/.config/myagent/config.json" "$OLD_HOME/.config/myagent/token" "$OLD_HOME/.config/myagent/kube/other-cluster.yaml"
old_listing="$(cd "$OLD_HOME/.config/myagent" && find . -type f -exec sha256sum {} + | sort)"
# The default folder (no WATTSON_CONFIG_DIR) in a made-up home.
RUN_ENV=(WATTSON_CONFIG_DIR= "HOME=$OLD_HOME")
run deploy.sh
RUN_ENV=()
NEW_CLI="$OLD_HOME/.config/wattson"
check "it ends well, and says in one line that it copied the old settings" bash -c '
  [[ $1 -eq 0 ]] && [[ "$(grep -c "^Copied your settings of the old name myagent: ~/.config/myagent to ~/.config/wattson. The old folders are kept.$" "$2")" -eq 1 ]]' _ "$RUN_EXIT" "$STUB_STATE.out"
check "the read-only kubeconfig of the old folder is in the new one: file 600, folders 700" bash -c '
  [[ "$(cat "$1/kube/other-cluster.yaml")" == "{\"current-context\": \"other-cluster\"}" ]] &&
  [[ "$(stat -c %a "$1/kube/other-cluster.yaml") $(stat -c %a "$1/kube") $(stat -c %a "$1")" == "600 700 700" ]]' _ "$NEW_CLI"
check "config.json: the new deployment, the other keys of the old one kept" python3 -c '
import json, sys
data = json.load(open(sys.argv[1] + "/config.json"))
assert data["deployment"] == "wattson-orchestrator" and data["kept_by_the_cli"] is True, data' "$NEW_CLI"
check "the token is the new one (WATTSON_TOKEN of the new Secret), mode 600" bash -c '
  [[ "$(stat -c %a "$1/token")" == 600 && "$(cat "$1/token")" == "$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[\"values\"][\"WATTSON_TOKEN\"])" "$2")" ]]' _ "$NEW_CLI" "$STUB_STATE/secret__wattson.json"
check "the old folder is not changed" test "$(cd "$OLD_HOME/.config/myagent" && find . -type f -exec sha256sum {} + | sort)" = "$old_listing"
check "clean: context, namespace, no secret shown" clean_run
show_output_on_failure "$before"

section "deploy.sh: --dry-run"
before=$FAILED
namespace_with
run deploy.sh --dry-run
check "it ends well" test "$RUN_EXIT" -eq 0
check "it calls neither kubectl nor docker nor curl" test ! -s "$STUB_LOG"
check "it makes no CLI settings" test ! -e "$CLI_DIR"
check "it prints the 25 objects (15 of the agents with the rules of the Guard, 6 of Tempo, 4 of the shield), none of them a Secret" python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
assert len(docs) == 25 and not any(d["kind"] == "Secret" for d in docs), [d["kind"] for d in docs]' "$STUB_STATE.out"
check "it says that the digests are stand-ins" said "The image digests below are stand-ins"
check "the shield's image has the hash of its files as its tag (not the commit: a deploy that does not change them leaves the shield alone)" \
  grep -q "image: registry.example.test/p/wattson-shield:$SHIELD_TAG_OF_REPO@sha256:0000" "$STUB_STATE.out"
check "the image has the commit as its tag" grep -q "image: registry.example.test/p/wattson:$COMMIT@sha256:0000" "$STUB_STATE.out"
check "it prints nothing that looks like a token" not grep -Eq '(TOKEN|token)["]?: *[A-Za-z0-9_-]{32,}' "$STUB_STATE.out"
show_output_on_failure "$before"

section "deploy.sh: changes that are not committed"
before=$FAILED
printf '# a change\n' >>"$REPO/src/wattson/__init__.py"
namespace_with
run deploy.sh
check "they are refused" test "$RUN_EXIT" -ne 0
check "it names the file" said "src/wattson/__init__.py"
check "nothing was built or applied" not grep -Eq '^(docker build|apply|create)' "$STUB_LOG"
namespace_with
run deploy.sh --allow-dirty
check "with --allow-dirty it deploys" test "$RUN_EXIT" -eq 0
check "with --allow-dirty the tag says dirty" grep -qE "^docker build tag=registry.example.test/p/wattson:$COMMIT-dirty-[0-9a-f]{8} " "$STUB_LOG"
check "a change of the agents' code leaves the shield's tag as it is" grep -qF "docker build tag=registry.example.test/p/wattson-shield:$SHIELD_TAG_OF_REPO " "$STUB_LOG"
git -C "$REPO" checkout -q -- src/wattson/__init__.py
show_output_on_failure "$before"

section "deploy.sh: what it refuses before changing anything"
nothing_changed() { ! grep -Eq '^(docker build|docker push|apply|create|delete)' "$STUB_LOG"; }

before=$FAILED
namespace_with "${GRAFANA_THERE[@]}"
RUN_ENV=("GUARD_DIR=$(guard_variant deploy-bad "rules: [{name: x, label: risky, regex: ['(unclosed'], reason: Risky.}]")")
run deploy.sh
RUN_ENV=()
check "wrong rules of the Guard: refused before any call (no kubectl, no docker), with what is wrong" bash -c '
  [[ $1 -ne 0 ]] && test ! -s "$2" && grep -q "PROBLEM: rule 1 (x): regex 1 is not valid" "$3" && grep -q "Nothing was changed" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
RUN_ENV=("GUARD_DIR=$WORK/guard-nowhere")
run deploy.sh
RUN_ENV=()
check "no rules of the Guard: refused before any call" bash -c '[[ $1 -ne 0 ]] && test ! -s "$2" && grep -q "rules of the Guard are missing" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
show_output_on_failure "$before"

before=$FAILED
STUB_STATE="$(mktemp -d "$WORK/namespace-XXXXXX")" STUB_LOG="$STUB_STATE.log" CLI_DIR="$STUB_STATE.cli"
: >"$STUB_LOG"
run deploy.sh
check "no namespace: refused" test "$RUN_EXIT" -ne 0
check "no namespace: it says it never makes one" said "never makes a namespace"
check "no namespace: nothing was made" nothing_changed
show_output_on_failure "$before"

namespace_with deployment/wattson-triage=someone-else
run deploy.sh
check "an object with our name that is not ours: refused, and named" bash -c '[[ $1 -ne 0 ]] && grep -q "deployment/wattson-triage" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "an object that is not ours: nothing was made" nothing_changed

namespace_with service/wattson-orchestrator=wattson
run deploy.sh
check "a Service for the orchestrator: refused" bash -c '[[ $1 -ne 0 ]] && grep -q "must have none" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "a Service for the orchestrator: nothing was made" nothing_changed

namespace_with "secret/wattson=wattson:WATTSON_TOKEN,TRIAGE_TOKEN"
run deploy.sh
check "a Secret without KNOWLEDGE_TOKEN: refused, and never changed" bash -c '[[ $1 -ne 0 ]] && grep -q "has no KNOWLEDGE_TOKEN" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "a Secret without a key: nothing was made" nothing_changed

namespace_with "secret/wattson-shield=wattson:ANOTHER_KEY"
run deploy.sh
check "a Secret wattson-shield without SHIELD_TOKEN: refused, and never changed" bash -c '[[ $1 -ne 0 ]] && grep -q "The Secret wattson-shield has no SHIELD_TOKEN" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "a Secret wattson-shield without its key: nothing was made" nothing_changed

namespace_with
RUN_DOCKER_CONFIG="$WORK/docker-out" run deploy.sh
check "not logged in to the registry: refused, with the docker login command" bash -c '[[ $1 -ne 0 ]] && grep -q "docker login registry.example.test" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "not logged in: nothing was built" nothing_changed

namespace_with
RUN_ENV=(STUB_REGISTRY=000)
run deploy.sh
RUN_ENV=()
check "the registry does not answer: refused" bash -c '[[ $1 -ne 0 ]] && grep -q "does not answer" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "the registry does not answer: nothing was built" nothing_changed

namespace_with
RUN_ENV=(STUB_BROKEN=deployment/wattson-knowledge)
run deploy.sh
RUN_ENV=()
check "the cluster cannot be asked about one object: refused" test "$RUN_EXIT" -ne 0
check "the cluster cannot be asked: nothing was made" nothing_changed

sed 's/^K8S_CONTEXT=.*/K8S_CONTEXT=another-context/' "$FLOW_SETTINGS" >"$WORK/other-context.env"
namespace_with
FLOW_SAVED="$FLOW_SETTINGS" FLOW_SETTINGS="$WORK/other-context.env"
run deploy.sh
FLOW_SETTINGS="$FLOW_SAVED"
check "a context that kubectl does not have: refused" bash -c '[[ $1 -ne 0 ]] && grep -q "has no context" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "a context that kubectl does not have: nothing was made" nothing_changed

section "deploy.sh: etcd answers too late"
before=$FAILED
namespace_with
RUN_ENV=(STUB_APPLY_FAILS=2)
run deploy.sh
RUN_ENV=()
check "two timeouts: the third try works" bash -c '[[ $1 -eq 0 && "$(grep -c "^apply try" "$2")" -eq 3 ]]' _ "$RUN_EXIT" "$STUB_LOG"
check "two timeouts: it says it tries again" said "Trying again"
namespace_with
RUN_ENV=(STUB_APPLY_FAILS=3)
run deploy.sh
RUN_ENV=()
check "three timeouts: it stops after three tries" bash -c '[[ $1 -ne 0 && "$(grep -c "^apply try" "$2")" -eq 3 ]]' _ "$RUN_EXIT" "$STUB_LOG"
check "three timeouts: no pod is waited for" not grep -q '^wait for' "$STUB_LOG"
namespace_with
RUN_ENV=(STUB_APPLY_ERROR=1)
run deploy.sh
RUN_ENV=()
check "an error that is no timeout: it stops at once" bash -c '[[ $1 -ne 0 && "$(grep -c "^apply try" "$2")" -eq 1 ]]' _ "$RUN_EXIT" "$STUB_LOG"
show_output_on_failure "$before"

# ---- grafana.sh -------------------------------------------------------------------
# The Grafana is made up by the stand-in curl ($STUB_STATE/grafana.state, every call in
# grafana-calls.jsonl); its admin login is the Secret monitoring-grafana of the stand-in kubectl.
stopped_soon() {  # PATTERN -> true when no process matches it within 3 seconds
  local _
  for _ in 1 2 3 4 5 6; do
    pgrep -f "$1" >/dev/null || return 0
    sleep 0.5
  done
  return 1
}
grafana_calls() {  # -> the calls to the made-up Grafana, one JSON per line
  cat "$STUB_STATE/grafana-calls.jsonl" 2>/dev/null || true
}
printf 'no\n' >"$WORK/answer-no-context"
printf 'test-context\n' >"$WORK/answer-context"

section "grafana.sh, on the namespace of the first deploy"
before=$FAILED
STUB_STATE="$FIRST_STATE" STUB_LOG="$FIRST_LOG" CLI_DIR="$FIRST_CLI"
: >"$STUB_LOG"
run grafana.sh
check "without --apply: it ends well and says that nothing was run" bash -c '[[ $1 -eq 0 ]] && grep -q "Nothing was run" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "without --apply: no kubectl and no curl call at all" test ! -s "$STUB_LOG"
check "without --apply: it prints the data source (Tempo, by its address in the cluster) and the panels" bash -c '
  grep -qF "\"url\": \"http://wattson-tempo.test-ns.svc.cluster.local:3200\"" "$1" && grep -qF "\"type\": \"tempo\"" "$1" &&
  grep -q "^#   Tasks per 15 minutes, by outcome$" "$1" && grep -q "^#   Recent tasks$" "$1"' _ "$STUB_STATE.out"
check "without --apply: it says that Grafana keeps its data in an emptyDir: run it again after a restart" said "emptyDir: after it restarts, run this again"
RUN_INPUT="$WORK/answer-no-context" run grafana.sh --apply
check "--apply without the typed context: nothing changed, no login read, no call to Grafana" bash -c '
  [[ $1 -ne 0 ]] && grep -q "Nothing was changed" "$2" && ! grep -qE "^(grafana |port-forward|get secret)" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
check "--apply: before the question it only reads names (the context, the two Services, the Deployment of Grafana)" bash -c '
  ! grep -vE "^(config view --context test-context|get service/wattson-tempo|get service/monitoring-grafana in test-grafana|get deployment/monitoring-grafana in test-grafana)$" "$1" | grep -q .' _ "$STUB_LOG"
: >"$STUB_LOG"
RUN_INPUT="$WORK/answer-context" run grafana.sh --apply
check "--apply with the typed context: it ends well" test "$RUN_EXIT" -eq 0
check "it reaches Grafana through a port-forward to its Service, in its namespace" called "port-forward service/monitoring-grafana in test-grafana"
check "it reads the admin login from the Secret that the Deployment of Grafana names" called "get secret/monitoring-grafana in test-grafana"
check "every call to Grafana carried the admin login, given to curl on its standard input" bash -c '
  calls="$(cat "$1/grafana-calls.jsonl")"; [[ -n "$calls" ]] && ! grep -q "\"logged_in\": false" <<<"$calls"' _ "$STUB_STATE"
check "the first time it makes the data source: type tempo, uid wattson-tempo, Tempo's address in the cluster" python3 -c '
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
posts = [c["body"] for c in calls if c["method"] == "POST" and c["path"] == "/api/datasources"]
assert len(posts) == 1 and not any(c["method"] == "PUT" for c in calls), calls
body = posts[0]
assert (body["uid"], body["type"], body["access"], body["url"]) == ("wattson-tempo", "tempo", "proxy", "http://wattson-tempo.test-ns.svc.cluster.local:3200"), body' "$STUB_STATE/grafana-calls.jsonl"
check "it makes the folder wattson and saves the dashboard of k8s/grafana/wattson-dashboard.json in it, overwrite on" python3 -c '
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
folders = [c["body"] for c in calls if c["method"] == "POST" and c["path"] == "/api/folders"]
dashboards = [c["body"] for c in calls if c["method"] == "POST" and c["path"] == "/api/dashboards/db"]
expected = json.load(open(sys.argv[2]))
expected["id"] = None
assert folders == [{"uid": "wattson-folder", "title": "wattson"}], folders
assert len(dashboards) == 1 and dashboards[0]["dashboard"] == expected, "the dashboard differs from the file"
assert dashboards[0]["folderUid"] == "wattson-folder" and dashboards[0]["overwrite"] is True, dashboards[0]' "$STUB_STATE/grafana-calls.jsonl" k8s/grafana/wattson-dashboard.json
check "it asks Grafana whether it reaches Tempo, and says so" bash -c '
  grep -q "\"path\": \"/api/datasources/uid/wattson-tempo/health\"" "$1/grafana-calls.jsonl" && grep -q "Check: Grafana reaches Tempo (Data source is working)" "$2"' _ "$STUB_STATE" "$STUB_STATE.out"
check "it says where to open the dashboard: the Ingress of Grafana, or a port-forward" bash -c '
  grep -q "https://grafana.example.test/d/wattson$" "$1" && grep -q "port-forward service/monitoring-grafana 3000:80" "$1" && grep -q "run this again" "$1"' _ "$STUB_STATE.out"
check "it changes nothing in Kubernetes" not grep -Eq '^(apply|create|delete)' "$STUB_LOG"
check "nothing of the old name in Grafana: it says so, and deletes nothing" bash -c '
  grep -q "Old name: nothing of it is left in Grafana." "$1" && ! grep -q "\"method\": \"DELETE\"" "$2"' _ "$STUB_STATE.out" "$STUB_STATE/grafana-calls.jsonl"
check "clean: context, namespaces, no token and no Grafana login on a command line or on screen" clean_run
check "the port-forward to Grafana is stopped at the end" stopped_soon "$WORK/bin/kubectl.*port-forward"
show_output_on_failure "$before"

before=$FAILED
cp "$STUB_STATE/grafana.state" "$WORK/grafana-after-first.json"
: >"$STUB_LOG"
rm -f "$STUB_STATE/grafana-calls.jsonl"
RUN_INPUT="$WORK/answer-context" run grafana.sh --apply
check "a second run: it ends well, updates the data source (PUT), keeps the folder, overwrites the dashboard" python3 -c '
import json, sys
calls = [(c["method"], c["path"]) for c in map(json.loads, open(sys.argv[1]))]
assert ("PUT", "/api/datasources/uid/wattson-tempo") in calls and ("POST", "/api/datasources") not in calls, calls
assert ("POST", "/api/folders") not in calls and calls.count(("POST", "/api/dashboards/db")) == 1, calls
grafana = json.load(open(sys.argv[2]))
assert [len(grafana[k]) for k in ("datasources", "folders", "dashboards")] == [1, 1, 1], grafana.keys()
assert int(sys.argv[3]) == 0' "$STUB_STATE/grafana-calls.jsonl" "$STUB_STATE/grafana.state" "$RUN_EXIT"
check "a second run: the same data source and dashboard as the first (nothing piles up)" python3 -c '
import json, sys
assert json.load(open(sys.argv[1])) == json.load(open(sys.argv[2]))' "$STUB_STATE/grafana.state" "$WORK/grafana-after-first.json"
check "a second run: clean" clean_run
show_output_on_failure "$before"

section "grafana.sh: what was added under the old name (myagent) is removed"
before=$FAILED
namespace_with service/wattson-tempo=wattson "${GRAFANA_THERE[@]}"
# The Grafana of the old install: its data source, its folder with its dashboard, and a data
# source that is not ours.
python3 -c '
import json, sys
json.dump({
    "datasources": {"myagent-tempo": {"uid": "myagent-tempo", "name": "myagent Tempo", "type": "tempo"},
                    "other": {"uid": "other", "name": "Other", "type": "loki"}},
    "folders": {"myagent-folder": {"uid": "myagent-folder", "title": "myagent"}},
    "dashboards": {"myagent": {"dashboard": {"uid": "myagent"}, "folderUid": "myagent-folder", "overwrite": True}},
}, open(sys.argv[1], "w"))' "$STUB_STATE/grafana.state"
RUN_INPUT="$WORK/answer-context" run grafana.sh --apply
check "it ends well, and says what it removed" bash -c '
  [[ $1 -eq 0 ]] && grep -q "Old name: the dashboard myagent removed." "$2" && grep -q "Old name: the data source myagent-tempo removed." "$2" &&
  grep -q "Old name: the folder myagent-folder (it was empty) removed." "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "only after the new dashboard is saved: the old dashboard, the old data source, then the old folder after a look inside it" python3 -c '
import json, sys
calls = [(c["method"], c["path"]) for c in map(json.loads, open(sys.argv[1]))]
saved = calls.index(("POST", "/api/dashboards/db"))
steps = [("DELETE", "/api/dashboards/uid/myagent"), ("DELETE", "/api/datasources/uid/myagent-tempo"),
         ("GET", "/api/search?folderUIDs=myagent-folder"), ("DELETE", "/api/folders/myagent-folder")]
where = [calls.index(step) for step in steps]
assert saved < where[0] < where[1] < where[2] < where[3], calls
assert [c for c in calls if c[0] == "DELETE"] == [steps[0], steps[1], steps[3]], calls' "$STUB_STATE/grafana-calls.jsonl"
check "what is left: the new data source, folder and dashboard, and the data source that is not ours" python3 -c '
import json, sys
g = json.load(open(sys.argv[1]))
assert sorted(g["datasources"]) == ["other", "wattson-tempo"] and list(g["folders"]) == ["wattson-folder"] and list(g["dashboards"]) == ["wattson"], g' "$STUB_STATE/grafana.state"
check "clean: context, namespaces, no Grafana login on a command line or on screen" clean_run
# The old folder again, now with a dashboard of someone else in it: the folder is kept.
python3 -c '
import json, sys
g = json.load(open(sys.argv[1]))
g["folders"]["myagent-folder"] = {"uid": "myagent-folder", "title": "myagent"}
g["dashboards"]["mine"] = {"dashboard": {"uid": "mine"}, "folderUid": "myagent-folder", "overwrite": True}
json.dump(g, open(sys.argv[1], "w"))' "$STUB_STATE/grafana.state"
: >"$STUB_LOG"
rm -f "$STUB_STATE/grafana-calls.jsonl"
RUN_INPUT="$WORK/answer-context" run grafana.sh --apply
check "an old folder with something else in it: kept, with what is in it, and it says so" bash -c '
  [[ $1 -eq 0 ]] && grep -q "Old name: the folder myagent-folder is kept: something else is in it." "$2" && ! grep -q "\"method\": \"DELETE\"" "$3" &&
  python3 -c "import json, sys; g = json.load(open(sys.argv[1])); assert \"myagent-folder\" in g[\"folders\"] and \"mine\" in g[\"dashboards\"]" "$4"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_STATE/grafana-calls.jsonl" "$STUB_STATE/grafana.state"
# The old dashboard again, and Grafana refuses every DELETE: a WARNING, the new ones are saved.
python3 -c '
import json, sys
g = json.load(open(sys.argv[1]))
g["dashboards"]["myagent"] = {"dashboard": {"uid": "myagent"}, "folderUid": "myagent-folder", "overwrite": True}
json.dump(g, open(sys.argv[1], "w"))' "$STUB_STATE/grafana.state"
RUN_ENV=(STUB_GRAFANA_DELETE=fails)
RUN_INPUT="$WORK/answer-context" run grafana.sh --apply
RUN_ENV=()
check "the old dashboard cannot be removed: a WARNING, the new dashboard is saved, and it ends with an error (run it again)" bash -c '
  [[ $1 -ne 0 ]] && grep -q "WARNING: the dashboard myagent could not be removed: Grafana answered 500: database is locked" "$2" &&
  grep -q "Dashboard: Wattson Agent (wattson) saved" "$2" && grep -q "The old name is not removed from Grafana yet" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
show_output_on_failure "$before"

section "grafana.sh: what it refuses"
before=$FAILED
namespace_with "${GRAFANA_THERE[@]}"
RUN_INPUT="$WORK/answer-context" run grafana.sh --apply
check "no Tempo yet: refused before the question (deploy first), no call to Grafana" bash -c '
  [[ $1 -ne 0 ]] && grep -q "deploy first" "$2" && ! grep -q "^Type " "$2" && ! grep -qE "^(grafana |port-forward)" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
namespace_with service/wattson-tempo=wattson "${GRAFANA_THERE[@]}"
RUN_ENV=(STUB_GRAFANA_LITERAL_LOGIN=1)
RUN_INPUT="$WORK/answer-context" run grafana.sh --apply
RUN_ENV=()
check "Grafana without its admin login in a Secret: refused before the question" bash -c '
  [[ $1 -ne 0 ]] && grep -q "does not take its admin login from a Secret" "$2" && ! grep -qE "^(grafana |port-forward)" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
namespace_with service/wattson-tempo=wattson "${GRAFANA_THERE[@]}"
RUN_ENV=(STUB_GRAFANA_TEMPO=down)
RUN_INPUT="$WORK/answer-context" run grafana.sh --apply
RUN_ENV=()
check "Grafana cannot reach Tempo: a WARNING; the data source and the dashboard are saved all the same" bash -c '
  [[ $1 -eq 0 ]] && grep -q "WARNING: Grafana cannot reach Tempo yet" "$2" && grep -q "Dashboard: Wattson Agent (wattson) saved" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
sed 's/^TELEMETRY=.*/TELEMETRY=off/' "$FLOW_SETTINGS" >"$WORK/telemetry-off.env"
namespace_with service/wattson-tempo=wattson "${GRAFANA_THERE[@]}"
FLOW_SAVED="$FLOW_SETTINGS" FLOW_SETTINGS="$WORK/telemetry-off.env"
run grafana.sh --apply
FLOW_SETTINGS="$FLOW_SAVED"
check "TELEMETRY=off: refused, nothing called" bash -c '[[ $1 -ne 0 ]] && grep -q "TELEMETRY is off" "$2" && test ! -s "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
show_output_on_failure "$before"

# ---- doctor.sh -------------------------------------------------------------------
section "doctor.sh, on the namespace of the first deploy"
before=$FAILED
STUB_STATE="$FIRST_STATE" STUB_LOG="$FIRST_LOG" CLI_DIR="$FIRST_CLI"
: >"$STUB_LOG"
run doctor.sh
check "it finds no problem" bash -c '[[ $1 -eq 0 ]] && grep -q "No problem found." "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "it asks /healthz inside each pod, and llama's /health" bash -c '
  grep -qxF "exec deployment/wattson-orchestrator -c orchestrator: python" "$1" && grep -qxF "exec deployment/wattson-triage -c triage: python" "$1" &&
  grep -qxF "exec deployment/wattson-knowledge -c knowledge: python" "$1" && grep -qxF "exec deployment/wattson-triage -c llama: curl" "$1"' _ "$STUB_LOG"
check "it reads the model download log" called "logs deployment/wattson-triage -c fetch-model"
check "the shield: its pod, its download log, /healthz and the files inside it, its network rule, and the orchestrator reaches it" bash -c '
  grep -qxF "get pods -l app=wattson-shield" "$1" && grep -qxF "logs deployment/wattson-shield -c fetch-model" "$1" &&
  [[ "$(grep -cxF "exec deployment/wattson-shield -c shield: python" "$1")" -eq 2 ]] && grep -qxF "get networkpolicy/wattson-shield" "$1" &&
  grep -q "ok: wattson-orchestrator reaches wattson-shield:9003 (the injection model)" "$2" && grep -q "ok: the injection model runs in mode cut." "$2"' _ "$STUB_LOG" "$STUB_STATE.out"
check "it asks /v1/health through a port-forward, with the token" bash -c 'grep -qxF "port-forward deployment/wattson-orchestrator" "$1" && grep -q "\"status\": \"ok\"" "$2"' _ "$STUB_LOG" "$STUB_STATE.out"
check "it asks Tempo /ready through a port-forward" bash -c 'grep -qxF "port-forward service/wattson-tempo" "$1" && grep -qxF "curl GET /ready" "$1"' _ "$STUB_LOG"
check "it opens the OTLP port of Tempo from inside each agent pod" bash -c '
  for name in orchestrator triage knowledge; do
    [[ "$(grep -cxF "exec deployment/wattson-$name -c $name: python" "$1")" -ge 2 ]] || exit 1
  done
  grep -q "ok: wattson-knowledge reaches wattson-tempo:4318 (OTLP)" "$2"' _ "$STUB_LOG" "$STUB_STATE.out"
check "it finds the data source and the dashboard in Grafana, and that Grafana reaches Tempo" bash -c '
  grep -q "ok: the data source Wattson Tempo (wattson-tempo) is there." "$1" && grep -q "ok: the dashboard Wattson Agent (wattson) is there." "$1" &&
  grep -q "ok: Grafana reaches Tempo" "$1"' _ "$STUB_STATE.out"
check "it changes nothing" not grep -Eq '^(apply|create|delete|docker)' "$STUB_LOG"
check "it only reads Grafana (GET calls)" bash -c '! grep -E "^grafana " "$1" | grep -qv "^grafana GET "' _ "$STUB_LOG"
check "clean: context, namespace, no secret shown" clean_run
check "the port-forwards are stopped at the end" stopped_soon "$WORK/bin/kubectl.*port-forward"
show_output_on_failure "$before"

before=$FAILED
cp "$CLI_DIR/token" "$WORK/token.saved"
printf 'x%.0s' {1..43} >"$CLI_DIR/token"
: >"$STUB_LOG"
run doctor.sh
check "a token that does not match: a PROBLEM, and the way to fix it" bash -c '[[ $1 -ne 0 ]] && grep -q "the token was refused" "$2" && grep -q "run bash k8s/deploy.sh" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
chmod 644 "$CLI_DIR/token"
run doctor.sh
check "a token file others can read: a PROBLEM, with chmod 600" bash -c '[[ $1 -ne 0 ]] && grep -q "chmod 600" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
cp "$WORK/token.saved" "$CLI_DIR/token" && chmod 600 "$CLI_DIR/token"
show_output_on_failure "$before"

before=$FAILED
# Grafana restarted: its emptyDir is new, and what grafana.sh added is gone.
mv "$STUB_STATE/grafana.state" "$WORK/grafana.saved"
: >"$STUB_LOG"
run doctor.sh
check "Grafana lost the data source and the dashboard: two PROBLEMs, each with bash k8s/grafana.sh --apply" bash -c '
  [[ $1 -ne 0 ]] && [[ "$(grep -c "^PROBLEM: .*Run: bash k8s/grafana.sh --apply" "$2")" -eq 2 ]]' _ "$RUN_EXIT" "$STUB_STATE.out"
mv "$WORK/grafana.saved" "$STUB_STATE/grafana.state"
RUN_ENV=(STUB_GRAFANA_TEMPO=down)
run doctor.sh
RUN_ENV=()
check "Grafana cannot reach Tempo: a PROBLEM that names the network rule" bash -c '
  [[ $1 -ne 0 ]] && grep -q "PROBLEM: Grafana cannot reach Tempo" "$2" && grep -q "network rule (namespace test-grafana, port 3200)" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
show_output_on_failure "$before"

section "doctor.sh: the Guard"
before=$FAILED
: >"$STUB_LOG"
run doctor.sh
check "it shows the mode of the Guard, and that it has no model with the sidecar" said "Guard: mode monitor; fixed rules only (GUARD_MODEL_URL is empty)"
check "the rules on this laptop pass, the ConfigMap is there, the orchestrator runs them, in mode monitor" bash -c '
  grep -q "ok: the rules in .* pass the checks: 2 rules, 1 on" "$1" && grep -q "ok: the ConfigMap wattson-guard (the rules of the Guard) is there." "$1" &&
  grep -q "ok: the orchestrator runs the rules of " "$1" && grep -q "ok: the orchestrator runs in mode monitor." "$1"' _ "$STUB_STATE.out"
check "with the sidecar it does not ask KubeAI from the orchestrator" not said "reaches KubeAI for the Guard"
RUN_ENV=("GUARD_DIR=$WORK/guard-other")
run doctor.sh
RUN_ENV=()
check "rules changed on this laptop: no problem, a note with bash k8s/guard.sh --apply" bash -c '
  [[ $1 -eq 0 ]] && grep -q "note: the orchestrator does not run the rules of .*bash k8s/guard.sh --apply" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
RUN_ENV=("GUARD_DIR=$WORK/guard-deploy-bad")
run doctor.sh
RUN_ENV=()
check "wrong rules on this laptop: a PROBLEM that says what is wrong" bash -c '
  [[ $1 -ne 0 ]] && grep -q "PROBLEM: the rules in .* are not right: rule 1 (x): regex 1 is not valid.*Fix them, then: bash k8s/guard.sh --apply" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
sed 's/^GUARD_MODE=.*/GUARD_MODE=enforce/' "$FLOW_SETTINGS" >"$WORK/flow-enforce.env"
FLOW_SAVED="$FLOW_SETTINGS"
FLOW_SETTINGS="$WORK/flow-enforce.env"
run doctor.sh
FLOW_SETTINGS="$FLOW_SAVED"
check "settings.env says enforce, the orchestrator runs monitor: a note with bash k8s/deploy.sh" bash -c '
  [[ $1 -eq 0 ]] && grep -q "note: the orchestrator runs in mode monitor, and settings.env says enforce. To change it: bash k8s/deploy.sh" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "it changes nothing" not grep -Eq '^(apply|create|delete|patch|docker)' "$STUB_LOG"
show_output_on_failure "$before"

# ---- guard.sh --------------------------------------------------------------------
# On a copy of the namespace of the first deploy: the remove.sh runs below start from it.
section "guard.sh: new rules of the Guard, without a new image"
before=$FAILED
GUARD_NS="$WORK/guard-namespace"
cp -r "$FIRST_STATE" "$GUARD_NS"
STUB_STATE="$GUARD_NS" STUB_LOG="$GUARD_NS.log" CLI_DIR="$FIRST_CLI"
NEW_RULES="$(guard_variant new "$(cat "$GUARD_DIR/rules.yaml")
  - name: drop-database
    label: malicious
    match: [drop database]
    reason: It asks to destroy a database.")"
NEW_SHA="$(cd "$NEW_RULES" && sha256sum rules.yaml policies.cedar | sha256sum | cut -c1-64)"
: >"$STUB_LOG"
RUN_ENV=("GUARD_DIR=$NEW_RULES")
run guard.sh
check "without --apply: it ends well, says that nothing was run, and calls no kubectl at all" bash -c '
  [[ $1 -eq 0 ]] && grep -q "Nothing was run. To put these rules on the cluster: bash k8s/guard.sh --apply" "$2" && test ! -s "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
check "without --apply: it shows what it checked and the commands, for the context and namespace of the settings" bash -c '
  grep -q "^# Checked: 3 rules, 2 on (malicious 1, risky 1)" "$1" && grep -q "^kubectl --context test-context -n test-ns apply -f -" "$1" &&
  grep -q "^kubectl --context test-context -n test-ns patch deployment wattson-orchestrator --type merge" "$1" &&
  grep -q "^kubectl --context test-context -n test-ns rollout status deployment/wattson-orchestrator --timeout=300s" "$1"' _ "$STUB_STATE.out"
RUN_INPUT="$WORK/answer-no-context" run guard.sh --apply
check "--apply without the typed context: nothing is changed, and before the question it only reads" bash -c '
  [[ $1 -ne 0 ]] && grep -q "Nothing was changed." "$2" && ! grep -vE "^(config view|get )" "$3" | grep -q .' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
: >"$STUB_LOG"
RUN_INPUT="$WORK/answer-context" run guard.sh --apply
RUN_ENV=()
check "--apply with the typed context: it ends well" test "$RUN_EXIT" -eq 0
check "it applies the ConfigMap only, then restarts the orchestrator with the SHA-256 of the new rules, and waits for it" python3 -c '
import json, re, sys
log = open(sys.argv[1]).read().splitlines()
changes = [line for line in log if re.match(r"(apply|create|delete|patch) [a-z]+/", line)]
patch = "patch deployment/wattson-orchestrator merge " + json.dumps({"spec": {"template": {"metadata": {"annotations": {"wattson/guard-config": sys.argv[2]}}}}}, sort_keys=True)
assert changes == ["apply configmap/wattson-guard", patch], changes
assert log.index(patch) < log.index("wait for deployment/wattson-orchestrator --timeout=300s"), log' "$STUB_LOG" "$NEW_SHA"
check "the ConfigMap holds the new files as they are; the pod of the orchestrator carries their SHA-256" python3 -c '
import json, os, sys
state, guard, sha = sys.argv[1:]
data = json.load(open(state + "/configmap__wattson-guard.json"))["doc"]["data"]
assert data == {n: open(os.path.join(guard, n), newline="").read() for n in ("rules.yaml", "policies.cedar")}, sorted(data)
meta = json.load(open(state + "/deployment__wattson-orchestrator.json"))["doc"]["spec"]["template"]["metadata"]
assert meta["annotations"]["wattson/guard-config"] == sha, meta' "$STUB_STATE" "$NEW_RULES" "$NEW_SHA"
check "it points to Grafana" said "See its decisions in Grafana: the dashboard \"Wattson Agent\""
check "clean: context, namespace, no secret shown" clean_run
: >"$STUB_LOG"
RUN_ENV=("GUARD_DIR=$NEW_RULES")
RUN_INPUT="$WORK/answer-context" run guard.sh --apply
RUN_ENV=()
check "the same rules again: nothing to do, nothing changed" bash -c '
  [[ $1 -eq 0 ]] && grep -q "The orchestrator runs these rules already" "$2" && ! grep -Eq "^(apply|patch|wait)" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
: >"$STUB_LOG"
RUN_ENV=("GUARD_DIR=$WORK/guard-deploy-bad")
RUN_INPUT="$WORK/answer-context" run guard.sh --apply
RUN_ENV=()
check "wrong rules: refused before any kubectl call, with what is wrong" bash -c '
  [[ $1 -ne 0 ]] && test ! -s "$2" && grep -q "PROBLEM: rule 1 (x): regex 1 is not valid" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
show_output_on_failure "$before"

before=$FAILED
namespace_with deployment/wattson-orchestrator=wattson
RUN_INPUT="$WORK/answer-context" run guard.sh --apply
check "an orchestrator deployed before the Guard (no ConfigMap mounted): refused, with bash k8s/deploy.sh; nothing changed" bash -c '
  [[ $1 -ne 0 ]] && grep -q "it was deployed before the Guard. Run bash k8s/deploy.sh" "$2" && ! grep -Eq "^(apply|patch)" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
namespace_with
RUN_INPUT="$WORK/answer-context" run guard.sh --apply
check "no orchestrator: refused, with bash k8s/deploy.sh first" bash -c '
  [[ $1 -ne 0 ]] && grep -q "There is no Deployment wattson-orchestrator" "$2" && ! grep -Eq "^(apply|patch)" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
namespace_with configmap/wattson-guard=someone-else
cp "$FIRST_STATE/deployment__wattson-orchestrator.json" "$STUB_STATE/"
RUN_INPUT="$WORK/answer-context" run guard.sh --apply
check "a ConfigMap wattson-guard that is not ours: refused, nothing changed" bash -c '
  [[ $1 -ne 0 ]] && grep -q "was not made by k8s/deploy.sh" "$2" && ! grep -Eq "^(apply|patch)" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
show_output_on_failure "$before"

# ---- remove.sh -------------------------------------------------------------------
section "remove.sh"
before=$FAILED
STUB_STATE="$FIRST_STATE" STUB_LOG="$FIRST_LOG" CLI_DIR="$FIRST_CLI"
cp -r "$STUB_STATE" "$WORK/after-deploy"
printf 'no\n' >"$WORK/answer-no"
printf 'wattson\n' >"$WORK/answer-yes"
: >"$STUB_LOG"
RUN_INPUT="$WORK/answer-no" run remove.sh
check "without the typed name: nothing is removed" bash -c '[[ $1 -ne 0 ]] && ! grep -q "^delete" "$2" && grep -q "Nothing was removed." "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
RUN_INPUT="/dev/null" run remove.sh
check "without any answer: nothing is removed" bash -c '[[ $1 -ne 0 ]] && ! grep -q "^delete" "$2"' _ "$RUN_EXIT" "$STUB_LOG"
RUN_INPUT="$WORK/answer-yes" run remove.sh
check "with the typed name: it ends well" test "$RUN_EXIT" -eq 0
for object in deployment/wattson-orchestrator deployment/wattson-triage deployment/wattson-knowledge service/wattson-triage service/wattson-knowledge \
  networkpolicy/wattson-orchestrator networkpolicy/wattson-triage networkpolicy/wattson-knowledge \
  serviceaccount/wattson-orchestrator serviceaccount/wattson-triage serviceaccount/wattson-knowledge secret/wattson \
  deployment/wattson-tempo service/wattson-tempo networkpolicy/wattson-tempo serviceaccount/wattson-tempo configmap/wattson-tempo \
  configmap/wattson-guard; do
  check "it removes $object" called "delete $object"
done
check "the volumes are kept (the traces too)" bash -c '! grep -q "^delete persistentvolumeclaim" "$1" && ls "$2" | grep -c "^persistentvolumeclaim__" | grep -qx 4' _ "$STUB_LOG" "$STUB_STATE"
check "Grafana is not touched" not grep -qE '(^delete .*monitoring|in test-grafana$)' "$STUB_LOG"
check "the pull secret and the namespace are not touched" not grep -Eq '^delete (secret/my-pull-secret|namespace)' "$STUB_LOG"
check "clean: context and namespace" clean_run
show_output_on_failure "$before"

before=$FAILED
rm -rf "$STUB_STATE" && cp -r "$WORK/after-deploy" "$STUB_STATE"
: >"$STUB_LOG"
RUN_INPUT="$WORK/answer-yes" run remove.sh --delete-data
check "--delete-data: the four volumes go too (the traces too)" bash -c '[[ $1 -eq 0 ]] && [[ "$(grep -c "^delete persistentvolumeclaim/" "$2")" -eq 4 ]]' _ "$RUN_EXIT" "$STUB_LOG"
check "--delete-data: it warns before asking" said "WITH THE VOLUMES"
show_output_on_failure "$before"

before=$FAILED
namespace_with deployment/wattson-triage=someone-else deployment/wattson-knowledge=wattson
RUN_INPUT="$WORK/answer-yes" run remove.sh
check "an object with our name that is not ours is left alone" bash -c '[[ $1 -eq 0 ]] && ! grep -q "^delete deployment/wattson-triage" "$2" && grep -q "^delete deployment/wattson-knowledge" "$2"' _ "$RUN_EXIT" "$STUB_LOG"
show_output_on_failure "$before"

# ---- remove-old-myagent.sh ---------------------------------------------------------
# The old install, as it ran before the rename (23 Oct 2026): every object myagent-* with the
# label part-of=myagent, its read-only access in the whole cluster, and the KubeAI Model with
# the old label. The new install is there too (deployment/wattson-orchestrator).
OLD_FLOW="$WORK/old-flow-settings.env"
sed -e 's/^MODEL_BACKEND=.*/MODEL_BACKEND=kubeai/' -e 's/^KUBEAI_NAMESPACE=.*/KUBEAI_NAMESPACE=test-kubeai/' \
  -e 's#^KUBEAI_BASE_URL=.*#KUBEAI_BASE_URL=http://kubeai.test-kubeai.svc.cluster.local/openai/v1#' "$FLOW_SETTINGS" >"$OLD_FLOW"
OLD_OBJECTS=(deployment/myagent-orchestrator deployment/myagent-triage deployment/myagent-knowledge deployment/myagent-tempo
  service/myagent-triage service/myagent-knowledge service/myagent-tempo
  networkpolicy/myagent-orchestrator networkpolicy/myagent-triage networkpolicy/myagent-knowledge networkpolicy/myagent-tempo
  serviceaccount/myagent-orchestrator serviceaccount/myagent-triage serviceaccount/myagent-knowledge serviceaccount/myagent-tempo
  configmap/myagent-tempo secret/myagent serviceaccount/myagent-reader secret/myagent-reader-token)
OLD_CLUSTER=(clusterrolebinding/myagent-reader-view clusterrolebinding/myagent-reader-nodes clusterrole/myagent-reader-nodes)
OLD_VOLUMES=(persistentvolumeclaim/myagent-data persistentvolumeclaim/myagent-knowledge-data persistentvolumeclaim/myagent-models
  persistentvolumeclaim/myagent-tempo-data)
old_install() {  # [THING...] -> the old install next to the new one, then these things
  namespace_with "${OLD_OBJECTS[@]/%/=myagent}" "${OLD_CLUSTER[@]/%/=myagent}" "${OLD_VOLUMES[@]/%/=myagent}" \
    "secret/myagent=myagent:MYAGENT_TOKEN,TRIAGE_TOKEN,KNOWLEDGE_TOKEN" model/granite-4-2-3b=myagent crd/models.kubeai.org \
    deployment/wattson-orchestrator=wattson deployment/wattson-triage=wattson persistentvolumeclaim/wattson-data=wattson "$@"
}
run_old() {  # ARGS... -> remove-old-myagent.sh with the settings above
  local saved="$FLOW_SETTINGS"
  FLOW_SETTINGS="${OLD_FLOW_FILE:-$OLD_FLOW}"
  run remove-old-myagent.sh "$@"
  FLOW_SETTINGS="$saved"
}
printf 'test-context\nno\n' >"$WORK/answer-old-no"
printf 'test-context\nyes\n' >"$WORK/answer-old-yes"

section "remove-old-myagent.sh: what it shows (without --apply)"
before=$FAILED
old_install
run_old
check "it ends well, deletes nothing, and says how to delete" bash -c '
  [[ $1 -eq 0 ]] && grep -q "Nothing was deleted. To delete it: bash k8s/remove-old-myagent.sh --apply" "$2" && ! grep -q "^delete" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
check "it lists the 19 objects of the namespace, the 4 old volumes and the 3 objects of the whole cluster" python3 -c '
import sys
lines = open(sys.argv[1]).read().splitlines()
for thing in sys.argv[2:]:
    assert "  " + thing in lines, thing
assert len(sys.argv[2:]) == 26' "$STUB_STATE.out" "${OLD_OBJECTS[@]}" "${OLD_VOLUMES[@]}" "${OLD_CLUSTER[@]}"
check "it only reads: one get per object, the objects of the whole cluster without a namespace" bash -c '
  ! grep -vE "^(config view --context test-context|get [a-z]+/[a-z0-9.-]+( in test-kubeai)?)$" "$1" | grep -q . &&
  grep -qxF "get clusterrolebinding/myagent-reader-view" "$1" && grep -qxF "get clusterrole/myagent-reader-nodes" "$1"' _ "$STUB_LOG"
check "in the namespace of KubeAI it only reads the Model, and says that it is kept (kubeai.sh gives it the new label)" bash -c '
  [[ "$(grep " in test-kubeai$" "$1")" == "get model/granite-4-2-3b in test-kubeai" ]] &&
  grep -q "nothing to delete. The Model granite-4-2-3b is kept, but it still has the label part-of=myagent: run bash k8s/kubeai.sh --apply." "$2"' _ "$STUB_LOG" "$STUB_STATE.out"
check "the new install is there: no NOT READY" not said "NOT READY"
check "clean: the context of the settings, no value of a Secret on a command line or on screen" clean_run
# The reader kubeconfig on the laptop still has the old account.
mkdir -p "$CLI_DIR/kube"
printf '{"users": [{"name": "myagent-reader"}], "current-context": "test-context"}\n' >"$CLI_DIR/kube/test-context.yaml"
run_old
check "a reader kubeconfig with the old account: a NOTE with wattson k8s add" said "still uses the old account myagent-reader: run wattson k8s add test-context first"
show_output_on_failure "$before"

section "remove-old-myagent.sh --apply"
before=$FAILED
old_install
RUN_INPUT="$WORK/answer-no-context" run_old --apply
check "without the typed context: nothing is deleted" bash -c '[[ $1 -ne 0 ]] && grep -q "Nothing was deleted." "$2" && ! grep -q "^delete" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
: >"$STUB_LOG"
RUN_INPUT="$WORK/answer-old-no" run_old --apply
check "with the typed context and no to the volumes: it ends well" test "$RUN_EXIT" -eq 0
for object in "${OLD_OBJECTS[@]}" "${OLD_CLUSTER[@]}"; do
  check "it deletes $object" called "delete $object"
done
check "the Deployments go first (their pods stop using the volumes)" bash -c '
  [[ "$(grep "^delete " "$1" | head -n 4 | cut -d/ -f1 | sort -u)" == "delete deployment" ]]' _ "$STUB_LOG"
check "no to the volumes: they are kept, and it says how to delete them later" bash -c '
  ! grep -q "^delete persistentvolumeclaim/" "$1" && [[ "$(ls "$2" | grep -c "^persistentvolumeclaim__myagent-")" -eq 4 ]] &&
  grep -q "the old volumes are kept" "$3" && grep -q "kubectl --context test-context -n test-ns delete persistentvolumeclaim/myagent-data" "$3"' _ "$STUB_LOG" "$STUB_STATE" "$STUB_STATE.out"
check "only the old install is deleted: not the new one, not KubeAI (the Model stays), not Grafana" bash -c '
  only_old() { ! grep -E "^delete " "$1" | grep -Eq "wattson|harbor|monitoring|namespace/| in test-kubeai$| in test-grafana$"; }
  only_old "$1" && test -f "$2/model__granite-4-2-3b.json" && test -f "$2/deployment__wattson-orchestrator.json"' _ "$STUB_LOG" "$STUB_STATE"
check "clean: the context of the settings, objects of the whole cluster without a namespace" clean_run
show_output_on_failure "$before"

before=$FAILED
old_install
RUN_INPUT="$WORK/answer-old-yes" run_old --apply
check "yes to the volumes: the 4 old volumes go too, after the other objects" bash -c '
  [[ $1 -eq 0 ]] && [[ "$(grep -c "^delete persistentvolumeclaim/myagent-" "$2")" -eq 4 ]] &&
  [[ "$(grep "^delete " "$2" | tail -n 4 | cut -d/ -f1 | sort -u)" == "delete persistentvolumeclaim" ]] &&
  grep -q "Done. The old install is removed from the cluster." "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
check "it warns before it asks about the volumes" said "Their data cannot come back."
check "the volume of the new install is not touched" test -f "$STUB_STATE/persistentvolumeclaim__wattson-data.json"
check "clean (with the volumes)" clean_run
old_install
printf 'test-context\n' >"$WORK/answer-old-keep"
RUN_INPUT="$WORK/answer-old-keep" run_old --apply --keep-data
check "--keep-data: the objects go, the volumes stay, and it does not ask about them" bash -c '
  [[ $1 -eq 0 ]] && grep -q "^delete deployment/myagent-orchestrator$" "$2" && ! grep -q "^delete persistentvolumeclaim" "$2" &&
  ! grep -q "Type \"yes\"" "$3" && grep -q "the old volumes are kept" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
old_install deployment/myagent-triage=someone-else
RUN_INPUT="$WORK/answer-old-no" run_old --apply
check "an old name with another label is left alone, and named" bash -c '
  [[ $1 -eq 0 ]] && ! grep -q "^delete deployment/myagent-triage$" "$2" && grep -q "^delete deployment/myagent-knowledge$" "$2" &&
  grep -q "deployment/myagent-triage (label: someone-else)" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
show_output_on_failure "$before"

section "remove-old-myagent.sh: what it refuses"
before=$FAILED
namespace_with "${OLD_OBJECTS[@]/%/=myagent}" "${OLD_VOLUMES[@]/%/=myagent}"
RUN_INPUT="$WORK/answer-old-yes" run_old --apply
check "the new install is not there: refused before any question, nothing deleted" bash -c '
  [[ $1 -ne 0 ]] && grep -q "NOT READY: the new install is not there" "$2" && grep -q "Deploy it first" "$2" &&
  ! grep -q "^Type " "$2" && ! grep -q "^delete" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
namespace_with deployment/wattson-orchestrator=wattson
RUN_INPUT="$WORK/answer-old-yes" run_old --apply
check "nothing of the old install: it says so, asks nothing, deletes nothing" bash -c '
  [[ $1 -eq 0 ]] && grep -q "Nothing of the old install is there." "$2" && ! grep -q "Type " "$2" && ! grep -q "^delete" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
sed 's/^APP_NAME=.*/APP_NAME=myagent/' "$OLD_FLOW" >"$WORK/old-app-name.env"
old_install
: >"$STUB_LOG"
OLD_FLOW_FILE="$WORK/old-app-name.env" RUN_INPUT="$WORK/answer-old-yes" run_old --apply
check "APP_NAME=myagent (that would be the install it deletes): refused, no kubectl call" bash -c '
  [[ $1 -ne 0 ]] && grep -q "APP_NAME is myagent" "$2" && test ! -s "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
check "remove-old-myagent.sh deletes nothing in the namespace of KubeAI or Grafana, and its lists never use APP_NAME" bash -c '
  ! grep -Eq "(kubeai_k|grafana_k) delete|grafana_call (DELETE|POST|PUT)" k8s/remove-old-myagent.sh &&
  ! grep -E "^(NAMESPACED|CLUSTER_WIDE|VOLUMES)=|^  \"" k8s/remove-old-myagent.sh | grep -q "APP_NAME"'
show_output_on_failure "$before"

# ---- MODEL_BACKEND=kubeai: deploy.sh, doctor.sh, kubeai.sh ------------------------------
# KubeAI lives in the namespace test-kubeai of the stand-in. deploy.sh only reads there;
# kubeai.sh installs there, and only with --apply and the typed name of the context.
KUBEAI_FLOW="$WORK/kubeai-flow-settings.env"
sed -e 's/^MODEL_BACKEND=.*/MODEL_BACKEND=kubeai/' -e 's/^KUBEAI_NAMESPACE=.*/KUBEAI_NAMESPACE=test-kubeai/' \
  -e 's#^KUBEAI_BASE_URL=.*#KUBEAI_BASE_URL=http://kubeai.test-kubeai.svc.cluster.local/openai/v1#' "$FLOW_SETTINGS" >"$KUBEAI_FLOW"
KUBEAI_POLICIES=(networkpolicy/wattson-kubeai-deny-ingress networkpolicy/wattson-kubeai-proxy networkpolicy/wattson-kubeai-models)
KUBEAI_THERE=(service/kubeai model/granite-4-2-3b=wattson crd/models.kubeai.org "${KUBEAI_POLICIES[@]/%/=wattson}")
with_kubeai_settings() {  # COMMAND... -> runs it with the kubeai flow settings
  local saved="$FLOW_SETTINGS"
  FLOW_SETTINGS="${KUBEAI_FLOW_FILE:-$KUBEAI_FLOW}"
  "$@"
  FLOW_SETTINGS="$saved"
}

section "deploy.sh with MODEL_BACKEND=kubeai, KubeAI installed"
before=$FAILED
namespace_with "${KUBEAI_THERE[@]}" "${GRAFANA_THERE[@]}"
with_kubeai_settings run deploy.sh
check "it ends well" test "$RUN_EXIT" -eq 0
check "it looks for the proxy Service and the Model in the namespace of KubeAI (only reads there)" bash -c '
  grep -qxF "get service/kubeai in test-kubeai" "$1" && grep -qxF "get model/granite-4-2-3b in test-kubeai" "$1" &&
  ! grep -E " in test-kubeai$" "$1" | grep -qvE "^get "' _ "$STUB_LOG"
check "it installs nothing of KubeAI (no helm)" not grep -q '^helm ' "$STUB_LOG"
check "it applies the 24 objects (14 of the agents with the rules of the Guard, 6 of Tempo, 4 of the shield), without the models volume" bash -c '
  [[ "$(grep -cE "^apply [a-z]+/" "$1")" -eq 24 ]] && ! grep -q "^apply persistentvolumeclaim/wattson-models" "$1"' _ "$STUB_LOG"
check "the applied triage pod has the triage container only, and asks the KubeAI proxy" python3 -c '
import json, sys
doc = json.load(open(sys.argv[1] + "/deployment__wattson-triage.json"))["doc"]
spec = doc["spec"]["template"]["spec"]
assert not spec.get("initContainers") and [c["name"] for c in spec["containers"]] == ["triage"], spec
env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
assert env["LLM_BASE_URL"] == "http://kubeai.test-kubeai.svc.cluster.local/openai/v1" and env["LLM_MODEL"] == "granite-4-2-3b", env' "$STUB_STATE"
check "it waits 5 minutes for triage, also the first time (no model download in the pod)" called "wait for deployment/wattson-triage --timeout=300s"
check "it says that KubeAI serves the model" said "KubeAI serves granite-4-2-3b"
check "the applied orchestrator asks the KubeAI proxy for the model of the Guard, and may go out to it" python3 -c '
import json, sys
spec = json.load(open(sys.argv[1] + "/deployment__wattson-orchestrator.json"))["doc"]["spec"]["template"]["spec"]
env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
assert env["GUARD_MODEL_URL"] == "http://kubeai.test-kubeai.svc.cluster.local/openai/v1" and env["GUARD_MODEL"] == "granite-4-2-3b" and env["GUARD_MODE"] == "monitor", env
egress = json.load(open(sys.argv[1] + "/networkpolicy__wattson-orchestrator.json"))["doc"]["spec"]["egress"]
assert {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "test-kubeai"}}, "podSelector": {"matchLabels": {"app.kubernetes.io/name": "kubeai"}}}],
        "ports": [{"protocol": "TCP", "port": 8000}]} in egress, egress' "$STUB_STATE"
check "it says that the Guard asks the small model after its rules" said "Guard: monitor; fixed rules, then the small model granite-4-2-3b (KubeAI)."
check "clean: context, namespaces, no secret shown" clean_run
show_output_on_failure "$before"
KUBEAI_STATE="$STUB_STATE" KUBEAI_LOG="$STUB_LOG" KUBEAI_CLI="$CLI_DIR"

section "deploy.sh with MODEL_BACKEND=kubeai: what it refuses before changing anything"
before=$FAILED
namespace_with
with_kubeai_settings run deploy.sh
check "KubeAI not installed: refused, with the way to install it" bash -c '[[ $1 -ne 0 ]] && grep -q "bash k8s/kubeai.sh --apply" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "KubeAI not installed: nothing was made" nothing_changed
namespace_with service/kubeai
RUN_ENV=(STUB_NO_KUBEAI_CRD=1)
with_kubeai_settings run deploy.sh
RUN_ENV=()
check "the proxy but no Model CRD: refused, with the way to install it" bash -c '[[ $1 -ne 0 ]] && grep -q "Model granite-4-2-3b" "$2" && grep -q "kubeai.sh --apply" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "no Model CRD: nothing was made" nothing_changed
namespace_with "${KUBEAI_THERE[@]}"
RUN_ENV=(STUB_BROKEN=service/kubeai)
with_kubeai_settings run deploy.sh
RUN_ENV=()
check "the namespace of KubeAI cannot be asked: refused" bash -c '[[ $1 -ne 0 ]] && grep -q "could not look for the service" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "the namespace of KubeAI cannot be asked: nothing was made" nothing_changed
show_output_on_failure "$before"

section "doctor.sh with MODEL_BACKEND=kubeai"
before=$FAILED
STUB_STATE="$KUBEAI_STATE" STUB_LOG="$KUBEAI_LOG" CLI_DIR="$KUBEAI_CLI"
cp "$WORK/grafana-after-first.json" "$STUB_STATE/grafana.state"
MODEL_SHA="$(value_of "$KUBEAI_FLOW" MODEL_SHA256)"
LLAMA_PINNED="$(value_of "$KUBEAI_FLOW" LLAMA_IMAGE)"
# Where llama.cpp keeps the file of MODEL_URL in the model pod: <cache>/models--<user>--<repo>/
# snapshots/<commit>/<file>.
MODEL_IN_POD="/llama-cache/models--ibm-granite--granite-4.2-3b-GGUF/snapshots/$(value_of "$KUBEAI_FLOW" MODEL_HF_COMMIT)/$(value_of "$KUBEAI_FLOW" MODEL_FILE)"
MODEL_POD_ENV=(STUB_MODEL_PODS=1 STUB_MODEL_REPLICAS=1/1 "STUB_MODEL_IMAGE=$LLAMA_PINNED" "STUB_MODEL_PATH=$MODEL_IN_POD")
: >"$STUB_LOG"
RUN_ENV=("${MODEL_POD_ENV[@]}" "STUB_MODEL_SHA256=$MODEL_SHA")
with_kubeai_settings run doctor.sh
RUN_ENV=()
check "the model running: no problem found" bash -c '[[ $1 -eq 0 ]] && grep -q "No problem found." "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "it says the model is running" said "model: running"
check "it shows the engine of the Model (llama.cpp) and its image" bash -c '
  grep -q "^Model granite-4-2-3b: engine=LlamaCpp hf://" "$1" && grep -q "engine llama.cpp, image ghcr.io/ggml-org/llama.cpp:server-b11804" "$1" &&
  grep -q "runs the pinned llama.cpp image" "$1"' _ "$STUB_STATE.out"
check "it shows the state of the model pod" bash -c 'grep -q "^model-granite-4-2-3b-7f9c6d5b4-xyz12 .*Running" "$1" && grep -q "^  server: ready=true restarts=0" "$1"' _ "$STUB_STATE.out"
check "it asks the KubeAI proxy for /models from inside the triage pod" called "exec deployment/wattson-triage -c triage: python"
check "it asks the KubeAI proxy for /models from inside the orchestrator too (the model of the Guard)" bash -c '
  grep -q "^Guard: mode monitor; fixed rules, then the small model granite-4-2-3b (http://kubeai.test-kubeai.svc.cluster.local/openai/v1), at most 20s" "$1" &&
  grep -q "^ok: wattson-orchestrator reaches KubeAI for the Guard: http://kubeai.test-kubeai.svc.cluster.local/openai/v1/models -> 200" "$1"' _ "$STUB_STATE.out"
check "it checks the commit and the SHA-256 of the model file in the model pod" bash -c '
  grep -qxF "exec model-granite-4-2-3b-7f9c6d5b4-xyz12 -c server: sha256sum in test-kubeai" "$1" && grep -q "at the commit of MODEL_URL, with the SHA-256 of MODEL_SHA256" "$2"' _ "$STUB_LOG" "$STUB_STATE.out"
check "it asks no llama container and reads no model download log of triage" not grep -Eq -- '(-c llama|wattson-triage -c fetch-model)' "$STUB_LOG"
check "it finds the three network rules of KubeAI, in its namespace" bash -c '
  grep -q "ok: the network rules wattson-kubeai-deny-ingress wattson-kubeai-proxy wattson-kubeai-models are there" "$1" &&
  grep -qxF "get networkpolicy/wattson-kubeai-proxy in test-kubeai" "$2"' _ "$STUB_STATE.out" "$STUB_LOG"
check "it changes nothing" not grep -Eq '^(apply|create|delete|docker|helm)' "$STUB_LOG"
check "clean: context, namespaces, no secret shown" clean_run
: >"$STUB_LOG"
with_kubeai_settings run doctor.sh
check "the model at 0: no problem, and it says so" bash -c '[[ $1 -eq 0 ]] && grep -q "model: at 0" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
mv "$STUB_STATE/networkpolicy__wattson-kubeai-proxy.json" "$WORK/kubeai-proxy-policy.saved"
with_kubeai_settings run doctor.sh
mv "$WORK/kubeai-proxy-policy.saved" "$STUB_STATE/networkpolicy__wattson-kubeai-proxy.json"
check "a network rule of KubeAI missing: a PROBLEM that names it, with kubeai.sh --apply" bash -c '
  [[ $1 -ne 0 ]] && grep -q "PROBLEM: the network rules wattson-kubeai-proxy are not in test-kubeai: any pod of the cluster may ask the model. Run: bash k8s/kubeai.sh --apply" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
RUN_ENV=("${MODEL_POD_ENV[@]}")
with_kubeai_settings run doctor.sh
RUN_ENV=()
check "a model file with another SHA-256: a PROBLEM" bash -c '[[ $1 -ne 0 ]] && grep -q "PROBLEM: the model file in .* has the SHA-256 0\{64\}, not MODEL_SHA256" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
RUN_ENV=("${MODEL_POD_ENV[@]}" "STUB_MODEL_PATH=/llama-cache/models--ibm-granite--granite-4.2-3b-GGUF/snapshots/$(printf 'a%.0s' {1..40})/x.gguf" "STUB_MODEL_COMMITS=$(printf 'a%.0s' {1..40})")
with_kubeai_settings run doctor.sh
RUN_ENV=()
check "the pod ready, but its file of another commit (main has moved): a PROBLEM that says so" bash -c '
  [[ $1 -ne 0 ]] && grep -q "PROBLEM: the model pod is ready, but has no granite-4.2-3b-Q4_K_M.gguf of the commit .*it has: a\{40\}.*has moved" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
RUN_ENV=("${MODEL_POD_ENV[@]}" STUB_MODEL_PATH=/nowhere STUB_MODEL_READY=false STUB_MODEL_REPLICAS=1/)
with_kubeai_settings run doctor.sh
RUN_ENV=()
check "the pod still downloading: no problem, it says so" bash -c '
  [[ $1 -eq 0 ]] && grep -q "model: starting" "$2" && grep -q "the pod is still downloading it" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
RUN_ENV=("${MODEL_POD_ENV[@]}" "STUB_MODEL_SHA256=$MODEL_SHA" STUB_MODEL_IMAGE=ollama/ollama:0.40.9 STUB_MODEL_ENGINE=OLlama)
with_kubeai_settings run doctor.sh
RUN_ENV=()
check "the Model still on Ollama: a PROBLEM for the engine and one for the image, with the way to fix them" bash -c '
  [[ $1 -ne 0 ]] && grep -q "PROBLEM: the Model granite-4-2-3b does not run on KubeAI.s llama.cpp engine (engine=OLlama). Run: bash k8s/kubeai.sh --apply" "$2" &&
  grep -q "PROBLEM: .* runs the image \"ollama/ollama:0.40.9\", not LLAMA_IMAGE" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
show_output_on_failure "$before"

section "kubeai.sh"
before=$FAILED
STAND_IN_CHART_SHA="$(printf 'a stand-in chart\n' | sha256sum | cut -c1-64)"
namespace_with
with_kubeai_settings run kubeai.sh
check "without --apply: it ends well and says that nothing was run" bash -c '[[ $1 -eq 0 ]] && grep -q "Nothing was run" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "without --apply: no kubectl and no helm call at all" test ! -s "$STUB_LOG"
check "without --apply: it prints the filled-in values and Model (pinned llama.cpp image, 0 when idle, 1 hour warm)" bash -c '
  grep -qF "default: \"$1\"" "$2" && grep -q "^kind: Model" "$2" && grep -q "^  name: granite-4-2-3b" "$2" && grep -q "^  engine: LlamaCpp" "$2" &&
  grep -q "^  minReplicas: 0" "$2" && grep -q "^  scaleDownDelaySeconds: 3600" "$2" && ! grep -v "^ *#" "$2" | grep -q "\${"' _ "$LLAMA_PINNED" "$STUB_STATE.out"
check "without --apply: it warns about the CRD and the ClusterRole for the whole cluster" bash -c 'grep -q "WHOLE cluster" "$1" && grep -q "models.kubeai.org" "$1"' _ "$STUB_STATE.out"
check "the printed values and Model are valid YAML" python3 -c '
import sys, yaml
text = open(sys.argv[1]).read().split("\n---\n")
docs = [d for part in text[1:3] for d in yaml.safe_load_all(part) if d]
assert docs[0]["modelServers"] == {"LlamaCpp": {"images": {"default": sys.argv[2], "cpu": sys.argv[2]}}} and docs[0]["open-webui"]["enabled"] is False, docs[0]
assert docs[1]["kind"] == "Model" and docs[1]["spec"]["engine"] == "LlamaCpp" and docs[1]["spec"]["resourceProfile"] == "cpu-burst:4", docs[1]
assert docs[1]["spec"]["args"][:6] == ["--hf-file", "granite-4.2-3b-Q4_K_M.gguf", "-c", "4096", "-t", "4"] and "env" not in docs[1]["spec"], docs[1]' "$STUB_STATE.out" "$LLAMA_PINNED"
# The network rules of the namespace of KubeAI: ingress only; every pod is selected (rule 1);
# the proxy only from the triage and orchestrator pods of the app (the orchestrator: the model of
# the Guard) and from its own namespace; the model pods only from the KubeAI pod.
cat >"$WORK/kubeai-policies.py" <<'POLICIES'
import sys, yaml
docs = []
for part in open(sys.argv[1]).read().split("\n---\n"):
    try:
        docs += [d for d in yaml.safe_load_all(part) if isinstance(d, dict)]
    except yaml.YAMLError:
        pass
policies = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "NetworkPolicy"}
assert sorted(policies) == ["wattson-kubeai-deny-ingress", "wattson-kubeai-models", "wattson-kubeai-proxy"], sorted(policies)
for policy in policies.values():
    assert policy["spec"]["policyTypes"] == ["Ingress"] and "egress" not in policy["spec"], policy
    assert policy["metadata"]["labels"]["app.kubernetes.io/part-of"] == "wattson" and "namespace" not in policy["metadata"], policy
kubeai_pod = {"app.kubernetes.io/name": "kubeai", "app.kubernetes.io/instance": "kubeai"}
assert policies["wattson-kubeai-deny-ingress"]["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}
proxy = policies["wattson-kubeai-proxy"]["spec"]
assert proxy["podSelector"] == {"matchLabels": kubeai_pod}, proxy
triage = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": sys.argv[2]}}, "podSelector": {"matchLabels": {"app": "wattson-triage"}}}
orchestrator = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": sys.argv[2]}}, "podSelector": {"matchLabels": {"app": "wattson-orchestrator"}}}
assert proxy["ingress"] == [
    {"from": [triage, orchestrator, {"podSelector": {}}], "ports": [{"protocol": "TCP", "port": 8000}]},
    {"from": [{"podSelector": {}}], "ports": [{"protocol": "TCP", "port": 8080}]},
], proxy["ingress"]
models = policies["wattson-kubeai-models"]["spec"]
assert models["podSelector"] == {"matchLabels": {"app": "model", "app.kubernetes.io/managed-by": "kubeai"}}, models
assert models["ingress"] == [{"from": [{"podSelector": {"matchLabels": kubeai_pod}}], "ports": [{"protocol": "TCP", "port": 8000}]}], models
POLICIES
check "without --apply: it prints the three network rules of KubeAI: ingress only, every pod, the proxy only from the triage and orchestrator pods" python3 "$WORK/kubeai-policies.py" "$STUB_STATE.out" test-ns
check "the network rules fit the deployed triage and orchestrator pods: their labels, and their way out to the proxy (pod labels, port)" python3 -c '
import json, sys, yaml
parts = open(sys.argv[1]).read().split("\n---\n")
docs = [d for part in parts if "kind: NetworkPolicy" in part for d in yaml.safe_load_all(part) if d]
proxy = next(d for d in docs if d["metadata"]["name"] == "wattson-kubeai-proxy")["spec"]
for number, name in enumerate(("wattson-triage", "wattson-orchestrator")):
    labels = json.load(open(sys.argv[2] + "/deployment__%s.json" % name))["doc"]["spec"]["template"]["metadata"]["labels"]
    wanted = proxy["ingress"][0]["from"][number]["podSelector"]["matchLabels"]
    assert all(labels.get(k) == v for k, v in wanted.items()), (wanted, labels)
    egress = json.load(open(sys.argv[2] + "/networkpolicy__%s.json" % name))["doc"]["spec"]["egress"]
    to_kubeai = [r for r in egress if any(t.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == "test-kubeai" for t in r.get("to", []))]
    assert len(to_kubeai) == 1, egress
    selector = to_kubeai[0]["to"][0]["podSelector"]["matchLabels"]
    assert all(proxy["podSelector"]["matchLabels"].get(k) == v for k, v in selector.items()), selector
    assert [p["port"] for p in to_kubeai[0]["ports"]] == [p["port"] for p in proxy["ingress"][0]["ports"]], to_kubeai' "$STUB_STATE.out" "$KUBEAI_STATE"
namespace_with
RUN_INPUT="$WORK/answer-no-context" with_kubeai_settings run kubeai.sh --apply
check "--apply without the typed context: nothing is installed" bash -c '[[ $1 -ne 0 ]] && grep -q "Nothing was installed" "$2" && ! grep -Eq "^(helm|apply)" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
check "--apply: before the question it only reads (the context, the CRD)" bash -c '! grep -vE "^(config view|get crd/)" "$1" | grep -q .' _ "$STUB_LOG"
namespace_with
RUN_ENV=("KUBEAI_CHART_SHA256=$STAND_IN_CHART_SHA")
RUN_INPUT="$WORK/answer-context" with_kubeai_settings run kubeai.sh --apply
RUN_ENV=()
check "--apply with the typed context: it ends well" test "$RUN_EXIT" -eq 0
check "it downloads the pinned chart" called "helm pull kubeai repo=https://www.kubeai.org version=0.24.0"
check "it installs it in the context and namespace of the settings, with the pinned llama.cpp image (no Ollama image)" bash -c '
  grep -qxF "helm upgrade --install kubeai chart=kubeai-0.24.0.tgz context=test-context namespace=test-kubeai create-namespace=True wait=True images=$1" "$2" &&
  ! grep -q AN-OLLAMA-IMAGE "$2"' _ "$LLAMA_PINNED" "$STUB_LOG"
check "it applies the Model in the namespace of KubeAI, with the label of the app, on the llama.cpp engine" bash -c '
  grep -qxF "apply model/granite-4-2-3b in test-kubeai" "$1" && python3 -c "
import json, sys
thing = json.load(open(sys.argv[1]))
spec = thing[\"doc\"][\"spec\"]
assert thing[\"label\"] == \"wattson\" and spec[\"engine\"] == \"LlamaCpp\" and spec[\"url\"] == \"hf://ibm-granite/granite-4.2-3b-GGUF\"" "$2/model__granite-4-2-3b.json"' _ "$STUB_LOG" "$STUB_STATE"
check "it applies the three network rules in the namespace of KubeAI, after the Model" python3 -c '
import sys
log = open(sys.argv[1]).read().splitlines()
model = log.index("apply model/granite-4-2-3b in test-kubeai")
for name in ("wattson-kubeai-deny-ingress", "wattson-kubeai-proxy", "wattson-kubeai-models"):
    assert log.index("apply networkpolicy/%s in test-kubeai" % name) > model, (name, log)
assert not any(line.startswith("apply networkpolicy/") and not line.endswith(" in test-kubeai") for line in log), log' "$STUB_LOG"
check "no Model was there: nothing is deleted" not grep -q '^delete ' "$STUB_LOG"
check "with 0 pods when idle it does not wait for a model pod" not grep -q '^wait for model/' "$STUB_LOG"
check "clean: context and namespaces" clean_run
sed 's/^KUBEAI_MIN_REPLICAS=.*/KUBEAI_MIN_REPLICAS=1/' "$KUBEAI_FLOW" >"$WORK/kubeai-flow-1.env"
namespace_with
RUN_ENV=("KUBEAI_CHART_SHA256=$STAND_IN_CHART_SHA")
KUBEAI_FLOW_FILE="$WORK/kubeai-flow-1.env" RUN_INPUT="$WORK/answer-context" with_kubeai_settings run kubeai.sh --apply
RUN_ENV=()
check "with KUBEAI_MIN_REPLICAS=1 it waits for the model pod" bash -c '[[ $1 -eq 0 ]] && grep -qxF "wait for model/granite-4-2-3b --timeout=900s in test-kubeai" "$2"' _ "$RUN_EXIT" "$STUB_LOG"
# The switch of 23 Oct 2026: the Model is there on Ollama. It is deleted before the chart, then
# made again on llama.cpp.
namespace_with crd/models.kubeai.org service/kubeai model/granite-4-2-3b=wattson
RUN_ENV=(STUB_MODEL_ENGINE=OLlama "KUBEAI_CHART_SHA256=$STAND_IN_CHART_SHA")
RUN_INPUT="$WORK/answer-context" with_kubeai_settings run kubeai.sh --apply
RUN_ENV=()
check "the Model on Ollama: it says so before the question" bash -c '
  [[ $1 -eq 0 ]] && python3 -c "
import sys
out = open(sys.argv[1]).read()
assert out.index(\"runs on OLlama now: it is deleted first\") < out.index(\"Type \")" "$2"' _ "$RUN_EXIT" "$STUB_STATE.out"
check "the Model on Ollama: it reads its engine, deletes it before helm, then applies the new one" python3 -c '
import sys
log = open(sys.argv[1]).read().splitlines()
get = log.index("get model/granite-4-2-3b in test-kubeai")
delete = log.index("delete model/granite-4-2-3b in test-kubeai")
helm = next(i for i, line in enumerate(log) if line.startswith("helm upgrade"))
apply = log.index("apply model/granite-4-2-3b in test-kubeai")
assert get < delete < helm < apply, log' "$STUB_LOG"
check "the Model on Ollama: clean (context and namespaces)" clean_run
namespace_with crd/models.kubeai.org service/kubeai model/granite-4-2-3b=wattson
RUN_ENV=(STUB_MODEL_ENGINE=LlamaCpp "KUBEAI_CHART_SHA256=$STAND_IN_CHART_SHA")
RUN_INPUT="$WORK/answer-context" with_kubeai_settings run kubeai.sh --apply
RUN_ENV=()
check "the Model already on llama.cpp: it is applied again, not deleted" bash -c '
  [[ $1 -eq 0 ]] && ! grep -q "^delete " "$2" && grep -qxF "apply model/granite-4-2-3b in test-kubeai" "$2"' _ "$RUN_EXIT" "$STUB_LOG"
check "with MODEL_BACKEND=kubeai it says that no deploy is needed" said "no deploy is needed"
# The Model of the install before the rename: the label part-of=myagent. It is kept (not deleted:
# its name is the same) and gets the new label.
namespace_with crd/models.kubeai.org service/kubeai model/granite-4-2-3b=myagent
RUN_ENV=(STUB_MODEL_ENGINE=LlamaCpp "KUBEAI_CHART_SHA256=$STAND_IN_CHART_SHA")
RUN_INPUT="$WORK/answer-context" with_kubeai_settings run kubeai.sh --apply
RUN_ENV=()
check "the Model with the old label part-of=myagent: not deleted, applied again with part-of=wattson" bash -c '
  [[ $1 -eq 0 ]] && ! grep -q "^delete " "$2" && grep -qxF "apply model/granite-4-2-3b in test-kubeai" "$2" &&
  python3 -c "import json, sys; assert json.load(open(sys.argv[1]))[\"label\"] == \"wattson\"" "$3/model__granite-4-2-3b.json"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE"
namespace_with
RUN_INPUT="$WORK/answer-context" with_kubeai_settings run kubeai.sh --apply
check "a chart file with another SHA-256 than the pinned one: refused before installing" bash -c '
  [[ $1 -ne 0 ]] && grep -q "not the pinned" "$2" && ! grep -q "^helm upgrade" "$3" && ! grep -q "^apply" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
namespace_with crd/models.kubeai.org
RUN_ENV=(STUB_CRD_OWNER=someone/kubeai "KUBEAI_CHART_SHA256=$STAND_IN_CHART_SHA")
RUN_INPUT="$WORK/answer-context" with_kubeai_settings run kubeai.sh --apply
RUN_ENV=()
check "another KubeAI owns the CRD: refused before the question, nothing installed" bash -c '
  [[ $1 -ne 0 ]] && grep -q "another KubeAI runs there" "$2" && ! grep -q "Type " "$2" && ! grep -Eq "^(helm|apply)" "$3"' _ "$RUN_EXIT" "$STUB_STATE.out" "$STUB_LOG"
show_output_on_failure "$before"

# ---- Cognee: k8s/cognee.sh and k8s/cognee/ ----------------------------------------------
# The memory for Claude Code and its web UI (docs/cognee.md), apart from Wattson: its own
# script, files and two images (k8s/cognee/ for the API server and cognee-mcp, k8s/cognee/ui/
# for the UI). The script runs against the same stand-ins, with its own docker stand-in (the
# registry has an image only after a push).
section "Cognee: the files (k8s/cognee/) and the settings"
check "k8s/cognee.sh is valid bash" bash -n k8s/cognee.sh
if command -v shellcheck >/dev/null 2>&1; then
  check "k8s/cognee.sh passes shellcheck" shellcheck -x k8s/cognee.sh
fi
for file in smoke.py fetch_models.py api_start.py; do
  check "k8s/cognee/$file is valid Python" python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "k8s/cognee/$file"
done
check "the settings of this project: a 10Gi volume, ports 8765 (MCP), 8766 (API), 8767 (UI) on the laptop, requests and limits of the form cpu/memory" bash -c '
  [[ "$(value_of settings.env COGNEE_SIZE)" == 10Gi && "$(value_of settings.env COGNEE_LOCAL_PORT)" == 8765 ]] &&
  [[ "$(value_of settings.env COGNEE_API_LOCAL_PORT)" == 8766 && "$(value_of settings.env COGNEE_UI_LOCAL_PORT)" == 8767 ]] || exit 1
  for name in COGNEE_REQUESTS COGNEE_LIMITS COGNEE_MCP_REQUESTS COGNEE_MCP_LIMITS COGNEE_UI_REQUESTS COGNEE_UI_LIMITS; do
    [[ "$(value_of settings.env "$name")" =~ ^[0-9]+m/[0-9]+(Mi|Gi)$ ]] || exit 1
  done'
check "settings.env says the CPU and memory of cognee are first guesses" grep -q 'FIRST GUESSES' settings.env
for bad in COGNEE_SIZE=10GB COGNEE_REQUESTS=1 COGNEE_LIMITS=4000m COGNEE_LOCAL_PORT=80 COGNEE_LOCAL_PORT=99999 'COGNEE_LOCAL_PORT=8765;id' \
  COGNEE_MCP_REQUESTS=50m COGNEE_MCP_LIMITS=1 COGNEE_UI_REQUESTS=20m/128 COGNEE_UI_LIMITS=500m \
  COGNEE_API_LOCAL_PORT=8765 COGNEE_UI_LOCAL_PORT=8766 COGNEE_UI_LOCAL_PORT=8765 COGNEE_API_LOCAL_PORT=8000 COGNEE_API_LOCAL_PORT=3000 \
  COGNEE_API_LOCAL_PORT=80 'COGNEE_UI_LOCAL_PORT=8767;id'; do
  check "cognee.sh refuses $bad" bash -c '! SETTINGS_FILE="$1" bash k8s/cognee.sh >/dev/null 2>&1' _ "$(settings "$bad")"
done
check "cognee.sh fills in exactly the names that cognee.yaml uses" bash -c '
  used="$(grep -v "^ *#" k8s/cognee/cognee.yaml | grep -oE "\\\$\{[A-Z0-9_]+\}" | sort -u)"
  filled="$(grep "^COGNEE_YAML_NAMES=" k8s/cognee.sh | grep -oE "\\\$\{[A-Z0-9_]+\}" | sort -u)"
  [[ -n "$used" && "$used" == "$filled" ]]'
check "cognee.yaml has no Ingress, no Secret, no NodePort or LoadBalancer, no host port or host network" bash -c '! grep -v "^ *#" k8s/cognee/cognee.yaml | grep -Eq "^kind: (Ingress|Secret)|NodePort|LoadBalancer|hostPort|hostNetwork"'
# deploy.sh and wattson.yaml name cognee only for "seen before" (the knowledge agent's settings
# and way out to the pod, COGNEE_RECALL); the manifest checks above see no object of the pod.
check "remove.sh does not touch cognee, deploy.sh never runs cognee.sh or reads its files" bash -c '
  ! grep -qi cognee k8s/remove.sh && ! grep -v "^ *#" k8s/deploy.sh | grep -qE "cognee\.sh|cognee/|cognee\.yaml"'
check "wattson.yaml names cognee only in comments, the COGNEE_RECALL of the knowledge agent and the \"cognee\" blocks" python3 -c '
import re
inside = False
for number, line in enumerate(open("k8s/wattson.yaml"), 1):
    mark = re.match(r"^ *# (begin|end): (\S+)", line)
    if mark and mark.group(2) == "cognee":
        inside = mark.group(1) == "begin"
    elif re.search("cognee", line, re.I) and not inside and not line.lstrip().startswith("#"):
        assert "name: COGNEE_RECALL, value:" in line, number'
check "the Dockerfile of cognee: every image pinned by digest, never latest" python3 -I -c '
import re, sys
text = open("k8s/cognee/Dockerfile").read()
images = re.findall(r"^ARG [A-Z_]+_IMAGE=(\S+)$", text, re.M)
assert len(images) == 2 and all(re.search(r":[0-9][^@]*@sha256:[0-9a-f]{64}$", i) for i in images), images
froms = re.findall(r"^FROM (\S+)", text, re.M)
assert froms and all(f.startswith("${") and f.endswith("_IMAGE}") or f in ("build",) for f in froms), froms
assert ":latest" not in text'
check "the Dockerfile of cognee: every download (ADD) has a SHA-256, at one commit" python3 -I -c '
import re
text = open("k8s/cognee/Dockerfile").read()
adds = [l for l in text.splitlines() if l.startswith("ADD ")]
assert len(adds) == 8 and all(re.search(r"--checksum=sha256:[0-9a-f]{64} ", l) for l in adds), adds
assert re.search(r"^ARG MCP_SRC=https://raw\.githubusercontent\.com/topoteretes/cognee/[0-9a-f]{40}$", text, re.M)'
check "the Dockerfile of cognee: user 10001, the version of the tag is the version of the lock (cognee 1.6.5)" python3 -I -c '
import re
text = open("k8s/cognee/Dockerfile").read()
version = re.search(r"^ARG COGNEE_VERSION=([0-9.]+)$", text, re.M).group(1)
lock = open("k8s/cognee/uv.lock").read()
assert re.search(r"^USER 10001:10001$", text, re.M)
assert "cognee[gliner]==%s" % version in open("k8s/cognee/pyproject.toml").read()
assert re.search(r"\[\[package\]\]\nname = \"cognee\"\nversion = \"%s\"" % re.escape(version), lock)
assert "HF_HUB_OFFLINE=1" in text and "TELEMETRY_DISABLED=1" in text'
check "the lock of cognee has none of the extras it does not use (posthog, neo4j, postgres, unstructured)" not grep -Eq '^name = "(posthog|neo4j|psycopg2|psycopg2-binary|unstructured)"$' k8s/cognee/uv.lock
check "every model file is pinned: repo at a commit, size, SHA-256" python3 -I -c '
import re
lines = [l for l in open("k8s/cognee/models.txt").read().splitlines() if l.strip() and not l.startswith("#")]
assert len(lines) >= 10
for line in lines:
    assert re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+ [0-9a-f]{40} [A-Za-z0-9._/-]+ [0-9a-f]{64} [0-9]+", line), line'
check "the build of cognee gets only its own files (.dockerignore)" bash -c '[[ "$(grep -v "^#" k8s/cognee/.dockerignore | head -n 1)" == "*" ]]'
check "the Dockerfile of cognee: the start file of the API server in a small layer after USER, then import checks as user 10001 with no network" python3 -I -c '
text = open("k8s/cognee/Dockerfile").read()
last = text[text.rindex("\nFROM "):]
user = last.index("\nUSER 10001:10001\n")
copy = last.index("\nCOPY --chmod=755 api_start.py /app/wattson/api_start.py\n")
check = last.index("\nRUN --network=none ")
assert user < copy < check, (user, copy, check)
assert "import cognee.api.client, src.server, api_start" in last[check:] and "from src import main_mcp" in last[check:]
assert "!api_start.py" in open("k8s/cognee/.dockerignore").read().split()'
check "the Dockerfile of the UI: Node pinned by digest, never latest, the same cognee version" python3 -I -c '
import re
text = open("k8s/cognee/ui/Dockerfile").read()
images = re.findall(r"^ARG [A-Z_]+_IMAGE=(\S+)$", text, re.M)
assert images and all(re.fullmatch(r"node:[0-9][^@]*@sha256:[0-9a-f]{64}", i) for i in images), images
froms = re.findall(r"^FROM (\S+)", text, re.M)
assert len(froms) == 4 and all(f == "${NODE_IMAGE}" for f in froms), froms
assert ":latest" not in text
version = re.search(r"^ARG COGNEE_VERSION=([0-9.]+)$", text, re.M).group(1)
assert version == re.search(r"^ARG COGNEE_VERSION=([0-9.]+)$", open("k8s/cognee/Dockerfile").read(), re.M).group(1), version'
check "the Dockerfile of the UI: one download, the archive of one commit, with its SHA-256; npm ci of the lock with no scripts" python3 -I -c '
import re
text = open("k8s/cognee/ui/Dockerfile").read()
adds = [l for l in text.splitlines() if l.startswith("ADD ")]
assert len(adds) == 1 and re.fullmatch(r"ADD --checksum=sha256:[0-9a-f]{64} https://github\.com/topoteretes/cognee/archive/\$\{COGNEE_COMMIT\}\.tar\.gz /cognee\.tar\.gz", adds[0]), adds
assert re.search(r"^ARG COGNEE_COMMIT=[0-9a-f]{40}$", text, re.M)
runs = " ".join(re.findall(r"^RUN .*$", text, re.M))
assert "npm ci --ignore-scripts " in runs and "npm install" not in text and "npm i " not in text and "curl" not in runs and "wget" not in runs'
check "the Dockerfile of the UI: telemetry off, the local UI, standalone, user 10001, every file readable for it, a check as that user with no network" python3 -I -c '
import re
text = open("k8s/cognee/ui/Dockerfile").read()
last = text[text.rindex("\nFROM "):]
assert text.count("NEXT_TELEMETRY_DISABLED=1") == 2 and "NEXT_PUBLIC_IS_CLOUD_ENVIRONMENT=false" in text
assert "COPY --from=build /app/.next/standalone ./" in last and "/usr/local/bin/npm" in last
assert re.findall(r"^USER (\S+)$", text, re.M) == ["10001:10001"]
user = last.index("\nUSER 10001:10001\n")
assert last.index("! -perm -a+rx") < user < last.index("\nRUN --network=none node -e ")
assert last.rstrip().endswith("CMD [\"node\", \"server.js\"]")'
check "the build of the UI gets no file of this repository (.dockerignore)" bash -c '[[ "$(grep -v "^#" k8s/cognee/ui/.dockerignore)" == "*" ]]'

# A docker for the cognee runs: the registry has an image only once it was pushed.
mkdir -p "$WORK/cognee-bin"
cat >"$WORK/cognee-bin/docker" <<'STUB'
#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
pushed = os.path.join(os.environ["STUB_STATE"], "cognee-pushed")
def option(name):
    return args[args.index(name) + 1] if name in args else "-"
with open(os.environ["STUB_LOG"], "a") as log:
    if args[0] == "build":
        log.write("docker build tag=%s platform=%s network=%s file=%s from=%s\n" % (
            option("-t"), option("--platform"), option("--network"),
            "/".join(option("-f").split("/")[-2:]), os.path.basename(args[-1])))
    elif args[0] == "push":
        log.write("docker push %s\n" % args[-1])
        open(pushed, "a").write(args[-1] + "\n")
    elif args[:3] == ["buildx", "imagetools", "inspect"]:
        log.write("docker digest %s\n" % args[-1])
        if os.path.exists(pushed) and args[-1] in open(pushed).read().split():
            sys.stdout.write("sha256:" + "ab" * 32)
        else:
            sys.stderr.write("ERROR: %s: not found\n" % args[-1])
            sys.exit(1)
    else:
        log.write("A-DOCKER-CALL-THE-STAND-IN-DOES-NOT-KNOW: %s\n" % " ".join(args))
        sys.exit(1)
STUB
chmod +x "$WORK/cognee-bin/docker"
COGNEE_ENV=(PATH="$WORK/cognee-bin:$WORK/bin:$PATH")
COGNEE_TAG="$(sed -n 's/^ARG COGNEE_VERSION=//p' "$REPO/k8s/cognee/Dockerfile")-$(cd "$REPO/k8s/cognee" && sha256sum Dockerfile .dockerignore pyproject.toml uv.lock models.txt fetch_models.py api_start.py | sha256sum | cut -c1-12)"
COGNEE_REF="registry.example.test/p/wattson-cognee:$COGNEE_TAG"
COGNEE_UI_TAG="$(sed -n 's/^ARG COGNEE_VERSION=//p' "$REPO/k8s/cognee/ui/Dockerfile")-$(cd "$REPO/k8s/cognee/ui" && sha256sum Dockerfile .dockerignore | sha256sum | cut -c1-12)"
COGNEE_UI_REF="registry.example.test/p/wattson-cognee-ui:$COGNEE_UI_TAG"
printf 'test-context\n' >"$WORK/answer-context"
RUN_ENV=("${COGNEE_ENV[@]}")

section "cognee.sh: the plan (no option) runs nothing"
before=$FAILED
namespace_with
run cognee.sh
check "it ends well" test "$RUN_EXIT" -eq 0
check "it calls neither kubectl nor docker" not test -s "$STUB_LOG"
check "it shows both images with their tags (version and hash of the files) and says nothing was run" bash -c '
  grep -qF "$1" "$3" && grep -qF "$2" "$3" && grep -q "Nothing was run" "$3"' _ "$COGNEE_REF" "$COGNEE_UI_REF" "$STUB_STATE.out"
show_output_on_failure "$before"

section "cognee.sh --apply: the first time (build, push, apply, wait)"
before=$FAILED
namespace_with
RUN_INPUT="$WORK/answer-no" run cognee.sh --apply
check "without the typed context: nothing is built or applied" bash -c '
  [[ $1 -ne 0 ]] && ! grep -Eq "^(apply|docker build|docker push)" "$2" && grep -q "Nothing was changed." "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
: >"$STUB_LOG"
RUN_ENV=("${COGNEE_ENV[@]}" BUILD_NETWORK=host)
RUN_INPUT="$WORK/answer-context" run cognee.sh --apply
RUN_ENV=("${COGNEE_ENV[@]}")
check "with the typed context: it ends well" test "$RUN_EXIT" -eq 0
check "it builds from k8s/cognee with its own Dockerfile, for linux/amd64, with BUILD_NETWORK" \
  called "docker build tag=$COGNEE_REF platform=linux/amd64 network=host file=cognee/Dockerfile from=cognee"
check "it builds the UI from k8s/cognee/ui with its own Dockerfile, for linux/amd64, with BUILD_NETWORK" \
  called "docker build tag=$COGNEE_UI_REF platform=linux/amd64 network=host file=ui/Dockerfile from=ui"
check "it pushes both images and reads their digests" bash -c '
  for ref in "$1" "$2"; do grep -qxF "docker push $ref" "$3" && grep -qxF "docker digest $ref" "$3" || exit 1; done' _ "$COGNEE_REF" "$COGNEE_UI_REF" "$STUB_LOG"
check "it makes the Secret once, before the apply: the UI password, the API key of the MCP container, the three token keys" python3 -I - "$STUB_STATE" "$STUB_LOG" <<'PY'
import json, re, sys
state, log = sys.argv[1:]
lines = open(log).read().splitlines()
assert lines.count("create secret/wattson-cognee") == 1, lines
assert lines.index("create secret/wattson-cognee") < min(i for i, l in enumerate(lines) if l.startswith("apply ") and "/" in l)
thing = json.load(open(state + "/secret__wattson-cognee.json"))
values, doc = thing["values"], thing["doc"]
assert thing["label"] == "wattson" and doc["metadata"]["labels"]["app.kubernetes.io/component"] == "cognee"
assert sorted(values) == ["COGNEE_MCP_API_KEY", "DEFAULT_USER_PASSWORD", "FASTAPI_USERS_JWT_SECRET",
                          "FASTAPI_USERS_RESET_PASSWORD_TOKEN_SECRET", "FASTAPI_USERS_VERIFICATION_TOKEN_SECRET"], sorted(values)
assert re.fullmatch(r"[0-9a-f]{64}", values["COGNEE_MCP_API_KEY"])
assert all(len(v) >= 43 for v in values.values()) and len(set(values.values())) == 5
PY
check "the UI password is in the CLI folder, mode 600, the one of the Secret" python3 -I - "$STUB_STATE" "$CLI_DIR/cognee-ui-password" <<'PY'
import json, os, stat, sys
state, path = sys.argv[1:]
assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
assert open(path).read() == json.load(open(state + "/secret__wattson-cognee.json"))["values"]["DEFAULT_USER_PASSWORD"]
PY
for object in serviceaccount/wattson-cognee persistentvolumeclaim/wattson-cognee-data deployment/wattson-cognee service/wattson-cognee networkpolicy/wattson-cognee; do
  check "it applies $object" called "apply $object"
done
check "it applies only these five" bash -c '[[ "$(grep -c "^apply [a-z]*/" "$1")" -eq 5 ]]' _ "$STUB_LOG"
check "it waits for the pod, 15 minutes" called "wait for deployment/wattson-cognee --timeout=900s"
check "the checks come before the question, the build after it (then only the UI password is read, from the new Secret)" bash -c '
  first_build="$(grep -n "^docker build" "$1" | head -n 1 | cut -d: -f1)"
  [[ -n "$first_build" ]] && head -n "$first_build" "$1" | grep -qxF "get namespace/test-ns" &&
  ! tail -n +"$first_build" "$1" | grep "^get " | grep -vqxF "get secret/wattson-cognee"' _ "$STUB_LOG"
check "it prints the command that connects Claude Code (the same as before the UI)" said "claude mcp add --transport http --scope user cognee http://127.0.0.1:8765/mcp"
check "it prints the address of the UI and where its password is" bash -c '
  grep -qF "http://127.0.0.1:8767" "$1" && grep -qF "$2" "$1"' _ "$STUB_STATE.out" "$CLI_DIR/cognee-ui-password"
check "clean: context and namespace" clean_run
check "the applied files: one pod, Recreate, three containers (api, mcp, ui), the pushed images by digest, locked, with limits and probes" python3 -I - "$STUB_STATE" "$COGNEE_REF" "$COGNEE_UI_REF" <<'PY'
import json, sys
state, ref, ui_ref = sys.argv[1:]
deploy = json.load(open(state + "/deployment__wattson-cognee.json"))["doc"]
spec = deploy["spec"]
assert spec["replicas"] == 1 and spec["strategy"] == {"type": "Recreate"}, spec
pod = spec["template"]["spec"]
assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsNonRoot"] is True
assert pod["imagePullSecrets"] == [{"name": "my-pull-secret"}]
assert not pod.get("hostNetwork") and not pod.get("initContainers")
boxes = {box["name"]: box for box in pod["containers"]}
assert list(boxes) == ["api", "mcp", "ui"], list(boxes)
digest = "@sha256:" + "ab" * 32
assert boxes["api"]["image"] == boxes["mcp"]["image"] == ref + digest, boxes["api"]["image"]
assert boxes["ui"]["image"] == ui_ref + digest, boxes["ui"]["image"]
assert boxes["api"]["command"] == ["/app/.venv/bin/python", "/app/wattson/api_start.py"]
assert boxes["mcp"]["command"] == ["cognee-mcp"] and "--no-migration" in boxes["mcp"]["args"]
assert boxes["ui"]["command"] == ["node", "server.js"]
for name, box in boxes.items():
    sec = box["securityContext"]
    assert sec["readOnlyRootFilesystem"] and not sec["allowPrivilegeEscalation"] and sec["capabilities"] == {"drop": ["ALL"]}, name
    assert set(box["resources"]) == {"requests", "limits"}, name
    assert box["resources"]["limits"]["memory"] and box["resources"]["limits"]["cpu"], name
    assert all(probe in box for probe in ("startupProbe", "readinessProbe", "livenessProbe")), name
    assert not any("hostPort" in port for port in box["ports"]) and "envFrom" not in box, name
ports = {name: [port["containerPort"] for port in box["ports"]] for name, box in boxes.items()}
assert ports == {"api": [8766], "mcp": [8000], "ui": [3000]}, ports
for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
    assert boxes["api"][probe]["httpGet"]["path"] == "/health", probe
    get = boxes["mcp"][probe]["httpGet"]
    assert get["path"] == "/health" and get["httpHeaders"] == [{"name": "Host", "value": "localhost:8000"}], probe
    assert boxes["ui"][probe]["httpGet"]["path"] == "/api/runtime-config", probe
assert boxes["api"]["startupProbe"]["periodSeconds"] * boxes["api"]["startupProbe"]["failureThreshold"] >= 600
# Only the API server has the volume of the memory: it is the only process that opens the stores.
mounts = {name: {m["mountPath"]: m["name"] for m in box["volumeMounts"]} for name, box in boxes.items()}
assert mounts == {"api": {"/data": "data", "/tmp": "api-tmp"}, "mcp": {"/tmp": "mcp-tmp"},
                  "ui": {"/tmp": "ui-tmp", "/app/.next/cache": "ui-cache"}}, mounts
volumes = {volume["name"]: volume for volume in pod["volumes"]}
assert volumes["data"]["persistentVolumeClaim"] == {"claimName": "wattson-cognee-data"}
assert all(set(volume) == {"name", "emptyDir"} for name, volume in volumes.items() if name != "data"), volumes
# The Secret: the API server gets its five keys, the MCP container only its API key, the UI nothing
# (its server logs in with DEFAULT_USER_PASSWORD for a visitor who sends no login).
refs = {name: sorted((e["name"], e["valueFrom"]["secretKeyRef"]["name"], e["valueFrom"]["secretKeyRef"]["key"])
                     for e in box["env"] if "valueFrom" in e) for name, box in boxes.items()}
keys = ("COGNEE_MCP_API_KEY", "DEFAULT_USER_PASSWORD", "FASTAPI_USERS_JWT_SECRET",
        "FASTAPI_USERS_RESET_PASSWORD_TOKEN_SECRET", "FASTAPI_USERS_VERIFICATION_TOKEN_SECRET")
assert refs["api"] == [(key, "wattson-cognee", key) for key in keys], refs["api"]
assert refs["mcp"] == [("COGNEE_API_KEY", "wattson-cognee", "COGNEE_MCP_API_KEY")], refs["mcp"]
assert refs["ui"] == [], refs["ui"]
PY
check "the applied files: no LLM, local models, nothing phones home, no URL fetch; MCP calls the API server in the pod" python3 -I - "$STUB_STATE" <<'PY'
import json, re, sys
deploy = json.load(open(sys.argv[1] + "/deployment__wattson-cognee.json"))["doc"]
boxes = {box["name"]: {e["name"]: e.get("value") for e in box["env"]} for box in deploy["spec"]["template"]["spec"]["containers"]}
want = {
    "api": {"LLM_API_KEY": "", "GRAPH_EXTRACTOR": "gliner_demo", "EMBEDDING_PROVIDER": "fastembed", "TELEMETRY_DISABLED": "1",
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "ALLOW_HTTP_REQUESTS": "false", "COGNEE_TRACING_ENABLED": "false",
            "ENOLA_NO_UPDATE_CHECK": "1", "COGNEE_ALLOWED_LOCAL_FILE_ROOTS": "/tmp:/data/code", "OMP_NUM_THREADS": "4",
            "SYSTEM_ROOT_DIRECTORY": "/data/cognee/system", "DATA_ROOT_DIRECTORY": "/data/cognee/data", "HTTP_API_PORT": "8766",
            "ENABLE_BACKEND_ACCESS_CONTROL": "true", "REQUIRE_AUTHENTICATION": "true", "CORS_ALLOWED_ORIGINS": "http://127.0.0.1:8767"},
    "mcp": {"COGNEE_BASE_URL": "http://127.0.0.1:8766", "COGNEE_API_AUTH_SCHEME": "x-api-key", "LLM_API_KEY": "",
            "TELEMETRY_DISABLED": "1", "ALLOW_HTTP_REQUESTS": "false", "MCP_CORS_ALLOW_ORIGINS": "", "FASTMCP_CHECK_FOR_UPDATES": "off",
            "COGNEE_TRACING_ENABLED": "false"},
    "ui": {"COGNEE_BACKEND_URL": "http://127.0.0.1:8766", "NEXT_TELEMETRY_DISABLED": "1", "PORT": "3000", "NODE_ENV": "production"},
}
for name, values in want.items():
    assert {k: boxes[name].get(k) for k in values} == values, (name, {k: boxes[name].get(k) for k in values})
for name, env in boxes.items():
    assert not any(k in env for k in ("LLM_ENDPOINT", "OPENAI_API_KEY", "LLM_PROVIDER", "EMBEDDING_ENDPOINT", "EMBEDDING_API_KEY",
                                      "COGNEE_SERVICE_URL", "NEXT_PUBLIC_LOCAL_API_URL")), (name, env)
    # The only addresses: the pod itself, and the UI's address in the browser (CORS).
    assert all(not v or "://" not in v or re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", v) for v in env.values()), (name, env)
PY
check "the applied files: the volume (10Gi on my-storage-class), a ClusterIP Service, the network rule (in: orchestrator and knowledge; out: DNS only)" python3 -I - "$STUB_STATE" <<'PY'
import json, sys
state = sys.argv[1]
doc = lambda kind, name: json.load(open("%s/%s__%s.json" % (state, kind, name)))["doc"]
pvc = doc("persistentvolumeclaim", "wattson-cognee-data")["spec"]
assert pvc["storageClassName"] == "my-storage-class" and pvc["resources"]["requests"]["storage"] == "10Gi" and pvc["accessModes"] == ["ReadWriteOnce"]
svc = doc("service", "wattson-cognee")["spec"]
assert svc["type"] == "ClusterIP" and [p["port"] for p in svc["ports"]] == [8000]
rule = doc("networkpolicy", "wattson-cognee")["spec"]
assert rule["podSelector"] == {"matchLabels": {"app": "wattson-cognee"}} and rule["policyTypes"] == ["Ingress", "Egress"]
[ingress] = rule["ingress"]
assert [f["podSelector"]["matchLabels"]["app"] for f in ingress["from"]] == ["wattson-orchestrator", "wattson-knowledge"]
assert all(set(f) == {"podSelector"} for f in ingress["from"]) and ingress["ports"] == [{"protocol": "TCP", "port": 8000}]
[egress] = rule["egress"]
assert sorted((p["protocol"], p["port"]) for p in egress["ports"]) == [("TCP", 53), ("UDP", 53)]
assert egress["to"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                         "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}},
                        {"ipBlock": {"cidr": "169.254.25.10/32"}}], egress["to"]
for name in ("serviceaccount__wattson-cognee", "persistentvolumeclaim__wattson-cognee-data", "deployment__wattson-cognee",
             "service__wattson-cognee", "networkpolicy__wattson-cognee"):
    labels = json.load(open("%s/%s.json" % (state, name)))["doc"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/part-of"] == "wattson" and labels["app.kubernetes.io/component"] == "cognee", name
PY
show_output_on_failure "$before"
COGNEE_STATE="$STUB_STATE" COGNEE_LOG="$STUB_LOG" COGNEE_CLI="$CLI_DIR"

section "cognee.sh --apply: a second time (the image is in the registry, the volume is there)"
before=$FAILED
: >"$STUB_LOG"
RUN_INPUT="$WORK/answer-context" run cognee.sh --apply
check "it ends well" test "$RUN_EXIT" -eq 0
check "it builds nothing and pushes nothing" not grep -Eq '^docker (build|push)' "$STUB_LOG"
check "it says the memory is kept" said "is there already: the memory is kept"
check "the Secret is kept, not made again (the UI password and the MCP's API key stay)" bash -c '
  ! grep -q "^create secret" "$1" && grep -q "Secret: wattson-cognee is there already: kept" "$2"' _ "$STUB_LOG" "$STUB_STATE.out"
check "clean: context and namespace, no value of the Secret on screen or on a command line" clean_run
show_output_on_failure "$before"

section "cognee.sh: what it refuses"
before=$FAILED
namespace_with deployment/wattson-cognee=someone-else
RUN_INPUT="$WORK/answer-context" run cognee.sh --apply
check "an object with its name that it did not make: nothing is built or applied" bash -c '
  [[ $1 -ne 0 ]] && ! grep -Eq "^(apply|docker build|docker push)" "$2"' _ "$RUN_EXIT" "$STUB_LOG"
namespace_with
RUN_INPUT="$WORK/answer-context" RUN_DOCKER_CONFIG="$WORK/docker-out" run cognee.sh --apply
check "docker not logged in to the registry: nothing is built or applied" bash -c '
  [[ $1 -ne 0 ]] && ! grep -Eq "^(apply|docker build)" "$2" && grep -q "docker login registry.example.test" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
run cognee.sh --smoke
check "--smoke when cognee is not there: it says so, and starts no port-forward" bash -c '
  [[ $1 -ne 0 ]] && ! grep -q "^port-forward" "$2" && grep -q "is not running and ready" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
run cognee.sh --port-forward
check "--port-forward when cognee is not there: it says so, and starts no port-forward" bash -c '
  [[ $1 -ne 0 ]] && ! grep -q "^port-forward" "$2" && grep -q "is not running and ready" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
namespace_with secret/wattson-cognee=wattson:DEFAULT_USER_PASSWORD
RUN_INPUT="$WORK/answer-context" run cognee.sh --apply
check "a Secret of cognee without all its keys: refused, nothing is built, made or applied" bash -c '
  [[ $1 -ne 0 ]] && ! grep -Eq "^(apply|create|docker build|docker push)" "$2" && grep -q "The Secret wattson-cognee has no .*COGNEE_MCP_API_KEY" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
namespace_with secret/wattson-cognee=someone-else:DEFAULT_USER_PASSWORD
RUN_INPUT="$WORK/answer-context" run cognee.sh --apply
check "a Secret with its name that it did not make: nothing is built, made or applied" bash -c '
  [[ $1 -ne 0 ]] && ! grep -Eq "^(apply|create|docker build|docker push)" "$2"' _ "$RUN_EXIT" "$STUB_LOG"
run cognee.sh --with-data
check "--with-data without --delete is refused" test "$RUN_EXIT" -ne 0
show_output_on_failure "$before"

section "cognee.sh --delete"
before=$FAILED
STUB_STATE="$COGNEE_STATE" STUB_LOG="$COGNEE_LOG" CLI_DIR="$COGNEE_CLI"
cp -r "$STUB_STATE" "$WORK/cognee-after-apply"
: >"$STUB_LOG"
RUN_INPUT="$WORK/answer-no" run cognee.sh --delete
check "without the typed context: nothing is removed" bash -c '[[ $1 -ne 0 ]] && ! grep -q "^delete" "$2"' _ "$RUN_EXIT" "$STUB_LOG"
RUN_INPUT="$WORK/answer-context" run cognee.sh --delete
check "with the typed context: it ends well" test "$RUN_EXIT" -eq 0
for object in deployment/wattson-cognee service/wattson-cognee networkpolicy/wattson-cognee serviceaccount/wattson-cognee; do
  check "it removes $object" called "delete $object"
done
check "the volume (the memory) and the Secret (its login) are kept" bash -c '
  ! grep -Eq "^delete (persistentvolumeclaim|secret)" "$1" && test -f "$2/persistentvolumeclaim__wattson-cognee-data.json" && test -f "$2/secret__wattson-cognee.json"' _ "$STUB_LOG" "$STUB_STATE"
check "the copy of the UI password on the laptop is kept" test -s "$CLI_DIR/cognee-ui-password"
check "clean: context and namespace" clean_run
rm -rf "$STUB_STATE" && cp -r "$WORK/cognee-after-apply" "$STUB_STATE"
: >"$STUB_LOG"
RUN_INPUT="$WORK/answer-context" run cognee.sh --delete --with-data
check "--delete --with-data: the volume and the Secret go too, after a warning" bash -c '
  [[ $1 -eq 0 ]] && grep -qxF "delete persistentvolumeclaim/wattson-cognee-data" "$2" && grep -qxF "delete secret/wattson-cognee" "$2" && grep -q "WITH THE VOLUME" "$3"' _ "$RUN_EXIT" "$STUB_LOG" "$STUB_STATE.out"
check "--delete --with-data: the copy of the UI password on the laptop goes too" not test -e "$CLI_DIR/cognee-ui-password"
namespace_with service/wattson-cognee=someone-else deployment/wattson-cognee=wattson
RUN_INPUT="$WORK/answer-context" run cognee.sh --delete
check "an object with its name that it did not make is left alone" bash -c '
  [[ $1 -eq 0 ]] && ! grep -q "^delete service/wattson-cognee" "$2" && grep -q "^delete deployment/wattson-cognee" "$2"' _ "$RUN_EXIT" "$STUB_LOG"
show_output_on_failure "$before"
RUN_ENV=()

# ---- the real image, built once (Docker; no push) -------------------------------------
section "The image (docker build, no push), the model download script, and Tempo"
docker_ok=no
if [[ "${SKIP_DOCKER:-}" == 1 ]]; then
  skip "SKIP_DOCKER=1"
elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  skip "Docker is not there or not running"
else
  docker_ok=yes
fi

if [[ "$docker_ok" == yes ]]; then
  TEST_TAG="wattson-k8s-test:local"
  built=no
  if docker build -q --platform linux/amd64 -t "$TEST_TAG" . >"$WORK/build.log" 2>&1; then
    built=yes
  elif docker build -q --platform linux/amd64 --network host -t "$TEST_TAG" . >"$WORK/build-host.log" 2>&1; then
    # On some networks a build cannot look up names: the build then uses the host's network.
    built=host
  fi
  if [[ "$built" == no ]]; then
    if grep -qiE 'temporary failure|name resolution|could not resolve|connection|timed out|network' "$WORK/build.log" "$WORK/build-host.log" 2>/dev/null; then
      skip "the image could not be built: no network ($(grep -m1 -iE 'error' "$WORK/build-host.log" | cut -c1-120))"
    else
      fail "the image builds"
      tail -n 15 "$WORK/build-host.log" | sed 's/^/         | /'
    fi
  else
    if [[ "$built" == host ]]; then ok "the image builds (with --network host)"; else ok "the image builds"; fi
    in_image() {  # COMMAND... -> runs in the image the way the pods run it
      docker run --rm --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --network none "$TEST_TAG" "$@"
    }
    check "it runs as user 10001" test "$(in_image python -c 'import os; print(os.getuid(), os.getgid())')" = "10001 10001"
    check "it runs with a read-only root and writes no bytecode" in_image python -c 'import sys; assert sys.dont_write_bytecode'
    check "HOME and TMPDIR are /tmp" test "$(in_image python -c 'import os; print(os.environ["HOME"], os.environ["TMPDIR"])')" = "/tmp /tmp"
    check "the shared modules import inside it" in_image python -c 'import wattson.shared.contracts, wattson.shared.auth, wattson.shared.planner'
    check "Strands with A2A imports inside it" in_image python -c 'from strands.multiagent.a2a import A2AServer'
    check "it has the three programs" in_image sh -c 'command -v wattson-orchestrator && command -v wattson-triage && command -v wattson-knowledge'
    check "it has no uv and no pip" in_image python -c 'import shutil; assert not shutil.which("uv") and not shutil.which("pip") and not shutil.which("pip3")'
    check "wattson is installed as a package, not as a link to the source" in_image python -c '
import wattson, sys
assert wattson.__file__.startswith("/app/.venv/lib/python3.13/site-packages/wattson/"), wattson.__file__'
    # The app module of each agent, when its part is written already.
    for agent in orchestrator triage knowledge; do
      if [[ -f "src/wattson/agents/$agent/app.py" ]]; then
        check "wattson.agents.$agent.app imports inside it" in_image python -c "import wattson.agents.$agent.app as app; assert callable(app.main)"
      else
        skip "wattson.agents.$agent.app is not written yet"
      fi
    done
    # Each agent, started from the image the way its pod starts it: the command and the
    # settings of the filled-in manifests (made-up tokens), read-only root, user 10001, no
    # network. Its own startup probe must pass, and no token may show in its log.
    cat >"$WORK/smoke.py" <<'PY'
import secrets, subprocess, sys, time
import yaml

rendered, image, src = sys.argv[1:]
docs = [d for d in yaml.safe_load_all(open(rendered)) if d]
tokens = {k: secrets.token_urlsafe(32) for k in ("WATTSON_TOKEN", "TRIAGE_TOKEN", "KNOWLEDGE_TOKEN", "SHIELD_TOKEN")}
for cname in ("orchestrator", "triage", "knowledge"):
    if not __import__("os").path.exists(f"{src}/agents/{cname}/app.py"):
        print(f"skip|{cname}: its app is not written yet")
        continue
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-" + cname))
    c = next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == cname)
    envs = []
    for e in c["env"]:
        value = tokens[e["valueFrom"]["secretKeyRef"]["key"]] if "valueFrom" in e else e["value"]
        envs += ["-e", f"{e['name']}={value}"]
    mounts = ["--tmpfs", "/tmp"]
    if any(m["mountPath"] == "/data" for m in c["volumeMounts"]):
        mounts += ["--tmpfs", "/data:uid=10001,gid=10001"]
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "--user", "10001:10001", *mounts, *envs, image, *c["command"]],
        capture_output=True, text=True)
    if started.returncode != 0:
        print(f"FAIL|{cname}: starts from the image ({started.stderr.strip()[-200:]})")
        continue
    cid = started.stdout.strip()
    probe = c["startupProbe"]
    if "exec" in probe:
        command = probe["exec"]["command"]
    else:
        port = next(p["containerPort"] for p in c["ports"] if p["name"] == probe["httpGet"]["port"])
        command = ["/app/.venv/bin/python", "-c",
                   f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}{probe['httpGet']['path']}', timeout=3)"]
    up = False
    for _ in range(60):
        if subprocess.run(["docker", "exec", cid, *command], capture_output=True).returncode == 0:
            up = True
            break
        time.sleep(0.5)
    logs = subprocess.run(["docker", "logs", cid], capture_output=True, text=True)
    text = logs.stdout + logs.stderr
    subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    print(("ok|" if up else "FAIL|") + f"{cname}: starts from the image with the settings of its manifest, and its startup probe passes"
          + ("" if up else " (" + text.strip()[-300:].replace("\n", " / ") + ")"))
    print(("FAIL|" if any(t in text for t in tokens.values()) else "ok|") + f"{cname}: no token in its log")
PY
    while IFS= read -r line; do
      case "$line" in
        ok\|*) ok "${line#ok|}" ;;
        skip\|*) skip "${line#skip|}" ;;
        *) fail "${line#FAIL|}" ;;
      esac
    done < <(python3 "$WORK/smoke.py" "$WORK/rendered.yaml" "$TEST_TAG" src/wattson 2>&1)
    size_mb="$(docker image inspect "$TEST_TAG" --format '{{.Size}}' | awk '{printf "%d", $1 / 1000000}')"
    check "the image is small: ${size_mb} MB (under 400 MB unpacked)" test "$size_mb" -lt 400
  fi

  # The shield's own image (Dockerfile.shield): Python, the "shield" group of uv.lock, one file.
  SHIELD_TAG="wattson-shield-k8s-test:local"
  if docker build -q --platform linux/amd64 -f Dockerfile.shield -t "$SHIELD_TAG" . >"$WORK/shield-build.log" 2>&1 \
    || docker build -q --platform linux/amd64 --network host -f Dockerfile.shield -t "$SHIELD_TAG" . >"$WORK/shield-build.log" 2>&1; then
    ok "the shield's image builds (Dockerfile.shield)"
    in_shield() {  # COMMAND... -> runs in the shield's image the way its pod runs it
      docker run --rm --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --network none "$SHIELD_TAG" "$@"
    }
    check "the shield's image: user 10001, /app/shield.py, and nothing of wattson or Strands" in_shield python -c '
import importlib.util as u, os
assert (os.getuid(), os.getgid()) == (10001, 10001) and os.path.isfile("/app/shield.py")
assert u.find_spec("wattson") is None and u.find_spec("strands") is None'
    check "the shield's packages import inside it (ONNX Runtime, tokenizers, numpy, Starlette, uvicorn); no uv, no pip" in_shield python -c '
import shutil, numpy, onnxruntime, starlette, tokenizers, uvicorn
assert not shutil.which("uv") and not shutil.which("pip") and not shutil.which("pip3")'
    check "the shield stops at once without its token, and says so" bash -c '
      out="$(docker run --rm --read-only --tmpfs /tmp --network none --user 10001:10001 "$1" 2>&1)"; [[ $? -ne 0 ]] && grep -q "SHIELD_TOKEN is missing" <<<"$out"' _ "$SHIELD_TAG"
    check "the shield stops at once without its model files, and says which" bash -c '
      out="$(docker run --rm --read-only --tmpfs /tmp --network none --user 10001:10001 -e SHIELD_TOKEN="$(printf "s%.0s" {1..40})" -e INJECTION_LABEL=INJECTION "$1" 2>&1)"
      [[ $? -ne 0 ]] && grep -q "the model file /models/model.onnx is missing" <<<"$out" && ! grep -q "ssssssss" <<<"$out"' _ "$SHIELD_TAG"
    shield_mb="$(docker image inspect "$SHIELD_TAG" --format '{{.Size}}' | awk '{printf "%d", $1 / 1000000}')"
    check "the shield's image is small: ${shield_mb} MB (under 350 MB unpacked; the model is not in it)" test "$shield_mb" -lt 350
  elif grep -qiE 'temporary failure|name resolution|could not resolve|connection|timed out|network' "$WORK/shield-build.log"; then
    skip "the shield's image could not be built: no network"
  else
    fail "the shield's image builds (Dockerfile.shield)"
    tail -n 15 "$WORK/shield-build.log" | sed 's/^/         | /'
  fi

  # The model download script, taken from the filled-in manifests, in its pinned image.
  python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
triage = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-triage"))
init = triage["spec"]["template"]["spec"]["initContainers"][0]
open(sys.argv[2], "w").write(init["command"][-1])
open(sys.argv[3], "w").write(init["image"])' "$WORK/rendered.yaml" "$WORK/fetch.sh" "$WORK/fetch.image"
  FETCH_IMAGE_REF="$(cat "$WORK/fetch.image")"
  if ! docker image inspect "$FETCH_IMAGE_REF" >/dev/null 2>&1 && ! docker pull -q "$FETCH_IMAGE_REF" >/dev/null 2>&1; then
    skip "the download image $FETCH_IMAGE_REF cannot be pulled (no network)"
  else
    # fetch NETWORK FILE_NAME URL SHA256 [SETUP] -> runs the script twice in a row on an empty
    # volume (a tmpfs), the way the init container runs it, and lists the volume.
    fetch() {
      local network="$1"
      shift
      docker run --rm --network "$network" --read-only --user 10001:10001 --cap-drop ALL --tmpfs /tmp --tmpfs /models:uid=10001,gid=10001 \
        -e HOME=/tmp -e FILE_NAME="$1" -e SOURCE_URL="$2" -e SHA256="$3" -e SETUP="${4:-true}" -e SCRIPT="$(cat "$WORK/fetch.sh")" \
        --entrypoint /bin/sh "$FETCH_IMAGE_REF" -c 'eval "$SETUP"; sh -c "$SCRIPT"; echo "first=$?"; sh -c "$SCRIPT" >/dev/null 2>&1; echo "second=$?"; ls -A /models' 2>&1
    }
    known="$(printf 'model' | sha256sum | cut -c1-64)"
    out="$(fetch none m.gguf https://127.0.0.1:9/none "$known" 'printf model >/models/m.gguf')"
    check "the download script: a right file that is there is kept, without the network" bash -c 'grep -q "is there and its SHA-256 is right" <<<"$1" && grep -qx "first=0" <<<"$1"' _ "$out"
    out="$(fetch none m.gguf https://127.0.0.1:9/none "$known" 'printf model >/models/m.gguf; printf old >/models/old.gguf; printf x >/models/.partial-old.gguf')"
    check "the download script: the files of an earlier model are removed, the right one is kept" bash -c '
      grep -qx "first=0" <<<"$1" && grep -q "removed the old file old.gguf" <<<"$1" && [[ "$(grep -v -e fetch-model -e first= -e second= <<<"$1")" == m.gguf ]]' _ "$out"
    out="$(fetch none m.gguf https://127.0.0.1:9/none "$known" 'printf wrong >/models/m.gguf')"
    check "the download script: a wrong file is not used (it downloads; here that fails, and so does the pod start)" bash -c 'grep -q "downloading m.gguf" <<<"$1" && grep -qx "first=[1-9][0-9]*" <<<"$1"' _ "$out"
    check "the download script: a failed download leaves no partial file" bash -c '! grep -q "^\.partial-" <<<"$1"' _ "$out"
    small_url="https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/README.md"
    if small_sum="$(curl -fsSL -m 30 "$small_url" 2>/dev/null | sha256sum | cut -c1-64)" && [[ "$small_sum" != "$(printf '' | sha256sum | cut -c1-64)" ]]; then
      out="$(fetch bridge small.gguf "$small_url" "$small_sum")"
      check "the download script: downloads, checks, renames; the second run only checks" bash -c '
        grep -q "small.gguf downloaded and checked" <<<"$1" && grep -qx "first=0" <<<"$1" && grep -qx "second=0" <<<"$1" && grep -qx "small.gguf" <<<"$1"' _ "$out"
      out="$(fetch bridge small.gguf "$small_url" "$(printf '1%.0s' {1..64})")"
      check "the download script: a download with the wrong SHA-256 is removed, and the pod does not start" bash -c '
        grep -q "wrong SHA-256" <<<"$1" && grep -qx "first=1" <<<"$1" && ! grep -q "gguf" <<<"$(grep -v -e fetch-model -e first= -e second= <<<"$1")"' _ "$out"
    else
      skip "the download of a small file (no internet)"
    fi
  fi

  # Tempo in its pinned image, run the way its pod runs it (read-only root, user 10001, no
  # capabilities, 512 MiB), with the config of the manifests. The spans of six made-up tasks
  # (the names and attributes the code sends) go in over OTLP/HTTP; then TraceQL on 3200 finds
  # them, and every query of the Grafana dashboard runs on Tempo and finds data.
  python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-tempo"))
open(sys.argv[2], "w").write(config["data"]["tempo.yaml"])
tempo = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-tempo"))
open(sys.argv[3], "w").write(tempo["spec"]["template"]["spec"]["containers"][0]["image"])' "$WORK/rendered.yaml" "$WORK/tempo.yaml" "$WORK/tempo.image"
  chmod 644 "$WORK/tempo.yaml"
  TEMPO_IMAGE_REF="$(cat "$WORK/tempo.image")"
  if ! docker image inspect "$TEMPO_IMAGE_REF" >/dev/null 2>&1 && ! docker pull -q "$TEMPO_IMAGE_REF" >/dev/null 2>&1; then
    skip "the Tempo image $TEMPO_IMAGE_REF cannot be pulled (no network)"
  else
    TEMPO_CONTAINER="wattson-tempo-test-$$-$RANDOM"
    tempo_ready=no
    if docker run -d --name "$TEMPO_CONTAINER" --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
      --memory 512m --tmpfs /tmp --tmpfs /var/tempo:uid=10001,gid=10001,mode=0700 -v "$WORK/tempo.yaml:/etc/tempo/tempo.yaml:ro" \
      -p 127.0.0.1::4318 -p 127.0.0.1::3200 "$TEMPO_IMAGE_REF" -config.file=/etc/tempo/tempo.yaml >/dev/null 2>"$WORK/tempo-run.err"; then
      tempo_otlp="$(docker port "$TEMPO_CONTAINER" 4318/tcp | head -n 1)"
      tempo_http="$(docker port "$TEMPO_CONTAINER" 3200/tcp | head -n 1)"
      for _ in $(seq 1 60); do
        if [[ "$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://$tempo_http/ready")" == 200 ]]; then tempo_ready=yes; break; fi
        sleep 1
      done
    fi
    if [[ "$tempo_ready" == yes ]]; then
      ok "Tempo starts from its pinned image (read-only root, user 10001) with the config of the manifests, and is ready"
      # Six tasks, all spans 85 to 110 seconds old: older than the query cut-off of Tempo (30 s),
      # within its ingestion slack (2 min).
      python3 - "$WORK/spans.json" <<'PY'
import itertools, json, sys, time
now = time.time_ns()
ids = itertools.count(1)
def attr(k, v):
    if isinstance(v, bool): return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int): return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float): return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": v}}
spans = {"wattson-orchestrator": [], "wattson-triage": [], "wattson-knowledge": []}
outcomes = ["completed", "rejected", "failed", "interrupted", "expired", "blocked"]
guard_labels = ["work", "work", "risky", "out_of_scope", "work", "malicious"]
methods = ["model", "rules", "override", "model", "model", "rules"]
for i in range(6):
    trace, start = "%032x" % (0xA5E0 + i), now - 110 * 10**9 + i * 10**9
    def span(service, name, at, seconds, **attributes):
        spans[service].append({"traceId": trace, "spanId": "%016x" % next(ids), "name": name, "kind": 1,
                               "startTimeUnixNano": str(at), "endTimeUnixNano": str(at + int(seconds * 1e9)),
                               "attributes": [attr(k, v) for k, v in attributes.items()]})
    at = start
    for step in ("triage", "plan", "collect", "review", "handoff"):
        waited = {"wattson.interrupt": "approve_" + step, "wattson.wait_s": 12.5 + i, "wattson.wait_ms": 12500 + 1000 * i} if step in ("collect", "review") else {}
        span("wattson-orchestrator", "wattson.step", at, 2.0 + i / 10, **{"wattson.task_id": "t%d" % i, "wattson.step": step,
             "wattson.workflow": "prompt_help", "wattson.status": "ok"}, **waited)
        at += int(2.5e9)
    span("wattson-triage", "wattson.model.classify", start, 3.5, **{"wattson.model.name": "granite", "wattson.model.ok": True,
         "wattson.triage.confidence_pct": 50 + 5 * i})
    span("wattson-triage", "wattson.model.hints", start, 2.5, **{"wattson.model.name": "granite", "wattson.model.ok": i != 2})
    span("wattson-knowledge", "wattson.grimoire.call", start + 10**9, 0.4, **{"wattson.grimoire.tool": "search_documents", "wattson.grimoire.ok": i != 3})
    span("wattson-knowledge", "wattson.knowledge.retrieve", start + 10**9, 0.6, **{"wattson.knowledge.docs": 3})
    # The injection model reads the request too: it flags task 2's request.
    span("wattson-orchestrator", "wattson.guard.request", start, 0.0, **{"wattson.task_id": "t%d" % i, "wattson.guard.label": guard_labels[i],
         "wattson.guard.method": "rules+model", "wattson.guard.mode": "monitor", "wattson.guard.rule_hits": int(guard_labels[i] != "work"),
         "wattson.guard.latency_ms": 900 + 10 * i, "wattson.guard.injection_model": "Horizon-Labs/prompt-injection-guard-small",
         "wattson.guard.injection_ok": True, "wattson.guard.injection_flagged": int(i == 2),
         "wattson.guard.injection_score_pct": 99 if i == 2 else 3, "wattson.guard.injection_latency_ms": 300})
    if i in (1, 4):
        # The injection model: it flags one part in task 1, and does not answer in task 4.
        span("wattson-orchestrator", "wattson.guard.evidence", start + 10**9, 0.0, **{"wattson.task_id": "t%d" % i, "wattson.guard.evidence_removed": i,
             "wattson.guard.evidence_rules": "override-instructions", "wattson.guard.injection_model": "Horizon-Labs/prompt-injection-guard-small",
             "wattson.guard.injection_mode": "warn", "wattson.guard.injection_ok": i == 1, "wattson.guard.injection_parts": 4,
             "wattson.guard.injection_flagged": int(i == 1), "wattson.guard.injection_latency_ms": 1200})
    if guard_labels[i] == "risky":
        span("wattson-orchestrator", "wattson.step", start + 2 * 10**9, 0.1, **{"wattson.task_id": "t%d" % i, "wattson.step": "start_task",
             "wattson.workflow": "prompt_help", "wattson.status": "triage", "wattson.interrupt": "guard_confirm", "wattson.wait_s": 4.5, "wattson.wait_ms": 4500})
    span("wattson-orchestrator", "wattson.task.end", at, 0.01, **{"wattson.task_id": "t%d" % i, "wattson.outcome": outcomes[i],
         "wattson.workflow": "prompt_help", "wattson.triage.category": "prompt_help", "wattson.triage.confidence": 0.5 + i / 20,
         "wattson.triage.confidence_pct": 50 + 5 * i,
         "wattson.triage.method": methods[i], "wattson.task_duration_s": (at - start) / 1e9})
json.dump({"resourceSpans": [{"resource": {"attributes": [attr("service.name", s)]}, "scopeSpans": [{"scope": {"name": "wattson"}, "spans": v}]}
                             for s, v in spans.items()]}, open(sys.argv[1], "w"))
PY
      check "it takes the spans of six tasks over OTLP/HTTP on 4318 (200)" test "$(curl -s -o /dev/null -m 10 -w '%{http_code}' -H 'Content-Type: application/json' \
        --data-binary "@$WORK/spans.json" "http://$tempo_otlp/v1/traces")" = 200
      found=0
      for _ in $(seq 1 40); do
        found="$(curl -s -m 5 -G "http://$tempo_http/api/search" --data-urlencode 'q={name="wattson.task.end"}' --data-urlencode limit=50 \
          --data-urlencode "start=$(($(date +%s) - 600))" --data-urlencode "end=$(date +%s)" | python3 -c 'import json, sys; print(len(json.load(sys.stdin).get("traces") or []))' 2>/dev/null || echo 0)"
        [[ "$found" == 6 ]] && break
        sleep 1
      done
      check "TraceQL search on 3200 finds the six tasks (found $found)" test "$found" = 6
      while IFS= read -r line; do
        case "$line" in
          ok\|*) ok "${line#ok|}" ;;
          *) fail "${line#FAIL|}" ;;
        esac
      done < <(python3 - "$tempo_http" k8s/grafana/wattson-dashboard.json <<'PY' 2>&1
import json, sys, time, urllib.parse, urllib.request
base, path = sys.argv[1:]
now = int(time.time())
for panel in json.load(open(path))["panels"]:
    for target in panel["targets"]:
        params = {"q": target["query"], "start": now - 900, "end": now}
        if "metricsQueryType" in target:
            # Instant: a range query of Tempo leaves out the step that is not over yet.
            url, found = "/api/metrics/query", lambda data: [s for s in data.get("series", []) if s.get("value") is not None]
        else:
            params.update(limit=target.get("limit", 20), spss=target.get("spss", 3))
            url, found = "/api/search", lambda data: data.get("traces") or []
        try:
            with urllib.request.urlopen("http://%s%s?%s" % (base, url, urllib.parse.urlencode(params)), timeout=20) as answer:
                data = json.load(answer)
            result = len(found(data))
        except Exception as error:
            result = "error: %s" % error
        print(("ok|" if isinstance(result, int) and result > 0 else "FAIL|") + "the dashboard query runs on Tempo and finds data: %s (%s %s)" % (panel["title"], target["refId"], result))
PY
      )
    else
      fail "Tempo starts from its pinned image with the config of the manifests, and is ready ($(tail -c 300 "$WORK/tempo-run.err") $(docker logs "$TEMPO_CONTAINER" 2>&1 | tail -n 5 | tr '\n' ' ' | cut -c1-400))"
    fi
    docker rm -f "$TEMPO_CONTAINER" >/dev/null 2>&1
    check "the Tempo container is removed" not docker container inspect "$TEMPO_CONTAINER"
    TEMPO_CONTAINER=""
  fi
fi

printf '\n%d passed, %d failed, %d skipped\n' "$PASSED" "$FAILED" "$SKIPPED"
[[ "$FAILED" -eq 0 ]]
