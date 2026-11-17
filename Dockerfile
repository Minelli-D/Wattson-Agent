# One image for the three agents. Each Deployment picks its program:
#   wattson-orchestrator, wattson-triage or wattson-knowledge (pyproject.toml, [project.scripts]).
# The dependencies come from uv.lock, exactly (no resolving at build time). The final image
# holds Python, the installed packages and wattson itself: no uv, no pip, no source tree.
# It runs as user 10001 and writes nothing outside /tmp and /data (both volumes in the pod).
#
#   docker build --platform linux/amd64 -t wattson .        (k8s/deploy.sh does this)

# python:3.13-slim (3.13.17, Debian trixie) and uv 0.13.5, pinned by digest.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff

FROM ${UV_IMAGE} AS uv

# ---- build: install the locked dependencies, then wattson, into /app/.venv ----------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.13 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_CACHE=1
WORKDIR /src
# The dependencies first: this layer stays cached while only src/ changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
# Then wattson itself, installed as a package (not a link to /src).
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- the image that runs ------------------------------------------------------------
FROM ${PYTHON_IMAGE}
# pip is not needed at run time: one tool less in the image.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-* \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TMPDIR=/tmp
WORKDIR /tmp
USER 10001:10001
# No default program: the Deployment names one. This one only says which exist.
CMD ["python", "-c", "print('Run one of: wattson-orchestrator, wattson-triage, wattson-knowledge')"]
