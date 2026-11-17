# The image of cognee-mcp, the MCP server of Cognee (AI memory, Apache-2.0,
# github.com/topoteretes/cognee), for k8s/cognee.sh. Not Wattson's image: that one is the
# Dockerfile at the root. The build context is this folder (k8s/cognee), nothing else.
#
# Why our own image: the official one (docker.io/cognee/cognee-mcp) has no version tags (only
# "latest", "main" and "main-<commit>"), installs cognee 1.6.3 (the lock of cognee-mcp at the
# tag v1.6.5), carries the docs, Postgres and Neo4j extras (unstructured, about 1 GB packed),
# has no GLiNER, downloads the models at the first request, and its entrypoint can
# "uv pip install" more packages at start (EXTRAS).
#
# What is in this one, all pinned:
#   - Python 3.12 (the version of the official image) and uv 0.13.5, by digest;
#   - the Python packages of uv.lock, exactly: cognee 1.6.5 with the "gliner" extra (gliner2,
#     torch for the CPU), fastembed, fastmcp 3.4.6, mcp 1.28.1;
#   - the MCP server: the six files of cognee-mcp/src at the tag v1.6.5 (commit 37f788bd),
#     each checked by its SHA-256, installed as the wheel of cognee-mcp would install them;
#   - the model of models.txt (bge-small-en-v1.5, the embeddings), checked by SHA-256, and
#     the two tiktoken tables, so the pod downloads nothing;
#   - the JSON extension of the graph store (Ladybug), which the cognee wheel bundles.
#   - the start file of Cognee's API server (api_start.py: it stores the API key of the MCP
#     container, then serves the API; the pod runs it in the container "api").
# It runs as user 10001 and writes only to /tmp and /data (volumes in the pod). The pod runs this
# image twice: as the API server (the only process that opens the stores) and as cognee-mcp in
# API mode (it calls the API server; it has no volume).
#
#   docker build --platform linux/amd64 -t wattson-cognee k8s/cognee      (k8s/cognee.sh does this)
# The version, for the image tag (k8s/cognee.sh reads this line):
ARG COGNEE_VERSION=1.6.5

# python:3.12-slim (3.12.16, Debian trixie) and uv 0.13.5, pinned by digest.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff

FROM ${UV_IMAGE} AS uv

# ---- build: the locked packages into /app/.venv, then the MCP server -------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_CACHE=1
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

# cognee-mcp/src at the tag v1.6.5, file by file, each checked by its SHA-256.
ARG MCP_SRC=https://raw.githubusercontent.com/topoteretes/cognee/37f788bd287750e10d7610173e5332b3843f6ebd
ADD --chmod=644 --checksum=sha256:7440277fdf566c889e4fe9043cdd8c826299104e6a64a0b54bab3c7f4a7a9723 ${MCP_SRC}/cognee-mcp/src/__init__.py /src/mcp/src/__init__.py
ADD --chmod=644 --checksum=sha256:a6daa543bad908ccdf497eca5111e255dacd1cff742b255fe5d77d9b6499018a ${MCP_SRC}/cognee-mcp/src/cognee_client.py /src/mcp/src/cognee_client.py
ADD --chmod=644 --checksum=sha256:b0031300ef60b959e18c7af788313bcd6e0b70a195d98cbefdccb9f1d91848c3 ${MCP_SRC}/cognee-mcp/src/retrieval_utils.py /src/mcp/src/retrieval_utils.py
ADD --chmod=644 --checksum=sha256:0f16a14171143e8171192cc5bdf5490c8c848acbc8ac6300e4b651f1f6a1bf46 ${MCP_SRC}/cognee-mcp/src/server.py /src/mcp/src/server.py
ADD --chmod=644 --checksum=sha256:b468ca478893e1caca78d66fc4129ea43ea19013a0e062e1bd3c035e250f7baf ${MCP_SRC}/cognee-mcp/src/server_utils.py /src/mcp/src/server_utils.py
ADD --chmod=644 --checksum=sha256:cd4632ab9fabe33ee683c08982a855f7a8371e0e4bcb077d51380d314a9d74b7 ${MCP_SRC}/cognee-mcp/src/tool_registry.py /src/mcp/src/tool_registry.py
ADD --chmod=644 --checksum=sha256:a471e34908f50c3f120e9a56f3c30cb34e133e4ede01a9cbd5ce718fc358ecd4 ${MCP_SRC}/LICENSE /app/licenses/cognee/LICENSE
ADD --chmod=644 --checksum=sha256:e8bdec68fde44a97229e03b10e1f921a3312f0663bf51961d9d04d2da3b4a397 ${MCP_SRC}/NOTICE.md /app/licenses/cognee/NOTICE.md
# Installed where the wheel of cognee-mcp puts it (its package is named "src"), with the
# command "cognee-mcp" its pyproject.toml declares (src:main_mcp).
RUN cp -r /src/mcp/src /app/.venv/lib/python3.12/site-packages/src \
 && /app/.venv/bin/python -m compileall -q /app/.venv/lib/python3.12/site-packages/src \
 && printf '#!/app/.venv/bin/python\nimport sys\nfrom src import main_mcp\nif __name__ == "__main__":\n    sys.exit(main_mcp())\n' >/app/.venv/bin/cognee-mcp \
 && chmod 755 /app/.venv/bin/cognee-mcp
# No graph step (28 Oct 2026): cognify builds the chunks-and-embeddings task list
# (get_just_chunks_tasks) instead of the GLiNER one, so the GLiNER model is never loaded. The
# patch is a text replacement in cognee 1.6.5's cognify.py; it stops the build unless the text
# is there exactly once (a new cognee version then needs this patch looked at again).
RUN /app/.venv/bin/python -I - <<'PY'
import ast, pathlib
p = pathlib.Path("/app/.venv/lib/python3.12/site-packages/cognee/api/v1/cognify/cognify.py")
s = p.read_text()
anchor = "            tasks = await get_gliner_demo_tasks("
assert s.count(anchor) == 1, "cognify.py changed: the GLiNER call is not where the patch expects it"
start = s.index(anchor)
end = s.index("\n            )\n", start) + len("\n            )\n")
new = ("            from cognee.eval_framework.corpus_builder.task_getters.get_default_tasks_by_indices import get_just_chunks_tasks\n"
       "            from cognee.modules.pipelines.tasks.task import Task as _Task\n"
       "            # add_data_points defaults to needs_llm=True: with it, the run probes the LLM and fails\n"
       "            tasks = [_Task(t.executable, task_config={\"batch_size\": 10}, needs_llm=False) if t.executable.__name__ == \"add_data_points\" else t\n"
       "                     for t in await get_just_chunks_tasks(chunk_size=chunk_size, chunker=chunker)]\n")
patched = s[:start] + new + s[end:]
ast.parse(patched)
p.write_text(patched)
print("cognify.py: GLiNER tasks replaced by get_just_chunks_tasks")
PY

# Checks on the files only (nothing of cognee runs here): the pinned versions, the enola
# binary of the code graph, and the bundled Ladybug JSON extension (else Ladybug would
# download it at the first query).
RUN /app/.venv/bin/python -I -c 'import importlib.metadata as m; want = {"cognee": "1.6.5", "gliner2": "2.0.0", "fastembed": "0.8.0", "fastmcp": "3.4.6", "mcp": "1.28.1"}; seen = {k: m.version(k) for k in want}; assert seen == want, seen; print(seen)' \
 && test -x /app/.venv/bin/enola \
 && find /app/.venv -name '*.lbug_extension' | grep -q .

# ---- the models: downloaded once here, checked by SHA-256 ------------------------------
FROM build AS models
COPY models.txt fetch_models.py /src/
RUN /app/.venv/bin/python -I /src/fetch_models.py /src/models.txt /opt/cognee/hf/hub
# The tiktoken tables cognee counts tokens with (tiktoken checks their SHA-256 itself).
RUN TIKTOKEN_CACHE_DIR=/opt/cognee/tiktoken /app/.venv/bin/python -I -c 'import tiktoken; [tiktoken.get_encoding(n) for n in ("cl100k_base", "o200k_base")]' \
 && chmod -R a+rX /opt/cognee

# ---- the image that runs ------------------------------------------------------------
FROM ${PYTHON_IMAGE}
ARG COGNEE_VERSION
LABEL org.opencontainers.image.title="wattson-cognee" \
      org.opencontainers.image.description="cognee-mcp ${COGNEE_VERSION}, local models only (fastembed, no graph step), no telemetry" \
      org.opencontainers.image.source="https://github.com/topoteretes/cognee/tree/v1.6.5/cognee-mcp" \
      org.opencontainers.image.licenses="Apache-2.0"
# pip is not needed at run time: one tool less in the image.
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip /usr/local/lib/python3.12/site-packages/pip-* \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12
COPY --from=build /app /app
COPY --from=models /opt/cognee /opt/cognee
# The ADD --chmod=644 lines of the build stage also gave the folder of the MCP server (and of the
# licenses) mode 644, with no x: user 10001 could not enter it, and the first start on the cluster
# failed with "cannot import name 'main_mcp' from 'src'" (28 Oct 2026). The checks of the build
# stage run as root, which enters any folder, so they passed. The fix is here, in a small layer of
# its own, so the big /app layer stays the one the registry already has. Then the build stops if
# any folder or file is still closed to user 10001.
RUN chmod -R a+rX /app/.venv/lib/python3.12/site-packages/src /app/licenses \
 && test -z "$(find /app /opt/cognee \( -type d ! -perm -a+rx \) -o \( -type f ! -perm -a+r \) | head -n 1)"
# Where the models are, and never a download: the pod reads only the files above.
# The other "phone home" switches are here too, so the image is quiet wherever it runs; the
# Deployment (k8s/cognee/cognee.yaml) sets them again, with the rest of its settings.
ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    HF_HOME=/opt/cognee/hf \
    HF_HUB_CACHE=/opt/cognee/hf/hub \
    FASTEMBED_CACHE_PATH=/opt/cognee/hf/hub \
    TIKTOKEN_CACHE_DIR=/opt/cognee/tiktoken \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TELEMETRY_DISABLED=1 \
    COGNEE_TRACING_ENABLED=false \
    GLINER_AUTO_INSTALL=false \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    ENOLA_NO_UPDATE_CHECK=1 \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_SERVER_BANNER=false \
    RUNTIME__DLTHUB_TELEMETRY=false \
    DO_NOT_TRACK=1
WORKDIR /tmp
USER 10001:10001
# The start file of the API server (the container "api" of the pod; the container "mcp" runs
# the ENTRYPOINT below in API mode). A small layer of its own, after the big ones: they stay
# the layers the registry already has. Mode 755, not 644: --chmod also sets the mode of the
# folder it makes, and a folder needs x to be entered (the bug of 28 Oct 2026, see above).
COPY --chmod=755 api_start.py /app/wattson/api_start.py
# Import checks as user 10001, the user of the pod, and with no network: the API server, the
# MCP server and the start file must load as that user (a folder closed to it fails here, not
# on the cluster). Nothing starts and nothing is written to the image (the logs go to a folder
# in /tmp that is removed).
RUN --network=none test "$(stat -c %a /app/wattson)" = 755 \
 && COGNEE_LOG_FILE=false COGNEE_LOGS_DIR=/tmp/import-check \
    python -c 'import sys; sys.path.insert(0, "/app/wattson"); import cognee.api.client, src.server, api_start; from src import main_mcp; assert cognee.api.client.app.title and callable(main_mcp) and callable(api_start.main); print("imports: ok as user", __import__("os").getuid())' \
 && rm -rf /tmp/import-check \
 && test -z "$(find /tmp -mindepth 1 -maxdepth 1 | head -n 1)"
ENTRYPOINT ["cognee-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
