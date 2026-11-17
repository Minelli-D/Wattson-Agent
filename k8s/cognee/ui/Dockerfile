# The image of Cognee's web UI (cognee-frontend: Next.js 16, React 19, Apache-2.0) for the
# container "ui" of the pod wattson-cognee (k8s/cognee/cognee.yaml, docs/cognee.md). Built by
# k8s/cognee.sh --apply; the build context is this folder, and the build uses no file of it.
#
# The browser loads the pages from this server, then calls Cognee's API server directly, at
# COGNEE_BACKEND_URL (set by the pod at run time, read per request). Both are reached only
# through kubectl port-forward.
#
# All pinned:
#   - Node 22 on Alpine, by digest;
#   - the source: the archive of the Cognee repository at the commit of the tag v1.6.5
#     (37f788bd), checked by SHA-256; only cognee-frontend/, LICENSE and NOTICE.md are used;
#   - the npm packages: "npm ci" installs exactly package-lock.json (each package checked by its
#     own sha512), with no install scripts.
# Built as the open-source local UI (NEXT_PUBLIC_IS_CLOUD_ENVIRONMENT=false: no Cognee Cloud
# shell; its analytics are empty stubs) with Next.js telemetry off. Next.js "standalone" output:
# the server and only the packages it uses, no dev dependencies. Not pinned: the two Geist fonts,
# which next/font/google downloads from Google Fonts during the build and puts into the image
# (the browser then gets them from this server, never from Google).
#
# It runs as user 10001 with a read-only root: it writes only to /tmp and /app/.next/cache
# (emptyDir volumes in the pod).
#
#   docker build --platform linux/amd64 -t wattson-cognee-ui k8s/cognee/ui      (k8s/cognee.sh does this)
# The version, for the image tag (k8s/cognee.sh reads this line):
ARG COGNEE_VERSION=1.6.5

# node:22-alpine (Node 22.23.4, Alpine 3.24, 21 Oct 2026), pinned by digest.
ARG NODE_IMAGE=node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914
ARG COGNEE_COMMIT=37f788bd287750e10d7610173e5332b3843f6ebd

# ---- the source: one archive of the pinned commit, checked by SHA-256 --------------------
FROM ${NODE_IMAGE} AS source
ARG COGNEE_COMMIT
ADD --checksum=sha256:a37255141b245a4e5f8397b2f5e4e0154251cb46309d7193d5541a3ce08b6bc2 https://github.com/topoteretes/cognee/archive/${COGNEE_COMMIT}.tar.gz /cognee.tar.gz
RUN mkdir -p /unpack \
 && tar -xzf /cognee.tar.gz -C /unpack "cognee-${COGNEE_COMMIT}/cognee-frontend" "cognee-${COGNEE_COMMIT}/LICENSE" "cognee-${COGNEE_COMMIT}/NOTICE.md" \
 && mv "/unpack/cognee-${COGNEE_COMMIT}" /src \
 && chmod -R a+rX /src \
 && test -f /src/cognee-frontend/package-lock.json

# ---- the packages of the lock, exactly; no install scripts ---------------------------------
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY --from=source /src/cognee-frontend/package.json /src/cognee-frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund --loglevel=warn

# ---- the build: Next.js standalone output ------------------------------------------------
FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=source /src/cognee-frontend ./
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_IS_CLOUD_ENVIRONMENT=false
# The modes are set here, in the build stage: a chmod in the last stage would copy every file
# into one more layer (about 95 MB more to push).
RUN ./node_modules/.bin/next build \
 && test -f .next/standalone/server.js \
 && test -d .next/static \
 && chmod -R a+rX .next/standalone .next/static public

# ---- the image that runs -----------------------------------------------------------------
FROM ${NODE_IMAGE}
ARG COGNEE_VERSION
LABEL org.opencontainers.image.title="wattson-cognee-ui" \
      org.opencontainers.image.description="Cognee web UI ${COGNEE_VERSION} (cognee-frontend), local mode, telemetry off" \
      org.opencontainers.image.source="https://github.com/topoteretes/cognee/tree/v1.6.5/cognee-frontend" \
      org.opencontainers.image.licenses="Apache-2.0"
# npm, npx, yarn and corepack are not needed at run time: tools less in the image.
RUN rm -rf /usr/local/lib/node_modules /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
           /usr/local/bin/yarn /usr/local/bin/yarnpkg /opt/yarn-*
WORKDIR /app
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
COPY --from=source /src/LICENSE /src/NOTICE.md /app/licenses/cognee/
# Every folder open and every file readable for user 10001, not only for root, who runs the
# build (the bug of the cognee image on 28 Oct 2026): the build stops otherwise. /app/.next/cache
# is where Next.js writes (an emptyDir in the pod: the root is read-only).
RUN mkdir -p /app/.next/cache \
 && test -z "$(find /app \( -type d ! -perm -a+rx \) -o \( -type f ! -perm -a+r \) | head -n 1)"
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    HOME=/tmp \
    TMPDIR=/tmp
USER 10001:10001
# A check as user 10001 and with no network: Node loads Next.js from the image (every folder it
# needs is open to that user) and the server file is there. Nothing starts.
RUN --network=none node -e 'const next = require("next"); if (typeof next !== "function") throw new Error("next"); require("fs").accessSync("server.js", require("fs").constants.R_OK); console.log("next", require("next/package.json").version, "loads as user", process.getuid())'
EXPOSE 3000
CMD ["node", "server.js"]
