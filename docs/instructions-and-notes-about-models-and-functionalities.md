# Models in Wattson: what they are, what each one does, how to change them

Status: 27 Oct 2026. A short guide to the kinds of models, the models Wattson uses, where they
run, how we measure them, and the options for bigger ones. Plain words first; the technical names
are in brackets, so you can look them up.

## 1. Four kinds of models

Not every "AI model" does the same job. The kind decides what it can do, how big it is, and which
program can run it.

| Kind | What goes in | What comes out | Can follow instructions? | Typical size | Example in Wattson |
|---|---|---|---|---|---|
| **Chat model** (generative LLM) | text | new text, one piece (token) at a time | **yes**: you can ask it anything, e.g. "answer with this JSON" | 1–30 billion parameters, 1–60 GB | Granite-4.2-3B (triage, the Guard's request check) |
| **Safety chat model** (guard LLM) | text + the risk to check | a short verdict ("yes" / "no", a label) | yes, but trained for one job: judging risk | 8–20 billion | none yet (candidates: Granite Guardian 4.1 8B, gpt-oss-safeguard-20b) |
| **Classifier** (encoder) | text | **a score per label**, e.g. INJECTION 0.97 / SAFE 0.03 | **no**: it only answers the one question it was trained on | 0.1–0.6 billion, 0.1–1 GB | Horizon-small (the shield, prompt injection) |
| **Embedding model** | text | a list of numbers (to find similar texts) | no | 0.1–1 billion | none in Wattson (search engines like Grimoire use them) |

In one sentence each:

- A **chat model** *writes*. It is flexible, but big and slow on CPU, and it can be talked into
  things (that is why the Guard lets it only make a verdict *stricter*).
- A **safety chat model** is a chat model trained to be a judge.
- A **classifier** *scores*. It cannot write, chat or be argued with; it is small, fast on CPU,
  and very good at its one question. It is the right tool for "is this text an injection?".
- An **embedding model** *compares*. Not needed by Wattson today.

## 2. The models Wattson uses today

```
your request ──► orchestrator (my-cluster, namespace grimoire)
                   │
                   ├─ the Guard, request check ── 1. your rules (k8s/guard/rules.yaml)
                   │                              2. Granite-4.2-3B ──────────► KubeAI (namespace kubeai)
                   │
                   ├─ triage ───────────────────── Granite-4.2-3B ──────────► KubeAI (the same model)
                   │
                   ├─ evidence + Grimoire docs ─── 1. your rules (line by line)
                   │                              2. Horizon-small ────────► wattson-shield (namespace grimoire)
                   │
                   └─ the approved prompt ──────── Claude Code on your laptop (Anthropic's model; Wattson does
                                                   not host it, it only writes the prompt for it)
```

### Granite-4.2-3B (IBM): the chat model

| | |
|---|---|
| Jobs | **triage**: which workflow and which Wattson procedure fits the request; **the Guard**: a second opinion on the request (work / risky / out of scope / malicious) |
| File | `granite-4.2-3b-Q4_K_M.gguf`, 2.24 GB, pinned in `settings.env` (`MODEL_URL`, `MODEL_SHA256`) |
| Runs in | KubeAI, namespace `kubeai`, with its llama.cpp engine, on CPU (`cpu-burst:4`: 3 CPU and 3 GiB reserved, 8 CPU and 6 GiB at most; 7 threads read the prompt) |
| When idle | goes to 0 pods after 1 hour (`KUBEAI_MIN_REPLICAS=0`, `KUBEAI_SCALE_DOWN_DELAY_SECONDS=3600`); the next request waits about a minute for it to start |
| Context | 4,096 tokens (`LLAMA_CTX`); the model could take 131,072, but on CPU more context costs memory and time |
| "Thinking" | Granite 4.2 thinks before it answers by default. The server allows it (`--reasoning on`), but triage and the Guard switch it off in each request (`enable_thinking: false`): thinking would take about 20 s per call |
| Speed | the Guard's call: about 9 s (p50 8.7 s), at most 20 s (`GUARD_TIMEOUT_S`), then the rules decide alone |
| Measured | the Guard with it: risky caught 80 % (70 % with the rules alone), no normal work stopped. Triage was measured on the older 1B (91.7 %); **the 3B is not measured yet for triage** (`eval/run.py`). The procedure pick: 54/68 right (79 %, D42); a whole triage with procedures takes about 70–90 s, most of it the hint call |

### Horizon-small (Horizon-Labs): the classifier

| | |
|---|---|
| Job | scores each evidence item, each Grimoire section and your request: is it an attempt to give orders to the agent (prompt injection)? On your request, in `enforce`, a high score makes the Guard ask first (D39) |
| File | `prompt-injection-guard-small`, INT8 ONNX, 268 MB, Apache-2.0, built on mmBERT-small (141 million parameters) |
| Runs in | its own small pod, `wattson-shield`, in namespace `grimoire`; **not in KubeAI**: KubeAI runs chat models, embeddings and speech, not classifiers in ONNX |
| Memory | about 0.5 GiB loaded, 0.8 GiB at peak; always on (it is small, and so there is no wake-up wait) |
| Threshold | 0.95: a part counts as an injection only when its score is at least 0.95 |
| Mode | `cut` since 27 Oct 2026, your choice (D40): a flagged part is removed before the prompt and the review names it; `warn` keeps it and only names it (`INJECTION_MODE`) |
| Measured (study) | hard injections caught 96 % (the rules alone: 24 %); hard normal DevOps text flagged 20 % at 0.9, 10 % at 0.95; one task's evidence and documents: 3–8 s on 2 CPU. See `docs/study/guardrails.md` |

The shield is live since 27 Oct 2026 (commit `d7aea30`): its own image, the model files downloaded
when the pod starts (a pinned commit, each SHA-256 checked), 1 CPU / 768 Mi reserved, up to 6 CPU /
2 GiB (6 threads; it was 2, see D38 in `docs/decisions.md`). When it is down or slow (it waits up to
300 s, `INJECTION_TIMEOUT_S`), the rules alone check, and the panel counts it as "did not answer".
First scores on the cluster: real `git log` and `kubectl get` output 0.01–0.06, the two injection
strings 0.999; but short texts *about* guard rules scored 0.91–0.995. In `cut` (since 27 Oct
2026), such a false flag is removed from the prompt: the review names it, and it goes into
`eval/injection.jsonl`.

### Not models, but part of the same checks

- **Your rules** (`k8s/guard/rules.yaml`): words, phrases and regular expressions. They decide
  first, in about a millisecond, and the models can only make their verdict stricter.
- **Cedar policies** (`k8s/guard/policies.cedar`): a lock on the order of the steps. Claude Code
  runs only after you approved that exact prompt.

## 3. Words you will meet

| Word | Meaning |
|---|---|
| **parameter** | one number inside the model. "3B" = 3 billion of them. More parameters: usually smarter, always bigger and slower |
| **token** | the piece of text a model reads or writes: about ¾ of an English word, or about 4 characters |
| **context (window)** | how many tokens the model reads at once: the request, the instructions, the documents. Longer context = more memory |
| **quantization** | storing each parameter with fewer bits, so the file is smaller and faster, and a little less precise. `Q4_K_M` ≈ 4–5 bits (GGUF); `INT8` = 8 bits; `FP8`, `MXFP4`, `NVFP4` = GPU formats; `BF16` = the original 16 bits |
| **GGUF** | the file format of llama.cpp (CPU friendly). What KubeAI runs here |
| **ONNX** | a file format for small models, run by ONNX Runtime. What the shield runs |
| **safetensors** | the original weights from the maker (usually BF16). What GPU servers (vLLM, SageMaker) load |
| **MLX** | Apple Silicon only; not for us |
| **llama.cpp** | the program that runs GGUF chat models on CPU. KubeAI's engine here |
| **vLLM** | the program that runs chat models fast on GPUs |
| **KubeAI** | a Kubernetes operator: it runs chat models as pods behind one OpenAI-style address and stops them when idle |
| **threshold** | for a classifier: the score from which it says "yes". Higher = fewer false alarms, more misses |
| **recall / caught** | of the real attacks, the share it found |
| **false positive** | a normal text it wrongly flagged |
| **p50 / p95** | the typical time (half are faster) / the slow end (95 % are faster) |
| **cold start** | the wait when a model was stopped and must start and load again |
| **scale to zero** | no pod (or no cloud machine) when nobody asks, so no CPU and no money used |

How big is a model in memory? About **parameters × bytes per parameter**:

| Model | BF16 (2 bytes) | 8-bit (1 byte) | 4-bit (~0.6 byte) |
|---|---|---|---|
| Granite-4.2-3B | ~6 GB | ~3 GB | ~2.2 GB (our file) |
| Granite-4.2-8B / Guardian 4.1 8B | ~17 GB | ~9 GB | ~5 GB |
| Granite-4.2-30B | ~58 GB | ~29 GB | ~15 GB |
| Horizon-small | ~0.3 GB | ~0.27 GB (our file) | — |

Plus room for the context (on top of the file), which grows with every token.

## 4. CPU or GPU

- On **CPU** (our cluster: 5 nodes, 8 CPU and 24 GB each, no GPU), a chat model spends most of
  its time *reading* the prompt; writing is limited by memory speed. A 3B answers in seconds,
  an 8B in about 2–3 times that, a 30B is too slow to be practical.
- On a **GPU**, the same calls take well under a second, and a 30B with a long context is
  possible. But a GPU costs money while it runs, and a stopped one takes minutes to wake up.
- **Classifiers** like Horizon-small do not need a GPU.

Your requirement AC-12 says the decision model runs in the cluster without a GPU. Using a GPU
(below) is a decision to record in `docs/decisions.md` first.

## 5. How we measure them

Always on our own cases, never on the vendors' claims. Short runs.

| What | Script | Cases | Run it |
|---|---|---|---|
| The Guard (rules, rules + model) | `eval/guard.py` | `eval/guard.jsonl`: 62 requests + 10 evidence texts | `kubectl --context my-cluster -n kubeai port-forward svc/kubeai 18086:80`, then `uv run python eval/guard.py` (or `--rules-only`, no model) |
| Injection classifiers (any model) | `eval/injection_model.py` | `eval/injection.jsonl` (45 cases) + normal DevOps texts | `uv run python eval/injection_model.py --help`; any model as `--model NAME DIR ONNX WINDOW` |
| Triage | `eval/run.py` | `eval/requests.jsonl` (72) | see its first lines; it reports accuracy, confidence error (ECE), time |
| The procedure pick | `eval/pick.py` | `eval/procedures.jsonl` (68 cases: 30 from past sessions) | see its first lines; it runs the whole triage |

Results go to `eval/results/`. Granite is measured on **production** (through the port-forward),
never on a copy on the laptop.

## 6. How to change a model

| Change | Where | Then |
|---|---|---|
| the chat model (another Granite, another GGUF) | `settings.env`: `MODEL_URL` (pinned commit), `MODEL_FILE`, `MODEL_SHA256`, `KUBEAI_MODEL`; the flags in `k8s/kubeai/granite-model.yaml` | `bash k8s/kubeai.sh --apply` (type `my-cluster`), then `BUILD_NETWORK=host bash k8s/deploy.sh` if `KUBEAI_MODEL` changed; then measure (`eval/guard.py`, `eval/run.py`) |
| the injection classifier (Horizon ↔ Wolf, or another) | `settings.env`: `INJECTION_MODEL_REPO`, `INJECTION_MODEL_COMMIT`, `INJECTION_MODEL_FILE`, `INJECTION_MODEL_SHA256`, the tokenizer lines, `INJECTION_THRESHOLD`, `INJECTION_MODE` | commit, `BUILD_NETWORK=host bash k8s/deploy.sh`; measure first with `eval/injection_model.py` |
| the Guard's rules | `k8s/guard/rules.yaml`, `k8s/guard/policies.cedar` | `bash k8s/guard.sh --apply` (no new image) |
| the Guard's mode (`enforce`, `monitor`, `off`) | `settings.env`: `GUARD_MODE` | commit, `BUILD_NETWORK=host bash k8s/deploy.sh` |

The README section "Change a setting" has the full table.

Notes from what we measured:

- **Wolf Defender**: only its `fp16` small file behaves as its card says; its INT8 and "mixed"
  files give wrong scores in plain ONNX Runtime. Use the fp16 small one if you switch.
- **Horizon** cleans hidden-character tricks itself (zero-width, Unicode tags, full-width), in its
  tokenizer; it does not catch look-alike letters.
- **ProtectAI v2** flagged a third of the normal DevOps texts: not usable for us.
- **Llama Prompt Guard 2** needs Meta's approval to download: not measured.

## 7. Options for bigger models (not built; your decision)

| Option | What | Cost | Catch |
|---|---|---|---|
| **Granite Guardian 4.1 8B in KubeAI** | IBM's safety model, official GGUF (`ibm-granite/granite-guardian-4.1-8b-GGUF`), as a second KubeAI Model for the Guard | $0, stays in the cluster | on CPU about 20–25 s per call (estimate, to measure); the 8B needed 6–8 CPU before |
| **SageMaker** (AWS) | your own model (Granite, Guardian) on a GPU endpoint that goes to 0 machines; Strands already has `SageMakerAIModel` | about $1.13/h for ml.g6.xlarge (L4) while it runs, $0 idle | minutes to wake up, and requests fail meanwhile (the rules decide); the text goes to AWS |
| **Bedrock** (AWS) | pay per token: gpt-oss-safeguard-20b; Bedrock Guardrails (prompt attacks, denied topics) on any text | cents for our evals | the text goes to AWS |
| **Groq** free tier | gpt-oss-safeguard-20b, Llama Prompt Guard 2 | $0 within its limits | public service; limits change |
| **Modal** | your own model on a GPU, scales to 0 | $30 free credits a month | public service |
| **A GPU node on my-cluster** | an AWS GPU machine that joins the cluster only when needed (Auto Scaling Group from 0, Cluster Autoscaler) | about $0.17–0.29/h on spot, only while it runs | a VPN, and the company's OK: an outside machine joins the shared cluster |

Biggest IBM models today: Granite-4.2-30B (29.3 billion parameters, context 131,072; needs a 48 GB
GPU in 8-bit), Granite-4.2-8B, Granite Guardian 4.1 8B. For tests, use only the made-up eval cases,
never real tasks, with any outside service.

## 8. What goes to the traces (OpenTelemetry)

The three agents send a trace of each task to Tempo (`wattson-tempo`), and the Grafana dashboard
"Wattson Agent" reads it. **Only ids, names, labels, numbers and statuses**: never the request,
the evidence, the documents or the prompt (Strands' own content fields are replaced by
`[REDACTED]`, and every other string is redacted again).

| What | Span (attributes) | Grafana panel |
|---|---|---|
| each task's end | `wattson.task.end` (outcome, workflow, duration) | Tasks by outcome; Tasks per 15 minutes; Recent tasks |
| each step, and your waiting time | `wattson.step` (step, status, interrupt, wait) | Time waiting for you (the time of each step: in the trace view; its panel was removed on 27 Oct) |
| triage | `wattson.triage` (category, confidence, rules or model) | Who decided the kind of task; Triage confidence |
| Granite's call for triage | `wattson.model.classify` (model name, ok) | How long the model takes |
| Grimoire | `wattson.knowledge.retrieve`, `wattson.grimoire.call` (tool, ok, how many docs) | Grimoire calls: ok and failed |
| the Guard, request check | `wattson.guard.request` (label, rules or rules+model, mode, how many rules hit, time) | Guard decisions, by label; Blocked and confirmed tasks |
| the Guard, evidence check | `wattson.guard.evidence` (parts removed, the names of the rules that cut) | Evidence parts removed |
| the shield (Horizon-small) | on `wattson.guard.evidence`: parts checked, parts flagged, mode, model name, time, ok; on `wattson.guard.request`: flagged, score, time, ok | Injection model: parts flagged (with "Requests flagged") |
| every HTTP call of the agents | HTTP client spans (address without query string, status, time): also the calls to KubeAI and Grimoire | (in the trace view) |

**Not in the traces:**

- **Inside the model pods**: the KubeAI pod (llama.cpp) has a metrics page (`--metrics`), but
  nothing collects it: there is no Prometheus here, only Tempo. So tokens per second, the queue
  and the memory of the model pod are not in Grafana. The *time* of each model call is (above).
- **The laptop side**: the CLI and Claude Code send nothing; the orchestrator records only how
  long you and the run took.
- **Any text**: by design.

New panels come with `bash k8s/grafana.sh --apply` (run it again after the monitoring Grafana
restarts). New data shows in the panels a few minutes late.

## 9. Where to read more

- `docs/study/guardrails.md`: the injection-model study (numbers, design, how to switch models)
- `running.md`, "The Guard": modes, rules, tests, Grafana
- `docs/decisions.md`: why each model was chosen
- `README.md`, "The model" and "Change a setting"
