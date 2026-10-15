# Nami

A chat assistant that does tasks on websites in a background browser and manages the credentials it needs,
without passwords, keys or cards ever reaching the chat or the model.

## Run it locally

```bash
uv sync
uv run playwright install chromium
cp .env.example .env                      # add NAMI_ANTHROPIC_API_KEY; for local use also set NAMI_DEV_AUTH=1
export CHAINLIT_AUTH_SECRET=$(python -c "import secrets;print(secrets.token_hex(32))")
uv run uvicorn app.main:app --port 8000
```
Open http://localhost:8000 . Dev login: `dev@nami.local` / `dev` (only when `NAMI_DEV_AUTH=1`).
Without `NAMI_DEV_AUTH` and Google credentials the app refuses to start: there is no unauthenticated mode.

Migrations run on startup. Dev data lives in `data/` (git-ignored).

## How a request flows
`secret filter -> intent -> access plan (code) -> plan -> [guard -> run -> validate]* -> verify -> remember`.
See `docs/HARNESS.md` (what it does and why) and `docs/ARCHITECTURE.md` (build reference and status).

## Where secrets go
Only `POST /chainlit/vault/credentials` (from the secure form). Stored AES-256-GCM, per-user key wrapped by a KEK
(Azure Key Vault in production), bound to the user, the credential id and the allowed websites.

## Checks
```bash
uv run pytest                                   # unit, safety, integration and drift suites (browser tests need Chromium)
PYTHONPATH=. uv run python scripts/smoke_graph.py     # orchestrator end to end with a scripted model
PYTHONPATH=. uv run python -m app.eval.run_eval --agents adaptive brittle --n 1 --deny   # drift harness report
```
Safety suites: secret-leak (broker, prompts, events, DB, logs, traces), vault routes, chat prefilter, approval gate, injection
corpus, identity tokens, RBAC matrix, org-scoped sessions, skills (poisoned corpus, signing, sandbox), supply chain/deploy config.

## Docs
`docs/ACCESS_MANAGEMENT.md` (choices and limits, one page) · `docs/LIMITS.md` · `docs/DEMO_SCRIPT.md` · `docs/HARNESS.md` ·
`docs/ARCHITECTURE.md` · `docs/SKIPPED.md` (everything not done or not verified) · `docs/EVAL_RESULTS.json`.

## Deploy
`infra/` has a digest-pinned Dockerfile, Litestream config, Container Apps manifest and `deploy.sh`. **Never run**: no Azure
subscription yet. Treat as a draft.
