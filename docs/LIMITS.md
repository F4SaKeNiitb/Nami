# Limits and what is not done

Short and blunt. The long version, with every skipped test and unverified path, is `docs/SKIPPED.md`.

**Measured** (offline, repeatable): the test suite, `scripts/smoke_graph.py`, the drift harness (`docs/EVAL_RESULTS.json`: leaks, approval adherence, recovery under UI drift with a scripted model and a stand-in site).

**Not measured**: task success on real websites, cost per real task, how often the executor model is fooled by a page, the judge's accuracy on real pages (the 0.7 "unverified" threshold is a placeholder; `app/eval/calibrate.py` calibrates it once real recorded runs exist).

**Needs something I don't have yet**: an Anthropic key (the executor loop has never run live), Google OAuth credentials, a Kernel key, an Azure subscription (Key Vault, Container Apps, Blob).

**Known gaps**
- The browser agent's own page input isn't sanitised (library limitation); defence there is structural (plan, domains, placeholders, approvals), not a filter.
- Mid-task session expiry is recovered by retry → replan, which re-does earlier steps rather than just signing in and continuing.
- Skills: learned workflows are guidance text; code snippets run in a sandboxed page world with a timeout, size cap and network blocked outside the plan, but that is a policy layer, not a hardened VM.
- Org-level encryption keys, shared credentials and Postgres isolation are not built (credentials are never shared, so there is nothing for an org key to protect yet).
- Context management for long tasks (compaction, plan recitation) is not implemented; long tasks may hit model limits.
- Single replica; Alembic runs at start-up.

**Deliberately not done**: no unauthenticated mode, no email/password sign-in in production (Google only), no secret ever passed through `callAction` or the chat socket.
