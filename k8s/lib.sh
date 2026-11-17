#!/usr/bin/env bash
# The settings of Wattson Agent, for the scripts in this folder. It is read by them (". lib.sh"),
# never run by itself.
#
# settings.env is READ, line by line, not run as a script: a value can never become a command.
#
# shellcheck disable=SC2034  # the settings are set and read by name (printf -v, ${!name}) and by the scripts that source this file

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERE="$ROOT/k8s"
SETTINGS_FILE="${SETTINGS_FILE:-$ROOT/settings.env}"

# Every setting that settings.env may hold, with its value when the file does not say.
SETTING_NAMES=(APP_NAME K8S_CONTEXT K8S_NAMESPACE REGISTRY PULL_SECRET STORAGE_CLASS DATA_SIZE KNOWLEDGE_DATA_SIZE
  MODELS_SIZE GRIMOIRE_MCP_URL LLAMA_IMAGE FETCH_IMAGE MODEL_FILE MODEL_URL MODEL_SHA256 LLAMA_CTX LLAMA_THREADS LLAMA_BATCH_THREADS
  MIN_CONFIDENCE HISTORY_RETENTION_DAYS TASK_TTL_MINUTES NODE_LOCAL_DNS
  ORCHESTRATOR_REQUESTS ORCHESTRATOR_LIMITS TRIAGE_REQUESTS TRIAGE_LIMITS KNOWLEDGE_REQUESTS KNOWLEDGE_LIMITS
  LLAMA_REQUESTS LLAMA_LIMITS FETCH_REQUESTS FETCH_LIMITS
  MODEL_BACKEND KUBEAI_NAMESPACE KUBEAI_MODEL KUBEAI_MIN_REPLICAS KUBEAI_SCALE_DOWN_DELAY_SECONDS KUBEAI_BASE_URL
  TELEMETRY TEMPO_IMAGE TEMPO_SIZE TEMPO_RETENTION GRAFANA_NAMESPACE GRAFANA_SERVICE TEMPO_REQUESTS TEMPO_LIMITS
  GUARD_MODE GUARD_TIMEOUT_S
  INJECTION_MODE INJECTION_MODEL_REPO INJECTION_MODEL_COMMIT INJECTION_MODEL_FILE INJECTION_MODEL_SHA256
  INJECTION_TOKENIZER_FILE INJECTION_TOKENIZER_SHA256 INJECTION_CONFIG_FILE INJECTION_CONFIG_SHA256
  INJECTION_LABEL INJECTION_THRESHOLD INJECTION_WINDOW INJECTION_TIMEOUT_S SHIELD_REQUESTS SHIELD_LIMITS)
APP_NAME="wattson" K8S_CONTEXT="" K8S_NAMESPACE="" REGISTRY="" PULL_SECRET="" STORAGE_CLASS=""
DATA_SIZE="1Gi" KNOWLEDGE_DATA_SIZE="1Gi" MODELS_SIZE="3Gi" GRIMOIRE_MCP_URL="" LLAMA_IMAGE="" FETCH_IMAGE=""
MODEL_FILE="" MODEL_URL="" MODEL_SHA256="" LLAMA_CTX="4096" LLAMA_THREADS="4" LLAMA_BATCH_THREADS="6" MIN_CONFIDENCE="0.6"
HISTORY_RETENTION_DAYS="90" TASK_TTL_MINUTES="120" NODE_LOCAL_DNS=""
ORCHESTRATOR_REQUESTS="50m/192Mi" ORCHESTRATOR_LIMITS="1000m/512Mi"
TRIAGE_REQUESTS="50m/192Mi" TRIAGE_LIMITS="500m/384Mi"
KNOWLEDGE_REQUESTS="50m/192Mi" KNOWLEDGE_LIMITS="500m/384Mi"
LLAMA_REQUESTS="500m/1536Mi" LLAMA_LIMITS="2000m/2Gi"
FETCH_REQUESTS="50m/32Mi" FETCH_LIMITS="500m/128Mi"
MODEL_BACKEND="sidecar" KUBEAI_NAMESPACE="kubeai" KUBEAI_MODEL="granite-4-2-3b" KUBEAI_MIN_REPLICAS="0"
KUBEAI_SCALE_DOWN_DELAY_SECONDS="900" KUBEAI_BASE_URL=""
TELEMETRY="on" TEMPO_IMAGE="" TEMPO_SIZE="5Gi" TEMPO_RETENTION="336h" GRAFANA_NAMESPACE="" GRAFANA_SERVICE=""
TEMPO_REQUESTS="50m/256Mi" TEMPO_LIMITS="200m/512Mi"
GUARD_MODE="monitor" GUARD_TIMEOUT_S="20"
INJECTION_MODE="off" INJECTION_MODEL_REPO="" INJECTION_MODEL_COMMIT="" INJECTION_MODEL_FILE="" INJECTION_MODEL_SHA256=""
INJECTION_TOKENIZER_FILE="tokenizer.json" INJECTION_TOKENIZER_SHA256="" INJECTION_CONFIG_FILE="config.json"
INJECTION_CONFIG_SHA256="" INJECTION_LABEL="" INJECTION_THRESHOLD="0.95" INJECTION_WINDOW="2048" INJECTION_TIMEOUT_S="30"
SHIELD_REQUESTS="100m/768Mi" SHIELD_LIMITS="2000m/1280Mi"
# Cognee, the memory for Claude Code, and its web UI (k8s/cognee.sh; deploy.sh does not use these).
# COGNEE_REQUESTS and COGNEE_LIMITS are those of the API server (the container that does the work).
SETTING_NAMES+=(COGNEE_SIZE COGNEE_REQUESTS COGNEE_LIMITS COGNEE_LOCAL_PORT COGNEE_API_LOCAL_PORT COGNEE_UI_LOCAL_PORT
  COGNEE_MCP_REQUESTS COGNEE_MCP_LIMITS COGNEE_UI_REQUESTS COGNEE_UI_LIMITS)
COGNEE_SIZE="10Gi" COGNEE_REQUESTS="500m/1536Mi" COGNEE_LIMITS="4000m/5Gi" COGNEE_LOCAL_PORT="8765"
COGNEE_API_LOCAL_PORT="8766" COGNEE_UI_LOCAL_PORT="8767"
COGNEE_MCP_REQUESTS="50m/512Mi" COGNEE_MCP_LIMITS="1000m/1536Mi" COGNEE_UI_REQUESTS="20m/128Mi" COGNEE_UI_LIMITS="500m/512Mi"
# "Seen before": the knowledge agent reads the lessons in that pod, read-only (deploy.sh uses these).
SETTING_NAMES+=(COGNEE_RECALL COGNEE_RECALL_URL COGNEE_RECALL_DATASET COGNEE_RECALL_LESSONS COGNEE_RECALL_TIMEOUT_S)
COGNEE_RECALL="off" COGNEE_RECALL_URL="" COGNEE_RECALL_DATASET="claude_code_memory" COGNEE_RECALL_LESSONS="3"
COGNEE_RECALL_TIMEOUT_S="5"

# The three keys of the Secret <APP_NAME>. Each is 32 random bytes, base64url: the CLI's
# token for the orchestrator, and the orchestrator's tokens for the two other agents.
SECRET_KEYS=(WATTSON_TOKEN TRIAGE_TOKEN KNOWLEDGE_TOKEN)

# The key of the Secret <APP_NAME>-shield (INJECTION_MODE warn or cut): the orchestrator's token
# for the shield. 32 random bytes, base64url, made once by deploy.sh; the Secret <APP_NAME> is
# never touched for it.
SHIELD_SECRET_KEYS=(SHIELD_TOKEN)

# The ports of docs/design.md. The orchestrator listens on 127.0.0.1 only. The shield (the
# injection model) listens on 9003.
ORCHESTRATOR_PORT=9000 TRIAGE_PORT=9001 KNOWLEDGE_PORT=9002 LLAMA_PORT=8081 SHIELD_PORT=9003

# The MCP port of the pod <APP_NAME>-cognee (k8s/cognee.sh), also the port of its Service. The
# knowledge agent reads the lessons there (COGNEE_RECALL=on).
COGNEE_PORT=8000

# The port of the KubeAI proxy's pod (its Service "kubeai" listens on 80 and sends to 8000).
# The network rule of the triage pod names 8000: Calico checks the port of the pod, not the
# port of the Service.
KUBEAI_PROXY_PORT=8000

# The rules of the Guard (k8s/guard/, written by you): deploy.sh puts the two files in the
# ConfigMap <APP_NAME>-guard, mounted read-only in the orchestrator at /etc/wattson/guard; k8s/guard.sh
# changes only that ConfigMap. GUARD_DIR in the environment replaces the folder (the tests).
GUARD_DIR="${GUARD_DIR:-$HERE/guard}"
GUARD_RULES_NAME=rules.yaml GUARD_POLICIES_NAME=policies.cedar
# The most each file may hold (a ConfigMap holds at most 1 MiB in all).
GUARD_FILE_MAX_BYTES=262144

# The ports of Tempo (TELEMETRY=on): OTLP over HTTP from the agents, and its HTTP API (TraceQL)
# for Grafana. Its other ports (gRPC 9095, memberlist 7946) are for itself only.
TEMPO_OTLP_PORT=4318 TEMPO_HTTP_PORT=3200

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

# Reads settings.env into the variables above. A line is NAME=value; spaces around the value
# and one pair of quotes around it are dropped. A name that is not a setting stops the script.
read_settings() {
  [[ -f "$SETTINGS_FILE" ]] || die "The settings file was not found: $SETTINGS_FILE"
  local line name value known known_name
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" =~ ^[[:space:]]*([A-Z][A-Z0-9_]*)=(.*)$ ]] || die "settings.env: this line is not NAME=value: $line"
    name="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
    known=no
    for known_name in "${SETTING_NAMES[@]}"; do [[ "$known_name" == "$name" ]] && known=yes; done
    [[ "$known" == yes ]] || die "settings.env: $name is not a setting of Wattson Agent."
    printf -v "$name" '%s' "$value"
  done <"$SETTINGS_FILE"
}

is_k8s_name() {  # VALUE -> a DNS-1123 name (a Secret, a storage class)
  [[ "$1" =~ ^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$ ]]
}

is_size() {  # VALUE -> 1Gi, 500Mi
  [[ "$1" =~ ^[1-9][0-9]{0,3}(Mi|Gi)$ ]]
}

is_pinned_image() {  # VALUE -> name[:tag]@sha256:<64 hex>
  [[ "$1" =~ ^[a-z0-9]([a-z0-9._/:-]*[a-z0-9])?@sha256:[0-9a-f]{64}$ ]]
}

is_https_url() {  # VALUE -> https://host/path, no spaces, quotes or shell signs
  [[ "$1" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~%/+=-]*)?$ ]]
}

# "cpu/memory" -> two values. 50m or 1 or 1.5 for the CPU; 192Mi or 2Gi for the memory.
cpu_millis() {  # "6000m" or "6" or "1.5" -> millicores
  local value="$1"
  if [[ "$value" == *m ]]; then
    printf '%s' "${value%m}"
  else
    python3 -c 'import sys; print(round(float(sys.argv[1]) * 1000))' "$value"
  fi
}

split_resources() {  # SETTING_NAME PREFIX -> sets PREFIX_CPU and PREFIX_MEMORY
  local value="${!1}"
  [[ "$value" =~ ^([1-9][0-9]{0,4}m|[1-9][0-9]?(\.[0-9]{1,3})?)/([1-9][0-9]{0,5}(Mi|Gi))$ ]] \
    || die "$1 must look like 50m/192Mi (CPU/memory). It is: \"$value\""
  printf -v "${2}_CPU" '%s' "${BASH_REMATCH[1]}"
  printf -v "${2}_MEMORY" '%s' "${BASH_REMATCH[3]}"
}

# Checks every setting. A value goes into a Kubernetes file, an address or a command, so each
# one may hold only what is safe there.
check_settings() {
  [[ "$APP_NAME" =~ ^[a-z][a-z0-9-]{0,29}$ && "$APP_NAME" != *- && "$APP_NAME" != *--* ]] \
    || die "APP_NAME must be lower-case letters, digits and \"-\", start with a letter, and be at most 30 long. It is: \"$APP_NAME\""
  [[ "$K8S_CONTEXT" =~ ^[A-Za-z0-9@._:/-]+$ ]] || die "K8S_CONTEXT is missing or has signs that a context name does not have: \"$K8S_CONTEXT\""
  [[ "$K8S_NAMESPACE" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "K8S_NAMESPACE is not the name of a namespace: \"$K8S_NAMESPACE\""
  [[ "$REGISTRY" =~ ^[a-z0-9]([a-z0-9.:-]*[a-z0-9])?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*$ ]] \
    || die "REGISTRY must look like host/project. It is: \"$REGISTRY\""
  [[ -z "$PULL_SECRET" ]] || is_k8s_name "$PULL_SECRET" || die "PULL_SECRET is not the name of a Secret: \"$PULL_SECRET\""
  is_k8s_name "$STORAGE_CLASS" || die "STORAGE_CLASS must name a storage class (the cluster has no default one). It is: \"$STORAGE_CLASS\""
  is_size "$DATA_SIZE" || die "DATA_SIZE must look like 1Gi or 500Mi. It is: \"$DATA_SIZE\""
  is_size "$KNOWLEDGE_DATA_SIZE" || die "KNOWLEDGE_DATA_SIZE must look like 1Gi or 500Mi. It is: \"$KNOWLEDGE_DATA_SIZE\""
  is_size "$MODELS_SIZE" || die "MODELS_SIZE must look like 3Gi. It is: \"$MODELS_SIZE\""
  is_https_url "$GRIMOIRE_MCP_URL" || die "GRIMOIRE_MCP_URL must be an https:// address. It is: \"$GRIMOIRE_MCP_URL\""
  is_pinned_image "$LLAMA_IMAGE" || die "LLAMA_IMAGE must be pinned by digest: name:tag@sha256:<64 hex>. It is: \"$LLAMA_IMAGE\""
  is_pinned_image "$FETCH_IMAGE" || die "FETCH_IMAGE must be pinned by digest: name:tag@sha256:<64 hex>. It is: \"$FETCH_IMAGE\""
  [[ "$MODEL_FILE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.gguf$ ]] || die "MODEL_FILE must be a plain file name that ends with .gguf. It is: \"$MODEL_FILE\""
  is_https_url "$MODEL_URL" || die "MODEL_URL must be an https:// address. It is: \"$MODEL_URL\""
  [[ "$MODEL_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "MODEL_SHA256 must be 64 lower-case hex signs. It is: \"$MODEL_SHA256\""
  [[ "$LLAMA_CTX" =~ ^[1-9][0-9]{2,5}$ ]] || die "LLAMA_CTX must be a whole number of tokens, like 4096. It is: \"$LLAMA_CTX\""
  [[ "$LLAMA_THREADS" =~ ^[1-9][0-9]?$ ]] || die "LLAMA_THREADS must be a whole number from 1 to 99. It is: \"$LLAMA_THREADS\""
  [[ "$LLAMA_BATCH_THREADS" =~ ^[1-9][0-9]?$ ]] || die "LLAMA_BATCH_THREADS must be a whole number from 1 to 99. It is: \"$LLAMA_BATCH_THREADS\""
  [[ "$MIN_CONFIDENCE" =~ ^(0(\.[0-9]{1,3})?|1(\.0{1,3})?)$ ]] || die "MIN_CONFIDENCE must be a number from 0 to 1, like 0.6. It is: \"$MIN_CONFIDENCE\""
  [[ "$HISTORY_RETENTION_DAYS" =~ ^[1-9][0-9]{0,3}$ ]] || die "HISTORY_RETENTION_DAYS must be a whole number of days. It is: \"$HISTORY_RETENTION_DAYS\""
  [[ "$TASK_TTL_MINUTES" =~ ^[1-9][0-9]{0,4}$ ]] || die "TASK_TTL_MINUTES must be a whole number of minutes. It is: \"$TASK_TTL_MINUTES\""
  [[ -z "$NODE_LOCAL_DNS" || "$NODE_LOCAL_DNS" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "NODE_LOCAL_DNS must be an IPv4 address, or empty. It is: \"$NODE_LOCAL_DNS\""
  split_resources ORCHESTRATOR_REQUESTS ORCHESTRATOR_REQ
  split_resources ORCHESTRATOR_LIMITS ORCHESTRATOR_LIM
  split_resources TRIAGE_REQUESTS TRIAGE_REQ
  split_resources TRIAGE_LIMITS TRIAGE_LIM
  split_resources KNOWLEDGE_REQUESTS KNOWLEDGE_REQ
  split_resources KNOWLEDGE_LIMITS KNOWLEDGE_LIM
  split_resources LLAMA_REQUESTS LLAMA_REQ
  split_resources LLAMA_LIMITS LLAMA_LIM
  split_resources FETCH_REQUESTS FETCH_REQ
  # More threads than the CPU limit allows make llama.cpp wait on itself: much slower, not faster.
  local llama_lim_millis
  llama_lim_millis="$(cpu_millis "$LLAMA_LIM_CPU")"
  ((LLAMA_THREADS * 1000 <= llama_lim_millis)) \
    || die "LLAMA_THREADS ($LLAMA_THREADS) is more than the CPU limit of LLAMA_LIMITS ($LLAMA_LIM_CPU)."
  ((LLAMA_BATCH_THREADS * 1000 <= llama_lim_millis)) \
    || die "LLAMA_BATCH_THREADS ($LLAMA_BATCH_THREADS) is more than the CPU limit of LLAMA_LIMITS ($LLAMA_LIM_CPU)."
  split_resources FETCH_LIMITS FETCH_LIM
  check_model_backend
  check_telemetry
  check_guard
  check_injection
  check_cognee_recall

  # The names that follow from the settings.
  ORCHESTRATOR="$APP_NAME-orchestrator" TRIAGE="$APP_NAME-triage" KNOWLEDGE="$APP_NAME-knowledge" TEMPO="$APP_NAME-tempo"
  SHIELD="$APP_NAME-shield" SHIELD_SECRET="$APP_NAME-shield"
  GUARD_CONFIGMAP="$APP_NAME-guard"
  # The network rules of KubeAI's namespace (k8s/kubeai/networkpolicy.yaml, applied by kubeai.sh).
  KUBEAI_POLICIES=("$APP_NAME-kubeai-deny-ingress" "$APP_NAME-kubeai-proxy" "$APP_NAME-kubeai-models")
  # Where the agents send their traces (the Service of Tempo, in their own namespace), and the
  # address Grafana reads them from (from another namespace).
  OTLP_ENDPOINT="http://$TEMPO:$TEMPO_OTLP_PORT"
  TEMPO_URL="http://$TEMPO.$K8S_NAMESPACE.svc.cluster.local:$TEMPO_HTTP_PORT"
  # The name the triage agent reports for its model: the file name without ".gguf".
  MODEL_NAME="${MODEL_FILE%.gguf}"
  # Where the triage agent asks the model (an OpenAI-compatible address), and the "model" of
  # its requests: the llama.cpp server next to it, or the KubeAI proxy and the KubeAI Model.
  if [[ "$MODEL_BACKEND" == kubeai ]]; then
    LLM_BASE_URL="$KUBEAI_BASE_URL" LLM_MODEL="$KUBEAI_MODEL"
  else
    LLM_BASE_URL="http://127.0.0.1:$LLAMA_PORT/v1" LLM_MODEL="$MODEL_NAME"
  fi
  # The small model of the Guard (the orchestrator's aux model): with KubeAI the same Model as
  # triage, through the same proxy. With the sidecar none: its llama.cpp listens on 127.0.0.1 of
  # the triage pod, so the orchestrator cannot reach it; the Guard then uses its rules only.
  if [[ "$MODEL_BACKEND" == kubeai ]]; then
    GUARD_MODEL_URL="$KUBEAI_BASE_URL" GUARD_MODEL="$KUBEAI_MODEL"
  else
    GUARD_MODEL_URL="" GUARD_MODEL=""
  fi
  export APP_NAME K8S_NAMESPACE PULL_SECRET STORAGE_CLASS DATA_SIZE KNOWLEDGE_DATA_SIZE MODELS_SIZE GRIMOIRE_MCP_URL
  export LLAMA_IMAGE FETCH_IMAGE MODEL_FILE MODEL_URL MODEL_SHA256 MODEL_NAME LLAMA_CTX LLAMA_THREADS LLAMA_BATCH_THREADS MIN_CONFIDENCE
  export HISTORY_RETENTION_DAYS TASK_TTL_MINUTES NODE_LOCAL_DNS ORCHESTRATOR TRIAGE KNOWLEDGE
  export ORCHESTRATOR_PORT TRIAGE_PORT KNOWLEDGE_PORT LLAMA_PORT
  export ORCHESTRATOR_REQ_CPU ORCHESTRATOR_REQ_MEMORY ORCHESTRATOR_LIM_CPU ORCHESTRATOR_LIM_MEMORY
  export TRIAGE_REQ_CPU TRIAGE_REQ_MEMORY TRIAGE_LIM_CPU TRIAGE_LIM_MEMORY
  export KNOWLEDGE_REQ_CPU KNOWLEDGE_REQ_MEMORY KNOWLEDGE_LIM_CPU KNOWLEDGE_LIM_MEMORY
  export LLAMA_REQ_CPU LLAMA_REQ_MEMORY LLAMA_LIM_CPU LLAMA_LIM_MEMORY
  export FETCH_REQ_CPU FETCH_REQ_MEMORY FETCH_LIM_CPU FETCH_LIM_MEMORY
  export MODEL_BACKEND KUBEAI_NAMESPACE KUBEAI_MODEL KUBEAI_MIN_REPLICAS KUBEAI_SCALE_DOWN_DELAY_SECONDS KUBEAI_BASE_URL KUBEAI_SERVICE KUBEAI_PROXY_PORT
  export MODEL_HF_REPO MODEL_HF_COMMIT LLM_BASE_URL LLM_MODEL
  export TELEMETRY TEMPO TEMPO_IMAGE TEMPO_SIZE TEMPO_RETENTION GRAFANA_NAMESPACE GRAFANA_SERVICE TEMPO_OTLP_PORT TEMPO_HTTP_PORT
  export OTLP_ENDPOINT TEMPO_URL TEMPO_REQ_CPU TEMPO_REQ_MEMORY TEMPO_LIM_CPU TEMPO_LIM_MEMORY
  export GUARD_MODE GUARD_TIMEOUT_S GUARD_MODEL_URL GUARD_MODEL GUARD_CONFIGMAP GUARD_RULES_NAME GUARD_POLICIES_NAME
  export INJECTION_MODE INJECTION_MODEL_REPO INJECTION_MODEL_COMMIT INJECTION_MODEL_FILE INJECTION_MODEL_SHA256
  export INJECTION_TOKENIZER_FILE INJECTION_TOKENIZER_SHA256 INJECTION_CONFIG_FILE INJECTION_CONFIG_SHA256
  export INJECTION_LABEL INJECTION_THRESHOLD INJECTION_WINDOW INJECTION_TIMEOUT_S INJECTION_BASE_URL
  export SHIELD SHIELD_SECRET SHIELD_PORT SHIELD_THREADS SHIELD_REQ_CPU SHIELD_REQ_MEMORY SHIELD_LIM_CPU SHIELD_LIM_MEMORY
  export COGNEE COGNEE_PORT COGNEE_RECALL COGNEE_RECALL_URL COGNEE_RECALL_DATASET COGNEE_RECALL_LESSONS COGNEE_RECALL_TIMEOUT_S
}

# COGNEE_RECALL and the settings of "seen before". The knowledge agent may reach only the pod
# <APP_NAME>-cognee, on COGNEE_PORT, in this namespace (its network rule), so the URL must name
# that Service; empty, it is http://<APP_NAME>-cognee:8000/mcp. With "off" the values are checked
# all the same.
check_cognee_recall() {
  [[ "$COGNEE_RECALL" == on || "$COGNEE_RECALL" == off ]] || die "COGNEE_RECALL must be on or off. It is: \"$COGNEE_RECALL\""
  COGNEE="$APP_NAME-cognee"
  [[ -n "$COGNEE_RECALL_URL" ]] || COGNEE_RECALL_URL="http://$COGNEE:$COGNEE_PORT/mcp"
  [[ "$COGNEE_RECALL_URL" =~ ^http://([a-z0-9-]+)(\.([a-z0-9-]+)(\.svc(\.cluster\.local)?)?)?:([0-9]+)/mcp$ ]] \
    && [[ "${BASH_REMATCH[1]}" == "$COGNEE" && "${BASH_REMATCH[6]}" == "$COGNEE_PORT" ]] \
    && [[ -z "${BASH_REMATCH[3]}" || "${BASH_REMATCH[3]}" == "$K8S_NAMESPACE" ]] \
    || die "COGNEE_RECALL_URL must look like http://$COGNEE:$COGNEE_PORT/mcp (the Service of the pod $COGNEE in $K8S_NAMESPACE). It is: \"$COGNEE_RECALL_URL\""
  # One dataset name: recall reads a comma-separated list.
  [[ "$COGNEE_RECALL_DATASET" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] \
    || die "COGNEE_RECALL_DATASET must be one dataset name (letters, digits, _ and -), like claude_code_memory. It is: \"$COGNEE_RECALL_DATASET\""
  [[ "$COGNEE_RECALL_LESSONS" =~ ^[1-3]$ ]] || die "COGNEE_RECALL_LESSONS must be 1, 2 or 3. It is: \"$COGNEE_RECALL_LESSONS\""
  # With Grimoire's 30 s, under the 45 s the orchestrator waits for the knowledge agent.
  [[ "$COGNEE_RECALL_TIMEOUT_S" =~ ^([1-9]|10)$ ]] \
    || die "COGNEE_RECALL_TIMEOUT_S must be a whole number of seconds from 1 to 10, like 5. It is: \"$COGNEE_RECALL_TIMEOUT_S\""
}

# INJECTION_MODE and the files of the injection model. With "off" the other settings may be
# empty; a value that is there is checked all the same. Each value goes into a URL or a command
# of the download, so a path may hold only plain names.
check_injection() {
  [[ "$INJECTION_MODE" == off || "$INJECTION_MODE" == warn || "$INJECTION_MODE" == cut ]] \
    || die "INJECTION_MODE must be off, warn or cut. It is: \"$INJECTION_MODE\""
  local path='^[A-Za-z0-9][A-Za-z0-9._-]{0,99}(/[A-Za-z0-9][A-Za-z0-9._-]{0,99}){0,5}$' name
  [[ -z "$INJECTION_MODEL_REPO" || "$INJECTION_MODEL_REPO" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]] \
    || die "INJECTION_MODEL_REPO must be a Hugging Face repo, like Horizon-Labs/prompt-injection-guard-small. It is: \"$INJECTION_MODEL_REPO\""
  [[ -z "$INJECTION_MODEL_COMMIT" || "$INJECTION_MODEL_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
    || die "INJECTION_MODEL_COMMIT must be a commit of the repo: 40 lower-case hex signs. It is: \"$INJECTION_MODEL_COMMIT\""
  [[ -z "$INJECTION_MODEL_FILE" || ("$INJECTION_MODEL_FILE" =~ $path && "$INJECTION_MODEL_FILE" == *.onnx) ]] \
    || die "INJECTION_MODEL_FILE must be a path in the repo that ends with .onnx, like onnx/model_quantized.onnx. It is: \"$INJECTION_MODEL_FILE\""
  [[ "$INJECTION_TOKENIZER_FILE" =~ $path && "$INJECTION_TOKENIZER_FILE" == *.json ]] \
    || die "INJECTION_TOKENIZER_FILE must be a path in the repo that ends with .json, like tokenizer.json. It is: \"$INJECTION_TOKENIZER_FILE\""
  [[ "$INJECTION_CONFIG_FILE" =~ $path && "$INJECTION_CONFIG_FILE" == *.json ]] \
    || die "INJECTION_CONFIG_FILE must be a path in the repo that ends with .json, like config.json. It is: \"$INJECTION_CONFIG_FILE\""
  for name in INJECTION_MODEL_SHA256 INJECTION_TOKENIZER_SHA256 INJECTION_CONFIG_SHA256; do
    [[ -z "${!name}" || "${!name}" =~ ^[0-9a-f]{64}$ ]] || die "$name must be 64 lower-case hex signs. It is: \"${!name}\""
  done
  [[ -z "$INJECTION_LABEL" || "$INJECTION_LABEL" =~ ^[A-Za-z][A-Za-z0-9_-]{0,39}$ ]] \
    || die "INJECTION_LABEL must be a label of config.json, like INJECTION. It is: \"$INJECTION_LABEL\""
  [[ "$INJECTION_THRESHOLD" =~ ^(0\.[0-9]{0,2}[1-9]|1(\.0{1,3})?)$ ]] \
    || die "INJECTION_THRESHOLD must be a number above 0 and at most 1, like 0.95. It is: \"$INJECTION_THRESHOLD\""
  [[ "$INJECTION_WINDOW" =~ ^[1-9][0-9]{2,4}$ ]] && ((INJECTION_WINDOW >= 128 && INJECTION_WINDOW <= 32768)) \
    || die "INJECTION_WINDOW must be a whole number of tokens from 128 to 32768, like 2048. It is: \"$INJECTION_WINDOW\""
  [[ "$INJECTION_TIMEOUT_S" =~ ^[1-9][0-9]{0,2}$ ]] && ((INJECTION_TIMEOUT_S <= 300)) \
    || die "INJECTION_TIMEOUT_S must be a whole number of seconds from 1 to 300, like 30. It is: \"$INJECTION_TIMEOUT_S\""
  split_resources SHIELD_REQUESTS SHIELD_REQ
  split_resources SHIELD_LIMITS SHIELD_LIM
  # One inference uses as many threads as the CPU limit has whole cores (at least one).
  SHIELD_THREADS=$(($(cpu_millis "$SHIELD_LIM_CPU") / 1000))
  ((SHIELD_THREADS >= 1)) || SHIELD_THREADS=1
  INJECTION_BASE_URL="https://huggingface.co/$INJECTION_MODEL_REPO/resolve/$INJECTION_MODEL_COMMIT"
  if [[ "$INJECTION_MODE" != off ]]; then
    [[ "$GUARD_MODE" != off ]] \
      || die "INJECTION_MODE is $INJECTION_MODE, and GUARD_MODE is off: the injection model works inside the Guard. Set INJECTION_MODE=off too, or GUARD_MODE to monitor or enforce."
    for name in INJECTION_MODEL_REPO INJECTION_MODEL_COMMIT INJECTION_MODEL_FILE INJECTION_MODEL_SHA256 \
      INJECTION_TOKENIZER_SHA256 INJECTION_CONFIG_SHA256 INJECTION_LABEL; do
      [[ -n "${!name}" ]] || die "INJECTION_MODE is $INJECTION_MODE, and $name is empty."
    done
  fi
}

# GUARD_MODE and GUARD_TIMEOUT_S. The rules themselves are checked by check_guard_files.
check_guard() {
  [[ "$GUARD_MODE" == enforce || "$GUARD_MODE" == monitor || "$GUARD_MODE" == off ]] \
    || die "GUARD_MODE must be enforce, monitor or off. It is: \"$GUARD_MODE\""
  [[ "$GUARD_TIMEOUT_S" =~ ^[1-9][0-9]{0,2}$ ]] && ((GUARD_TIMEOUT_S <= 300)) \
    || die "GUARD_TIMEOUT_S must be a whole number of seconds from 1 to 300, like 20. It is: \"$GUARD_TIMEOUT_S\""
}

# TELEMETRY and the settings of Tempo and Grafana. With "off" TEMPO_IMAGE and the Grafana
# settings may be empty; a value that is there is checked all the same.
check_telemetry() {
  [[ "$TELEMETRY" == on || "$TELEMETRY" == off ]] || die "TELEMETRY must be on or off. It is: \"$TELEMETRY\""
  [[ -z "$TEMPO_IMAGE" ]] || is_pinned_image "$TEMPO_IMAGE" || die "TEMPO_IMAGE must be pinned by digest: name:tag@sha256:<64 hex>. It is: \"$TEMPO_IMAGE\""
  is_size "$TEMPO_SIZE" || die "TEMPO_SIZE must look like 5Gi or 500Mi. It is: \"$TEMPO_SIZE\""
  # Hours only: the value goes into the config of Tempo (retention, and the longest query).
  [[ "$TEMPO_RETENTION" =~ ^[1-9][0-9]{0,4}h$ ]] || die "TEMPO_RETENTION must be a whole number of hours, like 336h (14 days). It is: \"$TEMPO_RETENTION\""
  [[ -z "$GRAFANA_NAMESPACE" || "$GRAFANA_NAMESPACE" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
    || die "GRAFANA_NAMESPACE is not the name of a namespace: \"$GRAFANA_NAMESPACE\""
  [[ -z "$GRAFANA_SERVICE" || "$GRAFANA_SERVICE" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
    || die "GRAFANA_SERVICE is not the name of a Service: \"$GRAFANA_SERVICE\""
  split_resources TEMPO_REQUESTS TEMPO_REQ
  split_resources TEMPO_LIMITS TEMPO_LIM
  if [[ "$TELEMETRY" == on ]]; then
    [[ -n "$TEMPO_IMAGE" ]] || die "TELEMETRY is on, and TEMPO_IMAGE is empty."
    [[ -n "$GRAFANA_NAMESPACE" ]] || die "TELEMETRY is on, and GRAFANA_NAMESPACE is empty: the network rule of Tempo needs the namespace of Grafana."
    [[ -n "$GRAFANA_SERVICE" ]] || die "TELEMETRY is on, and GRAFANA_SERVICE is empty."
  fi
}

# MODEL_BACKEND and the KubeAI settings. With "sidecar" the KubeAI settings may be empty; a
# value that is there is checked all the same.
check_model_backend() {
  [[ "$MODEL_BACKEND" == sidecar || "$MODEL_BACKEND" == kubeai ]] \
    || die "MODEL_BACKEND must be sidecar or kubeai. It is: \"$MODEL_BACKEND\""
  [[ "$KUBEAI_NAMESPACE" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "KUBEAI_NAMESPACE is not the name of a namespace: \"$KUBEAI_NAMESPACE\""
  # KubeAI puts the Model name into the names of its pods: a short DNS label.
  [[ "$KUBEAI_MODEL" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] \
    || die "KUBEAI_MODEL must be lower-case letters, digits and \"-\", at most 40 long. It is: \"$KUBEAI_MODEL\""
  [[ "$KUBEAI_MIN_REPLICAS" =~ ^[01]$ ]] || die "KUBEAI_MIN_REPLICAS must be 0 or 1. It is: \"$KUBEAI_MIN_REPLICAS\""
  [[ "$KUBEAI_SCALE_DOWN_DELAY_SECONDS" =~ ^[1-9][0-9]{0,4}$ ]] \
    || die "KUBEAI_SCALE_DOWN_DELAY_SECONDS must be a whole number of seconds, like 900. It is: \"$KUBEAI_SCALE_DOWN_DELAY_SECONDS\""
  # The proxy inside the cluster: http://<service>.<KUBEAI_NAMESPACE>[.svc[.cluster.local]][:port]/openai/v1.
  # Only that namespace: the network rule of the triage pod lets it reach no other.
  KUBEAI_SERVICE=""
  if [[ -n "$KUBEAI_BASE_URL" ]]; then
    [[ "$KUBEAI_BASE_URL" =~ ^http://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)\.([a-z0-9-]+)(\.svc(\.cluster\.local)?)?(:[0-9]{1,5})?/openai/v1$ ]] \
      && [[ "${BASH_REMATCH[3]}" == "$KUBEAI_NAMESPACE" ]] \
      || die "KUBEAI_BASE_URL must look like http://kubeai.$KUBEAI_NAMESPACE.svc.cluster.local/openai/v1 (the KubeAI proxy, in KUBEAI_NAMESPACE). It is: \"$KUBEAI_BASE_URL\""
    KUBEAI_SERVICE="${BASH_REMATCH[1]}"
  fi
  # KubeAI's llama.cpp engine downloads the model from Hugging Face itself (-hf <repo>
  # --hf-file <file>): MODEL_URL must then be a file of a Hugging Face repo, at one commit,
  # and that file must be MODEL_FILE. MODEL_HF_REPO and MODEL_HF_COMMIT are its parts.
  MODEL_HF_REPO="" MODEL_HF_COMMIT=""
  if [[ "$MODEL_URL" =~ ^https://huggingface\.co/([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*)/resolve/([0-9a-f]{40})/([^/]+)$ ]] \
    && [[ "${BASH_REMATCH[3]}" == "$MODEL_FILE" ]]; then
    MODEL_HF_REPO="${BASH_REMATCH[1]}" MODEL_HF_COMMIT="${BASH_REMATCH[2]}"
  fi
  if [[ "$MODEL_BACKEND" == kubeai ]]; then
    [[ -n "$KUBEAI_BASE_URL" ]] || die "MODEL_BACKEND is kubeai, and KUBEAI_BASE_URL is empty."
    [[ -n "$MODEL_HF_REPO" ]] \
      || die "MODEL_BACKEND is kubeai: MODEL_URL must be https://huggingface.co/<user>/<repo>/resolve/<commit>/$MODEL_FILE (KubeAI's llama.cpp downloads the file from Hugging Face). It is: \"$MODEL_URL\""
  fi
}

# kubectl in the namespace of KubeAI, always with the context of the settings.
kubeai_k() {
  kubectl --context "$K8S_CONTEXT" -n "$KUBEAI_NAMESPACE" "$@"
}

# kubectl, always with the context and the namespace of the settings.
k() {
  kubectl --context "$K8S_CONTEXT" -n "$K8S_NAMESPACE" "$@"
}

# kubectl in the namespace of Grafana, always with the context of the settings.
grafana_k() {
  kubectl --context "$K8S_CONTEXT" -n "$GRAFANA_NAMESPACE" "$@"
}

# One value of the Secret, on standard output. Only ever used inside $(...), a pipe or a
# redirect into a file: never printed.
secret_value() {  # KEY
  k get secret "$APP_NAME" -o "jsonpath={.data.$1}" | base64 -d
}

# The names of the keys of a Secret, one per line. Never a value.
secret_keys() {  # SECRET
  # shellcheck disable=SC2016  # a Go template, not a shell expression
  k get secret "$1" -o go-template='{{range $key, $value := .data}}{{$key}}{{"\n"}}{{end}}'
}

# Where the CLI looks for its settings (src/wattson/cli/config.py): the same folder.
cli_config_dir() {
  printf '%s' "${WATTSON_CONFIG_DIR:-$HOME/.config/wattson}"
}

# ---- Grafana's HTTP API (k8s/grafana.sh adds to it, k8s/doctor.sh only reads) ---------
# The admin login comes from the Secret that the Grafana Deployment (named like its Service)
# names in GF_SECURITY_ADMIN_USER and GF_SECURITY_ADMIN_PASSWORD. It is kept in GRAFANA_LOGIN
# as a curl config line (an Authorization header) and goes to curl on its standard input:
# never on a command line, never on screen, never on disk.

GRAFANA_LOGIN="" GRAFANA_URL="" GRAFANA_FORWARD_PID=""

# What k8s/grafana.sh puts in Grafana. The dashboard file names the data source by its uid.
# Grafana keeps folders and dashboards in one table: the folder has a uid of its own.
GRAFANA_DATASOURCE_UID=wattson-tempo GRAFANA_DATASOURCE_NAME="Wattson Tempo"
GRAFANA_DASHBOARD_UID=wattson GRAFANA_DASHBOARD_TITLE="Wattson Agent"
GRAFANA_FOLDER_UID=wattson-folder GRAFANA_FOLDER_TITLE=wattson

# Where the Grafana Deployment takes its admin login from: "SECRET KEY" for the user, then the
# same for the password, one per line. Names only, never a value. Returns 2 when the login is
# not taken from a Secret.
grafana_login_refs() {
  local refs user_ref password_ref
  refs="$(grafana_k get deployment "$GRAFANA_SERVICE" \
    -o 'jsonpath={range .spec.template.spec.containers[*].env[*]}{.name}{" "}{.valueFrom.secretKeyRef.name}{" "}{.valueFrom.secretKeyRef.key}{"\n"}{end}')" || return 1
  user_ref="$(awk '$1 == "GF_SECURITY_ADMIN_USER" && NF == 3 { print $2 " " $3; exit }' <<<"$refs")"
  password_ref="$(awk '$1 == "GF_SECURITY_ADMIN_PASSWORD" && NF == 3 { print $2 " " $3; exit }' <<<"$refs")"
  [[ -n "$user_ref" && -n "$password_ref" ]] || return 2
  printf '%s\n%s\n' "$user_ref" "$password_ref"
}

# One value of a Secret in the namespace of Grafana, on standard output. Only ever used inside
# $(...): never printed.
grafana_secret_value() {  # SECRET KEY
  grafana_k get secret "$1" -o "jsonpath={.data.${2//./\\.}}" | base64 -d
}

# Reads the admin login of Grafana into GRAFANA_LOGIN. Returns 1 when kubectl could not read
# it, 2 when the Deployment does not take it from a Secret.
grafana_read_login() {
  local refs user_secret user_key password_secret password_key user password
  refs="$(grafana_login_refs)" || return $?
  { read -r user_secret user_key && read -r password_secret password_key; } <<<"$refs" || return 1
  user="$(grafana_secret_value "$user_secret" "$user_key")" || return 1
  password="$(grafana_secret_value "$password_secret" "$password_key")" || return 1
  [[ -n "$user" && -n "$password" ]] || return 1
  # printf is a shell builtin: the values go to base64 through a pipe, not on a command line.
  GRAFANA_LOGIN="header = \"Authorization: Basic $(printf '%s:%s' "$user" "$password" | base64 | tr -d '\n')\""
}

# kubectl port-forward to the Service of Grafana, on a free port of 127.0.0.1, in the
# background. Sets GRAFANA_URL and GRAFANA_FORWARD_PID; grafana_stop ends it.
grafana_forward() {  # LOG_FILE
  local remote_port local_port _
  remote_port="$(grafana_k get service "$GRAFANA_SERVICE" -o 'jsonpath={.spec.ports[0].port}')" || return 1
  [[ "$remote_port" =~ ^[0-9]{1,5}$ ]] || return 1
  local_port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  # kubectl itself in the background (not the function grafana_k): $! is then kubectl's own
  # process, and grafana_stop ends it.
  kubectl --context "$K8S_CONTEXT" -n "$GRAFANA_NAMESPACE" port-forward --address 127.0.0.1 "service/$GRAFANA_SERVICE" "$local_port:$remote_port" >"$1" 2>&1 &
  GRAFANA_FORWARD_PID=$!
  GRAFANA_URL="http://127.0.0.1:$local_port"
  for _ in {1..50}; do
    grep -q '^Forwarding from' "$1" 2>/dev/null && return 0
    kill -0 "$GRAFANA_FORWARD_PID" 2>/dev/null || return 1
    sleep 0.3
  done
  return 1
}

grafana_stop() {
  if [[ -n "$GRAFANA_FORWARD_PID" ]]; then kill "$GRAFANA_FORWARD_PID" 2>/dev/null || true; fi
  GRAFANA_FORWARD_PID=""
}

# One call to Grafana's HTTP API, with the login on curl's standard input. The answer goes to
# the file ANSWER; the HTTP status is printed ("000" when Grafana did not answer).
grafana_call() {  # METHOD PATH ANSWER [BODY_FILE]
  local status options=(--silent --max-time 30 --request "$1" --output "$3" --write-out '%{http_code}' --header 'Accept: application/json')
  if [[ -n "${4:-}" ]]; then options+=(--header 'Content-Type: application/json' --data-binary "@$4"); fi
  status="$(printf '%s\n' "$GRAFANA_LOGIN" | curl --config - "${options[@]}" "$GRAFANA_URL$2" 2>/dev/null)" || true
  [[ "$status" =~ ^[0-9]{3}$ ]] || status=000
  printf '%s' "$status"
}

# The "message" of an answer of Grafana (a JSON file), or its first 200 signs.
grafana_message() {  # ANSWER
  python3 -c '
import json, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
try:
    data = json.loads(text)
    print(data.get("message") or data.get("error") or text[:200] if isinstance(data, dict) else text[:200])
except ValueError:
    print(text[:200].replace("\n", " "))' "$1" 2>/dev/null || true
}

# ---- the rules of the Guard (k8s/guard/rules.yaml and k8s/guard/policies.cedar) ---------------
# deploy.sh and guard.sh check the two files with check_guard_files, then put their exact text in
# the ConfigMap <APP_NAME>-guard. The pod of the orchestrator carries GUARD_CONFIG_SHA256, the
# SHA-256 of the two files: a change of the rules starts a new pod.

# The Python that checks the rules: GUARD_PYTHON, else the project's own (.venv: it has PyYAML,
# cedarpy and the code of the Guard), else python3.
guard_python() {
  if [[ -n "${GUARD_PYTHON:-}" ]]; then
    printf '%s' "$GUARD_PYTHON"
  elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    printf '%s' "$ROOT/.venv/bin/python"
  else
    printf '%s' python3
  fi
}

# The checks of the two files. It prints "NOTE: ..." lines, and "PROBLEM: ..." lines; it ends
# with 1 when there is a problem. With the project's Python it runs the code of the Guard itself
# (src/wattson/agents/orchestrator/guard: parse_rules, compile_rule, load_policies): the checks the
# orchestrator makes when it starts, and every "examples" and "not_examples" of each rule. Without
# it, the same checks written again here (and Cedar only when cedarpy is there). The orchestrator
# uses its packaged default rules when a file is wrong: this check stops that before a deploy.
# shellcheck disable=SC2016
GUARD_CHECK='import re, sys
rules_path, policies_path, max_bytes = sys.argv[1], sys.argv[2], int(sys.argv[3])
problems, notes = [], []
LABELS = ("malicious", "risky", "out_of_scope", "injection")
WHERE = ("request", "evidence", "both")
FIELDS = {"name", "label", "applies_to", "match", "regex", "unless", "reason", "enabled", "examples", "not_examples"}

def read(path):
    try:
        data = open(path, "rb").read()
    except OSError as error:
        problems.append("%s cannot be read: %s" % (path, error.strerror))
        return None
    if not data.strip():
        problems.append("%s is empty" % path)
        return None
    if len(data) > max_bytes:
        problems.append("%s has %d bytes; at most %d" % (path, len(data), max_bytes))
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        problems.append("%s is not UTF-8 text" % path)
        return None
    for number, line in enumerate(text.split("\n"), 1):
        bad = [c for c in line if (ord(c) < 32 and c not in "\t\r") or 0x7F <= ord(c) <= 0x9F or ord(c) > 0xFFFF]
        if bad:
            problems.append("%s, line %d: a sign that cannot go in a ConfigMap (U+%04X): remove it" % (path, number, ord(bad[0])))
            return None
    return text

def short(text, limit=60):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."

def phrase_regex(phrase):
    # As the Guard reads a phrase: whole words, any whitespace between them.
    words = [re.escape(w) for w in phrase.split()]
    head = r"(?<!\w)" if re.match(r"\w", phrase) else ""
    tail = r"(?!\w)" if re.search(r"\w$", phrase) else ""
    return head + r"\s+".join(words) + tail

def texts(rule, key, who):
    value = rule.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append("%s: %s must be a list of texts" % (who, key))
        return None
    return value

def check_rules_here(text):
    try:
        import yaml
    except ImportError:
        problems.append("PyYAML is not installed for %s, so the rules cannot be checked. Run: uv sync" % sys.executable)
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        problems.append("%s is not valid YAML%s" % (rules_path, " (line %d)" % (mark.line + 1) if mark else ""))
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list) or not data["rules"]:
        problems.append("%s must be a mapping with a rules list (rules: then one \"- name: ...\" per rule)" % rules_path)
        return None
    if set(data) - {"version", "rules"} or data.get("version", 1) != 1:
        problems.append("%s: only version: 1 and rules: may be at the top" % rules_path)
    names, rules = set(), []
    for number, rule in enumerate(data["rules"], 1):
        who = "rule %d" % number
        if not isinstance(rule, dict):
            problems.append("%s: a rule must be a mapping (name: ..., label: ..., ...)" % who)
            continue
        before = len(problems)
        name = rule.get("name")
        if isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
            who = "rule %d (%s)" % (number, name)
            if name in names:
                problems.append("%s: the rule name is used twice" % who)
            names.add(name)
        else:
            problems.append("%s: name must be lower-case letters, digits and - (it is %r)" % (who, name))
        if set(rule) - FIELDS:
            problems.append("%s: unknown field(s): %s" % (who, ", ".join(sorted(set(rule) - FIELDS))))
        if rule.get("label") not in LABELS:
            problems.append("%s: label must be one of %s" % (who, ", ".join(LABELS)))
        if rule.get("applies_to", "request") not in WHERE:
            problems.append("%s: applies_to must be one of %s" % (who, ", ".join(WHERE)))
        match, regex, unless = (texts(rule, key, who) for key in ("match", "regex", "unless"))
        examples, not_examples = texts(rule, "examples", who), texts(rule, "not_examples", who)
        if any(not " ".join(v.split()) for v in (match or []) + (unless or [])):
            problems.append("%s: an empty phrase" % who)
        for index, pattern in enumerate(regex or [], 1):
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as error:
                problems.append("%s: regex %d is not valid: %s" % (who, index, error.msg))
        if match is not None and regex is not None and not match and not regex:
            problems.append("%s: a rule needs at least one match phrase or regex" % who)
        reason = rule.get("reason")
        if not isinstance(reason, str) or not 3 <= len(reason) <= 300:
            problems.append("%s: reason must be one plain sentence, 3 to 300 signs" % who)
        if not isinstance(rule.get("enabled", True), bool):
            problems.append("%s: enabled must be true or false" % who)
        if len(problems) > before:
            continue
        pattern = re.compile("|".join([phrase_regex(m) for m in match] + ["(?:%s)" % r for r in regex]), re.IGNORECASE | re.MULTILINE)
        skip = re.compile("|".join(phrase_regex(u) for u in unless), re.IGNORECASE) if unless else None
        def hits(text, pattern=pattern, skip=skip):
            return not (skip and skip.search(text)) and pattern.search(text) is not None
        rules.append((who, rule, hits, examples, not_examples))
    return rules

def check_rules_with_the_guard(text, guard):
    try:
        ruleset = guard.parse_rules(text, rules_path)
    except guard.RulesError as error:
        problems.append(str(error))
        return None
    out = []
    for number, rule in enumerate(ruleset.file.rules, 1):
        compiled = guard.compile_rule(rule)
        out.append(("rule %d (%s)" % (number, rule.name), rule.model_dump(), compiled.matches, rule.examples, rule.not_examples))
    return out

try:
    from wattson.agents.orchestrator.guard import rules as guard_rules
    from wattson.agents.orchestrator.guard import cedar as guard_cedar
except ImportError:
    guard_rules = guard_cedar = None
except Exception as error:
    guard_rules = guard_cedar = None
    notes.append("the code of the Guard could not be loaded here (%s): the checks of this script ran instead" % type(error).__name__)

text = read(rules_path)
if text is not None:
    rules = check_rules_with_the_guard(text, guard_rules) if guard_rules else check_rules_here(text)
    before = len(problems)
    for who, rule, hits, examples, not_examples in rules or []:
        for example in examples:
            if not hits(example):
                problems.append("%s: it does not match its example \"%s\"" % (who, short(example)))
        for example in not_examples:
            if hits(example):
                problems.append("%s: it matches its not_example \"%s\"" % (who, short(example)))
    if rules:
        on = [rule for _, rule, _, _, _ in rules if rule.get("enabled", True)]
        labels = sorted({r["label"] for r in on})
        counts = ", ".join("%s %d" % (label, sum(1 for r in on if r["label"] == label)) for label in labels)
        notes.append("%d rules, %d on (%s)" % (len(rules), len(on), counts))
    if guard_rules and rules and len(problems) == before:
        notes.append("the code of the Guard reads them, and every example and not_example is right")

text = read(policies_path)
if text is not None:
    if guard_cedar:
        _, problem = guard_cedar.load_policies(policies_path)
        if problem:
            problems.append(problem.split("; using the packaged default")[0])
        else:
            notes.append("the Cedar policies can be read")
    else:
        try:
            import cedarpy
        except ImportError:
            notes.append("the Cedar policies were not checked (cedarpy is not installed for %s)" % sys.executable)
        else:
            try:
                cedarpy.format_policies(text)
                notes.append("the Cedar policies can be read")
            except ValueError as error:
                problems.append("%s is not a valid Cedar policy: %s" % (policies_path, error))

for line in notes:
    print("NOTE: " + line)
for line in problems:
    print("PROBLEM: " + line)
sys.exit(1 if problems else 0)'

# Checks the two files of GUARD_DIR; stops the script when one is wrong. Then sets
# GUARD_CHECK_NOTES (what the checks found) and GUARD_CONFIG_SHA256.
check_guard_files() {
  local status=0
  [[ -f "$GUARD_DIR/$GUARD_RULES_NAME" && -f "$GUARD_DIR/$GUARD_POLICIES_NAME" ]] \
    || die "The rules of the Guard are missing: $GUARD_DIR must hold $GUARD_RULES_NAME and $GUARD_POLICIES_NAME."
  GUARD_CHECK_NOTES="$("$(guard_python)" -I -c "$GUARD_CHECK" "$GUARD_DIR/$GUARD_RULES_NAME" "$GUARD_DIR/$GUARD_POLICIES_NAME" "$GUARD_FILE_MAX_BYTES" 2>&1)" || status=$?
  if [[ "$status" -ne 0 ]]; then
    printf '%s\n' "$GUARD_CHECK_NOTES" >&2
    die "The rules of the Guard ($GUARD_DIR) are not right (see above). Nothing was changed."
  fi
  GUARD_CONFIG_SHA256="$(guard_config_sha256)"
  export GUARD_CONFIG_SHA256
}

# The SHA-256 of the two files (with their names).
guard_config_sha256() {
  (cd "$GUARD_DIR" && sha256sum -- "$GUARD_RULES_NAME" "$GUARD_POLICIES_NAME") | sha256sum | cut -c1-64
}

# The ConfigMap <APP_NAME>-guard with the exact text of the two files, as JSON (a JSON
# document is a YAML document too: kubectl apply takes it, next to the others).
guard_configmap() {
  python3 -I -c '
import json, sys
name, app, *files = sys.argv[1:]
data = {}
for key, path in zip(files[0::2], files[1::2]):
    with open(path, encoding="utf-8", newline="") as file:
        data[key] = file.read()
print(json.dumps({
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": app, "app.kubernetes.io/component": "orchestrator"}},
    "data": data,
}, indent=2))' "$GUARD_CONFIGMAP" "$APP_NAME" "$GUARD_RULES_NAME" "$GUARD_DIR/$GUARD_RULES_NAME" "$GUARD_POLICIES_NAME" "$GUARD_DIR/$GUARD_POLICIES_NAME"
}
