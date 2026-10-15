# Digest-pinned bases (re-pin deliberately; CI fails on an unpinned FROM/COPY --from).
FROM python:3.11-slim@sha256:6f31d6e9ba2b0a787a3f81c37b004155b87b9efa1b771182bd550c1615745be5
COPY --from=ghcr.io/astral-sh/uv:0.9@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /usr/local/bin/uv
COPY --from=litestream/litestream:0.3.13@sha256:027eda2a89a86015b9797d2129d4dd447e8953097b4190e1d5a30b73e76d8d58 /usr/local/bin/litestream /usr/local/bin/litestream
# Node >=22.19 for the pi read-only harness (tools/pi); copied from a digest-pinned image, not installed from apt.
COPY --from=node:22.20-bookworm-slim@sha256:b21fe589dfbe5cc39365d0544b9be3f1f33f55f3c86c87a76ff65a02f8f5848e /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22.20-bookworm-slim@sha256:b21fe589dfbe5cc39365d0544b9be3f1f33f55f3c86c87a76ff65a02f8f5848e /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
WORKDIR /srv
COPY pyproject.toml uv.lock ./
# --frozen: exactly the locked, hash-pinned set; nothing is resolved at build time.
ENV UV_HTTP_TIMEOUT=180 UV_HTTP_RETRIES=6
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
# pi: exactly the locked packages, no install scripts. A host node_modules is replaced, never trusted.
RUN rm -rf tools/pi/node_modules && cd tools/pi && npm ci --ignore-scripts --omit=dev
RUN useradd --system --create-home --uid 10001 nami && mkdir -p /srv/data/files && chown -R nami /srv/data
# Chainlit saves chat attachments and result images in /srv/.files. It must be writable by the app's user and survive a redeploy, so it lives in the data volume.
RUN ln -s /srv/data/files /srv/.files
ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1 NAMI_SKILLS_FREEZE=1
USER nami
# Browsers run on Kernel in production, so no local Chromium is installed. One replica only (SQLite + websockets).
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=15s --retries=5 --start-period=60s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=12).status==200 else 1)"
ENTRYPOINT ["/srv/infra/entrypoint.sh"]
