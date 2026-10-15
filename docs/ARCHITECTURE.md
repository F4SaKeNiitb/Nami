# Nami — Architecture & Build Reference

**Status (2026-10-04):** storage layer built and tested (tenancy, rules, memory, caching: 30 tests on fakeredis, 33 on real Redis 8.10). Everything else is specified here and unbuilt. Companion document for humans: [`HARNESS.md`](HARNESS.md).

## 0. How to use this document

- This is the single source of truth while building. If the code and this file disagree, fix one of them the same day.
- §15 is the build plan. Tick `[x]` when a step's code **and its tests** are merged; append the date and test count. Never tick without tests (standing rule: every change ships with a test suite, safety cases first).
- Decisions live in §2. Changing one means editing the row and adding a line to §17 (changelog). Don't leave two conflicting statements.
- "Verify" items in §12 are facts the design relies on but hasn't confirmed against the real SDK/service. Clear them in Phase 1 before building on them.

---

## 1. Mission

Take-home for Luffy (Zonko Labs): one chat assistant that (a) completes tasks on arbitrary websites through a background browser, and (b) collects, stores, reuses and revokes access — API keys, passwords, OAuth, cards, authenticator 2FA — without secrets ever touching the chat or the model. Deliverables: runnable repo, deployed link, ~2-min demo, a note on access-management choices and limits.

Evaluated on **UX taste · credentials thinking · reliability · judgment**.

**Reference product.** Instinct is *not* a Luffy product (it's by Spear Street Technology / Noah Shinn): text-first, persistent cloud computer, a credential Vault (moving to 1Password), TOTP autofill since Sep 2026. Reviewers' complaints: no visibility, no permission gates, 2FA/CAPTCHA walls. **Our bar = Instinct's "no new interfaces" chat feel + live view, step log and approval cards.**

Codename **Nami**.

---

## 2. Locked decisions

| Area | Decision | Why | What we gave up |
|---|---|---|---|
| Language | Python 3.11+ end to end | browser-use (best browser-agent library) is Python; one runtime, one deploy | React-level UI freedom |
| UI | Chainlit 2.12 mounted on FastAPI | streaming chat, steps, Google OAuth, custom JSX elements out of the box | styling limits; secure input must bypass the chat socket |
| App sign-in | Google only, via Chainlit OAuth | assignment requirement; simplest | — |
| Browser host | Kernel (Profiles + live view + stealth), behind a `BrowserProvider` interface; local Playwright for dev | persistent profiles, embeddable live view for takeover, free tier | vendor dependency (cheap to swap) |
| Executor | browser-use `Agent` per subgoal | indexed-DOM + screenshot hybrid; native `sensitive_data` placeholders and TOTP | Browser Use itself now suggests a coding-harness outer loop; revisit after demo |
| Orchestrator | LangGraph graph + `AsyncSqliteSaver` | durable pause/resume; `interrupt()` = ask / takeover / approve; replanning is an edge | one more dependency |
| LLMs | Claude Sonnet 5.5 for anything that sees a page; OpenRouter free models for user-text-only jobs (intent, titles, summaries), non-thinking mode; Claude fallback on 429/5xx | cost control without letting cheap models see PII or vault data | — |
| Database | **SQLite (WAL)** + Litestream → Azure Blob | one container, one writer, ~$0 | exactly one replica; Postgres+RLS is the scale-out path |
| Cache | **Redis 8 sidecar, cache-only** (no persistence, LRU) | exact cache, hot STM, semantic cache on core Vector Sets | nothing in Redis may be a source of truth |
| Secrets at rest | per-user DEK, AES-256-GCM, AAD-bound; DEK wrapped by Azure Key Vault (RSA-OAEP-256); `LocalKms` in dev | crypto-shred on revoke; cheap ($0.03/10k ops) | AES key-wrap needs Managed HSM → avoided |
| Vault ownership | our own vault, not Kernel Managed Auth | the vault *is* the assignment; we need origin binding, audit, shredding | faster wiring |
| Deploy | Azure Container Apps, 1 replica, Redis sidecar; Key Vault; Blob | user has Azure; ~$12–15/mo | horizontal scale |
| Embeddings | local `BAAI/bge-small-en-v1.5` via fastembed (384-d) | no text leaves the container | small model → weak intent separation (see §7.4) |
| Semantic cache | **intent only**, threshold 0.95 | measured: "show my order" scores 0.927 vs "repeat my order" | lower hit rate |
| Timeline | 3+ weeks | — | — |

---

## 3. Research → principles

| Finding | Source | Consequence |
|---|---|---|
| Planner → Executor → **Validator** beats flat ReAct (Skyvern 45→86% on WebVoyager; Agent-E; Agent S2; Magentic-One) | arXiv 2407.13032, 2504.00906, 2411.04468 | hierarchical loop, per-subgoal validator, final verifier |
| Live-site success ≪ vendor claims (Operator ≈61% on Online-Mind2Web); LLM judge over key points agrees with humans 86% | arXiv 2504.01382 | verification is a product feature; honest "unverified" |
| Distilled DOM + screenshot hybrid; small action space | AgentOccam 2410.13825; Browser Use internals | browser-use observation; vision off on credential steps |
| Report what changed after each action | Agent-E | streamed step log |
| Agent Workflow Memory (+51% rel.); Mariner Teach&Repeat; Magentic-UI plan gallery | arXiv 2409.07429, 2507.22358 | learned skills (§9), guidance only |
| Task ledger + progress ledger; replan on stall | Magentic-One | persisted ledger; stall detection |
| Confirmations cut risk ~90%; takeover mode | Operator system card | approval gates; takeover for creds/CAPTCHA |
| Prompt injection unsolved (WASP up to 86% partial success); CaMeL: fix capabilities **before** reading untrusted content | arXiv 2504.18575, 2503.18813 | Access Planner from user text only; page content can never widen scope |
| Google blocks automated sign-in by policy | Google dev blog | Google = takeover once, persist session; Gmail via OAuth API |
| Keep reasoning across tool calls; at 80% context, **discard old tool outputs** beat summarising (BrowseComp 67.6 vs 51.4) | DeepSeek V3.2, arXiv 2512.02556 | ephemeral observations; summary only for ledger snapshot |
| "Model-visible means logged"; append-only event log; resume = replay | DeepSeek Harness (`dsh`, preview) | `task_events` is the source of truth; takeover is just another event |
| Stable prefix for KV cache; mask tools don't remove; todo recitation; keep errors | Manus | frozen per-task prefix; hooks gate tools; plan block re-appended; errors retained |
| Hooks as guardrails; single thread owns writes; sub-agents read-only | Claude Agent SDK, Cognition, Anthropic multi-agent | pre/post-action hooks; one executor owns the browser |
| Don't over-wrap the model; raw CDP escape hatch | Browser Use "Bitter Lesson" | gated `run_cdp_snippet`, saved as code skills |
| Skills are attacker-influenced bundles; sign, pin, scan, govern | OWASP Agentic Skills Top 10 (RC, 2026) | §9 |

---

## 4. System architecture

```
┌──────────────────────────── User's browser ─────────────────────────────┐
│ Chainlit UI (Google sign-in)                                             │
│  one thread · streamed steps · cards: Connection/Approval/NeedsYou/      │
│  Result/Failure · LiveBrowserView (Kernel iframe; takeover mode)         │
│  SecureInput/TotpSetup → POST /chainlit/vault/* (never the chat socket)  │
│  Sidebar: Connections · Learned workflows · Activity log                 │
└──────────────┬──────────────────────────────────┬────────────────────────┘
               │ websocket (chat only)             │ HTTPS (secrets only)
┌──────────────▼──────────────────────────────────▼────────────────────────┐
│ FastAPI app — one container (Azure Container Apps)                        │
│  /chainlit · /chainlit/vault/* · /oauth/* · /tasks/* · /.well-known/jwks  │
│                                                                           │
│  LangGraph orchestrator (AsyncSqliteSaver)                                │
│   intent → access_plan → plan → [execute_subgoal ⇄ validate]* → verify    │
│   → remember · interrupts: ask_user / request_takeover / confirm_action   │
│                                                                           │
│  Security plane (§8): Gateway (labels, sanitiser, classifier, capability  │
│   policy, egress) · RBAC `can()` · ScopedSession · IdentityIssuer (SPIFFE │
│   IDs, ≤120 s audience-bound tokens) · OTel → LangSmith / Azure Monitor   │
│                                                                           │
│  Credential Broker · Session Manager · Guard · Verifier · LLM Router      │
│  Rules engine · Short/Long-term memory · Exact+Semantic cache (§7)        │
│  Skills store + scanner (§9) · Site hints (YAML)                          │
└──────┬──────────────┬───────────────┬──────────────┬─────────────┬────────┘
   SQLite (WAL)   Redis 8 sidecar   Azure Key Vault  Kernel browsers  Azure Blob
   source of      cache only        KEK, signing     CDP, live view,  screenshots,
   truth; Lite-   (LRU, no disk)    keys             per-user profile Litestream
   stream→Blob                                                        replica
```

### 4.1 Component responsibilities

| Component | Responsibility | Files |
|---|---|---|
| `cl_app.py` | Chainlit handlers: `oauth_callback` (Google only → `tenancy.ensure_user`), `on_message` (secret prefilter → graph), action callbacks (approve/deny/retry/hand-back), `on_chat_resume` | `app/cl_app.py` |
| Vault API | credentials CRUD, TOTP setup/verify, revoke (crypto-shred), access log. Auth: `Depends(chainlit.auth.get_current_user)`. Only path secrets travel | `app/vault/router.py`, `service.py`, `kms.py`, `crypto.py`, `totp.py` |
| Credential Broker | builds `sensitive_data` for a task from Access-Plan-approved credentials only; origin binding (eTLD+1 of top frame **and** target frame); TOTP at fill time; redaction-by-value over all model-visible text | `app/browser/broker.py` |
| Browser Provider | `open(user, headless) → {cdp_url, live_view_url, session_id}`, `close` (Kernel `delete_by_id` persists the profile), `snapshot_cookies/restore_cookies` over raw CDP, `clear_site` | `app/browser/provider.py`, `kernel_provider.py`, `local_provider.py` |
| Session Manager | one Kernel Profile per (org,user) + per-(user,site) encrypted snapshots; per-user profile lock; headless default; health check; re-auth state machine | `app/browser/session_manager.py` |
| Executor | browser-use `Agent` factory; hooks → events; custom tools `ask_user`, `request_takeover`, `confirm_action`, `extract_evidence`, `run_cdp_snippet` | `app/browser/executor.py`, `tools.py` |
| Agent brain | `intent` (≤1 clarifying question), `access_planner` (CaMeL), `planner` (subgoals + criteria + irreversibility), `validator`, `verifier` (WebJudge-style), `guard`, `memory_writer` | `app/agent/*` |
| Orchestrator | LangGraph graph, ledger, interrupts, retries/replan, resume | `app/agent/graph.py` |
| Rules / Memory / Cache | **built** — §7 | `app/rules/`, `app/memory/`, `app/cache/` |
| Security plane | gateway, RBAC, scoped session, identity issuer, telemetry | `app/security/*` |
| Skills | store, scanner, lifecycle | `app/skills/*` |
| Site hints | YAML: auth-check URL, login patterns, irreversible keywords, UA hints | `app/sites/registry.yaml` |
| Eval | golden tasks, drift harness, injection corpus, WebJudge scorer | `app/eval/` |

---

## 5. Core flows

### 5.1 Task lifecycle

```
"Repeat my last Swiggy order to the same address"
 ├ secret prefilter (regex: keys, PAN+Luhn, base32 seeds, otpauth, "password:") → none
 ├ intent (cheap LLM; exact+semantic cache, §7.4) → {site swiggy.com, goal reorder_last, constraints}
 │   clarify only if the answer changes the outcome
 ├ access plan (from USER TEXT ONLY) → required [swiggy session], optional [card]; action cats [purchase]
 │   missing → ask in chat (secure form): Swiggy = phone + password, the SMS code is asked for in chat; one-time window only on request
 ├ RBAC + rules: can(user, start_task) · effective rules (spend limit, approvals, domains) · mint agent tokens
 ├ plan → subgoals {goal, success_criterion, irreversible?, is_auth?}
 ├ per subgoal: session health → executor (≤15 steps, allowed_domains, sensitive_data, vision off if is_auth)
 │   → change observation streamed → validator on fresh page state → ledger write
 ├ irreversible subgoal → Guard → ApprovalCard(items, total, address, pay) → approve
 │   payment may need UPI push → NeedsYouCard → poll
 ├ verifier → confidence; <0.7 → "done but unverified" + screenshot
 ├ ResultCard with evidence (order id, screenshot)
 └ memory_writer → skill candidate (§9) + facts to long-term memory
```

### 5.2 Credential collection

| Need | Ask | Store | Use |
|---|---|---|---|
| API key | `SecureInput(kind=api_key)` → `/chainlit/vault/credentials` | AES-GCM; label + last4 plaintext | server-side call, or placeholder typed into a site |
| Website password | `SecureInput(kind=password)` (+ optional TOTP step) | same | `sensitive_data` scoped to origin; vision off |
| Google / SMS-OTP / passkey / CAPTCHA | `LiveBrowserView(interactive)` + "You're in control. I'm not watching." + "Done, hand back" | **only the session** (profile + encrypted snapshot) | reused next task |
| OAuth (Gmail read for email OTPs) | `/oauth/google/start?scopes=gmail.readonly` | tokens encrypted | server-side; only the newest matching message, code extracted server-side, never the whole email to the model |
| TOTP seed | paste key / otpauth URI / QR decoded client-side (jsQR); current code shown for the user to verify | seed + algo/digits/period (`pyotp.parse_uri`) | standard 6/30/SHA1 → placeholder named `<<cred_X>>_bu_2fa_code` (browser-use generates at type time); otherwise Broker pre-generates with pyotp (wait if <5 s left), one retry |
| Card | `SecureInput(kind=card)` with "demo vault, not PCI; prefer a virtual card" | PAN/CVV encrypted; brand/last4/exp plaintext | only after itemised ApprovalCard, every time |

Rules enforced in code: chat never carries a secret (prefilter stores it, redacts the message in the data layer, replies "moved to your vault ••••a3f9"); one unique placeholder per credential; no tool returns a secret; origin binding; revoke deletes ciphertext, clears site cookies, deletes snapshot, logs.

### 5.3 Sessions and re-auth

```
acquire per-user profile lock → Kernel browsers.create(profile=org-{o}-user-{u}, save_changes=True, headless=True)
snapshot(S) exists & profile lacks S cookies → restore via CDP Storage.setCookies (+localStorage)
health check: goto hints[S].auth_check_url
  logged in  → URL not /login|/signin|accounts.google.com, no password input
  expired    → ReAuth: 1) stored password(+TOTP) placeholders, vision off  2) ask in chat (secure form)
               3) success → re-snapshot, continue the SAME subgoal
after task: re-snapshot; browsers.delete_by_id → Kernel writes profile; release lock
```

### 5.4 Interrupts (all resumable; each is a LangGraph `interrupt()` and a ledger event)

| Event | UI | State |
|---|---|---|
| `ask_user` | NeedsYouCard (buttons / secure code field) | paused; browser kept alive (session_id in ledger) |
| `request_takeover` | interactive live view + hand-back | paused; **no screenshots to the model** until hand-back |
| `confirm_action` | ApprovalCard Approve / Edit / Cancel | paused |
| session expired | handled by Session Manager; card only if takeover needed | same subgoal retried |
| failure after retries | FailureCard: reason + last screenshot + [Retry] [Take over] [Try another way] | `blocked`, resumable |

---

## 6. Agent loop

- **Executor**: `Browser(cdp_url, allowed_domains)`; `Agent(task=goal+criterion, llm, browser, sensitive_data, use_vision=False if is_auth else "auto", extend_system_message=rules block + untrusted-content rule + "call confirm_action before pay/order/send/delete/security change" + retrieved skill guidance + Reflexion note, enable_planning=True, max_failures, fallback_llm)`; `run(max_steps≈15, on_step_start, on_step_end)`. Hooks write `task_events` + Chainlit steps; screenshots → Blob. Tools block on Chainlit asks after persisting the interrupt.
- **Context management**: keep thinking within a subgoal; old page observations → one-line stubs (last N full); frozen per-task prefix (system + all tool schemas + skill guidance; no timestamps) for Anthropic prompt caching; plan recitation block every K steps; failed actions stay in context until the step passes; tools gated by hooks, never added/removed mid-task.
- **Validator**: Claude call on fresh screenshot + URL + text excerpt + criterion → `{passed, confidence, evidence, what_changed}`; receives full plan + recent trace (no context loss). Fail → Reflexion → retry ≤2 → replan once → FailureCard.
- **Guard** (deterministic first): (1) planner-tagged irreversible subgoals gate before start; (2) `confirm_action` tool mandated; (3) URL/button-text backstop in `on_step_start`; (4) rules: `within_spend`, `domain_allowed`, `needs_approval`, `blocked`. LLM classifier only for the ambiguous remainder.
- **Verifier**: WebJudge-style key points from the plan → confidence + extracted facts.
- **LLM Router**: Sonnet 5.5 (`claude-sonnet-5-5`; forced `tool_choice` rejected → `auto` + strict) for page-seeing calls; OpenRouter free (non-thinking) for user-text-only purposes; cost/tokens per task in ledger. Thinking models on OpenRouter need `reasoning_content` echoed with tools — avoided by using them tool-less.
- **Sub-agents**: read-only lookups only; one executor owns the browser and all writes.

---

## 7. Rules, memory, caching — BUILT

Code: `app/rules/`, `app/memory/`, `app/cache/`, `app/db/`, `app/security/secrets.py`. Tests: `tests/`. Smoke: `scripts/smoke_storage.py`.

**Invariant:** SQLite is the source of truth; Redis holds only what can be rebuilt. Every Redis failure degrades to a miss; `NAMI_REDIS_URL` unset = caches off.

### 7.1 Tenancy
`orgs` (firm; every user also gets a **personal org** on first sign-in), `users`, `memberships(role owner|admin|member)`. Only owner/admin change firm rules or delete firm memories.

### 7.2 Rules (`app/rules/engine.py`, `policies.py`)
- Rule = firm-wide (`user_id NULL`) or per-user, optional `site_etld1`. `kind`: `policy` (code-enforced: `spend_limit {"INR":2000}`, `require_approval`, `blocked_actions`, `domain_deny`, `domain_allow`, `allow_card_use`, `allow_stored_passwords`, `max_task_minutes`), `preference` (prompt), `instruction` (prompt). `enforcement`: `hard` (firm only; users can't loosen) | `soft`.
- Resolution per key: base = user's rules if any else firm soft; site-specific beats general within a layer; then firm **hard** layered restrictively (min spend, union of approvals/denies, intersection of allow-lists, AND for booleans); preferences: hard wins. Instructions additive (firm-hard, firm-soft, user). Baseline `require_approval` for purchase/send_message/delete/account_change cannot be removed.
- Writes validate policy values; permission checks (admin for firm rules, self for own). Every change bumps `orgs.rules_version` / `users.rules_version` **in the same transaction**; the resolved-rules cache key and every LLM-cache partition include these versions → no stale rules ever served.
- `EffectiveRules.prompt_block()` renders deterministically (prompt-cache friendly). Guard helpers: `domain_allowed`, `needs_approval`, `blocked`, `within_spend`.

### 7.3 Memory (`app/memory/`)
- **Short-term** (per thread): rolling summary + last `stm_window_turns` (20) verbatim; fold older turns via pluggable `Summarizer` with optimistic check on `summary_upto_seq`; no DB session held during the LLM call; turns already summarised never shown twice. **Per channel**: `channel_recent()` across all threads. Redis hot copy: `RPUSHX` (never half-fill), seq-sorted reads, rebuild-on-miss. Content passes `redact()` before storage.
- **Long-term**: `memory_items` scoped `user | org | channel`, never crossing orgs; ranking `0.7·cos + 0.2·recency(half-life 30d) + 0.1·importance`; access bumps freshness; exact dupes reinforce, near-dupes (≥0.95) superseded; `SecretInMemoryError` for secret-shaped content; optional TTL; forget = hard delete (user scope: owner only — admins cannot read or delete personal memory). Brute-force numpy cosine over visible rows (→ sqlite-vec past ~50k rows). Rows carry `embedding_model`.
- **Procedures** (`kind='procedure'`) are guidance only and move to the `skills` table (§9).

### 7.4 Caching (`app/cache/`)
- **Exact**: `nami:{ns}:{sha256(canonical json)}`, TTL; versioned keys instead of invalidation.
- **LLM cache policy**: only user-text-only purposes. `intent` exact+semantic; `plan` exact, keyed on **structured intent JSON**; `clarify|title|summary` exact. Executor/validator/verifier/guard **never cached**. Partition = `(org, user, model, purpose, rules_version, prompt_version)`. Prompts containing secret-shaped text are never cached.
- **Semantic backends**: `VectorSetIndex` (default; Redis 8 core `VADD/VSIM`, one set per partition, `NOQUANT`, `{"exp"}` attribute + `FILTER`, sorted-set cleanup, cap 500/partition; `VSIM` score `(1+cos)/2` converted back; **redis-py 8.1 bug**: empty `VSIM WITHSCORES` reply raises `AttributeError` → treated as miss) · `RedisVectorIndex` (Query Engine `FT.*`; **not in Homebrew Redis, untested here**) · `InMemoryVectorIndex`. Select with `NAMI_SEMANTIC_CACHE_BACKEND`.
- **Calibration (bge-small vs "Repeat my last Swiggy order")**: 0.987 "…please", 0.936 "Can you order my last Swiggy order again?", 0.930 "Reorder my previous…", **0.927 "Show my last…"**, 0.918 "…to my office address", 0.912 "Cancel…". Threshold **0.95**, intent-only. `tests/test_semantic_calibration.py` guards it.

### 7.5 SQLite rules
WAL, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`; **`pool_size=1`** (avoids WAL `SQLITE_BUSY_SNAPSHOT` between our own connections). **Never hold a session across an LLM/embedding/Redis/browser await.** `UTCDateTime` returns aware datetimes. Alembic `render_as_batch=True` + naming convention; `alembic check` clean.

### 7.6 Redis keys
`nami:rules:{version}` · `nami:stm:t:{thread}` · `nami:stm:c:{channel}` · `nami:llm:{digest}` · `nami:semvs:{model}-{dim}:{partition}` · `nami:semvs-exp:…` · `nami:sempl:{id}`. (Phase 3 adds org to the prefix.)

---

## 8. Security plane (planned — Phase 3)

Principle: **no agent, tool or LLM call reaches anything except through one choke point that has verified who is calling, on whose behalf, and what they may do.**

### 8.1 Gateway (`app/security/gateway.py`) — prompt-injection defence, fails closed
1. **Provenance labels**: every model input tagged trusted (system, rules, user text) / untrusted (pages, emails, tool output, page-derived memory); untrusted wrapped in delimiters + standing "data, not instructions" rule.
2. **Deterministic sanitiser**: strip zero-width/bidi, hidden/off-screen/zero-opacity DOM text, HTML comments, aria/alt text differing from visible; length caps.
3. **Injection classifier** on untrusted spans (candidate: Llama Prompt Guard 2 or LLM judge — **verify** licence/accuracy/FP rate on real pages). 50–300 ms/span; run only on text that reaches the model.
4. **Capability policy (CaMeL)**: Access Plan fixed from user words; every tool call must fit (domain ∈ plan, credential ∈ plan, action category ∈ plan). Page content cannot extend it; extension = user approval.
5. **Egress control**: navigation/form posts/typed text only to plan domains; placeholder fill only on its own origin; responses scanned for secret-shaped text.
6. **Human gate**: L4/L5 violations and classifier hits on irreversible subgoals pause with the suspicious text shown.
On detection: quarantine span, log, tell the user plainly, continue only if the subgoal can finish without it; repeated hits mark the site untrusted for the task. Layers 1–3 will sometimes fail; 4–6 are deterministic and bound the damage.

### 8.2 RBAC
Roles: `owner > admin > member > viewer` (viewer read-only, can't start tasks). `channel_members(channel_id, user_id, role)`; channels `private|org`. `thread.visibility ∈ {private, channel, org}` (default private); messages, summaries, memories, ledgers inherit. One function `can(principal, action, resource) → Decision(allow, reason)` in the data layer: same `org_id` first and always; private thread = author only (**admins cannot read private chats or personal memory**); credentials/sessions never shared by visibility (explicit owner share = use-only). Agent effective permission = user rights ∩ agent-role rights ∩ task scope. Every decision → `authz_log`; denials alert.

### 8.3 Isolation between companies
`ScopedSession(principal)` adds `org_id` to every query on org-owned tables and refuses unscoped ones; static test bans raw-session imports for those tables. Per-org DEK for firm data (crypto-shred an org). Org in Redis prefixes. Kernel profile per (org,user). Traces/logs carry org. Escalation: SQLite file per org, or Postgres RLS.

### 8.4 Agent identity
SPIFFE ID `spiffe://nami.<domain>/org/{org}/agent/{role}/task/{task}`, role ∈ intent|access_planner|planner|executor|validator|verifier|memory_writer. Token minted **after** RBAC: JWT-SVID-style, `aud` = one internal target (vault|broker|browser|llm-gateway|memory), `exp ≤ 120 s`, claims org/act/task/scopes/jti. Verified at every internal boundary (sig, aud, exp, jti replay via Redis `SET NX`; **Redis down → vault/broker fail closed**). Issuer in-process, ES256 via Key Vault, JWKS at `/.well-known/jwks.json`. Not SPIRE yet (no attestation surface in one ACA container) — same ID format and claims, so swapping the issuer later is mechanical (**verify** SPIFFE JWT-SVID claims; SPIRE on ACA).

### 8.5 Observability
OpenTelemetry in-process; LangGraph/LangChain via LangSmith OTel integration (**verify** current OTLP ingest); spans: `task → graph node → agent step → llm.call | tool.call | gateway.check | authz.check | browser.action`. Attributes: `nami.org_id`, `nami.user_hash` (salted), `task_id`, `agent.spiffe_id`, `agent.role`, `authz.decision`, `gateway.verdict`, `cache.hit`, tokens, cost, model. Metrics: `gateway_blocks_total{layer}`, `authz_denied_total`, `approvals_*`, `secret_injections_total`, `cache_hit_ratio{kind}`, `task_success_total`, `task_duration_seconds`, `llm_tokens_total`, `llm_cost_usd_total`, `interrupts_total{type}`. Exporters: OTLP collector → LangSmith (traces) + Azure Monitor (metrics). **Privacy**: redaction before export; no screenshots/DOM/page text by default (hashes, lengths, verdicts); owner-approved time-limited `debug_capture` flag; project per org. Traces ≠ audit: audit (`secret_access_log`, `authz_log`, `task_events`, `skill_events`) is complete, append-only, in SQLite; shares `trace_id`.

---

## 9. Skills — OWASP Agentic Skills Top 10 (planned — Phase 4)

Source: OWASP Agentic Skills Top 10 (AST01–10), Incubator project; site says v1.0 (2026) but roadmap puts final release Q4 2026 → treat as RC; AST05's title has changed twice. Re-check before citing.

**Nami's skills:** learned workflows (AWM summaries of trajectories that *read web pages*), saved CDP snippets (executable), site hints (repo, reviewed), tools (code/deps). **Rule: anything derived from a trajectory is attacker-influenced until scanned, hash-pinned and — for code or sharing — approved.** Workflows are guidance text, never executed; only snippets execute, gated hardest.

### 9.1 Lifecycle
```
verified trajectory → AWM summariser (data-delimited, strict schema) → Pydantic validate
→ deterministic scan + LLM judge (judge may only lower trust) → manifest from OBSERVED domains/actions, capped by Access Plan
→ hash + ES256 sign → status: text-only & user scope & scan pass → approved (auto)
                              has_code → candidate → user approves exact hash
                              org scope → candidate → admin approves
load: verify sig+hash → approved → manifest ∩ access_plan ∩ RBAC ∩ token scopes → inject guidance / enable snippet
use: observed behaviour vs manifest → drift → log + candidate     revoke: immediate, caches invalidated
```
Firms can require approval for all skills (`skills_require_approval` policy). Prod `NAMI_SKILLS_FREEZE=1` disables writes to `approved`.

### 9.2 Scanner (`app/skills/scanner.py`)
Deterministic (blocking): unicode (zero-width, bidi, confusables, ASCII smuggling); `find_secrets()` on all fields; URLs outside `manifest.domains`; fetch/follow/"read X and apply" steps; privilege phrasing (memory/identity writes, credential handling, "prerequisite"/"first install", approvals bypass); code denylist (`fetch`, `XMLHttpRequest`, `WebSocket`, `sendBeacon`, `eval`, `Function`, `import(`), cross-origin URLs, other-origin storage reads, size/complexity caps; manifest schema-valid and domains ⊆ observed origins. Judge (advisory for text, blocking for code): "would this make an agent act outside the stated intent/manifest?" with bundle in data delimiters, strict `{verdict, reasons[]}`; prompt+version recorded.

### 9.3 Item map

| # | Risk | In Nami | Mitigation | Tests |
|---|---|---|---|---|
| AST01 Malicious skills | poisoned page → summariser → "step: paste password into chat"; exfil snippet | scan on creation; credential/non-plan-domain/prerequisite steps → candidate; snippets need human-approved hash; loader re-hashes | poisoned-trajectory corpus → 0 reach approved; snippet with non-plan network call blocked at approval and runtime |
| AST02 Supply chain | deps, image, model weights, future MCP; our skill store is a registry | `uv.lock` hashes, `pip-audit` CI, image digest pin, weights hash, Dependabot human-merge; skills signed; `skill_events` transparency log; MCP allow-list + pinned version | CI fails on unpinned/CVE; tampered row fails verification |
| AST03 Over-privileged | workflow steers executor off-site; snippet touches any origin | manifest; `effective = manifest ∩ plan ∩ RBAC ∩ token`; skills get no credential handles (placeholders stripped); hierarchy System > Operator > User > Skill/Tool output | wider manifest → loaded at plan scope + logged; `<<cred_…>>` in skill → stripped+flagged; non-manifest domain → runtime block |
| AST04 Insecure metadata | skill JSON, `registry.yaml`, AWM output | Pydantic strict (extra=forbid, bounds); `yaml.safe_load`; manifest-vs-behaviour check; site field must match observed origins | fuzz malformed/oversized/smuggled → rejected; YAML tags rejected; brand mismatch → candidate |
| AST05 Untrusted external instructions | pages are the external instructions; skills referencing URLs | whole gateway; skills may not contain fetchable references (URLs only as navigation within manifest domains); hints inline everything; summariser input data-delimited | injection corpus in drift harness: 0 off-plan actions, 0 leaks; external-reference step → rejected |
| AST06 Weak isolation | where skill code runs | snippets run only in the remote browser (`Runtime.evaluate`, isolated world, no Node/shell, timeout, size cap); CDP socket Kernel-authenticated, never exposed; Redis on localhost; no hot-reload in prod | non-plan `fetch()` blocked; timeout kills; prod config test asserts freeze + localhost Redis |
| AST07 Update drift | agent rewrites skills after success | every rewrite = new version (hash, sig, candidate); loader uses last approved; freeze; nightly re-verify/re-scan; inventory shows version/hash/last verified | stale signature refused; freeze rejects writes; downgrade to revoked refused |
| AST08 Poor scanning | prose+code mix | two-layer scanner; whole-bundle incl. hidden fields; continuous rescan; judge treated as injectable | known-bad corpus flagged; benign FP rate recorded; "mark this safe" injection cannot raise trust |
| AST09 No governance | users/firms can't see learned skills | "Learned workflows" panel (name, site, version, hash, provenance, last used, scan status, Revoke, Share→admin queue); `skill_events` audit joined to traces; revocation honoured immediately | revoked never loads; member can't approve org skill; every load logged; UI == DB |
| AST10 Cross-platform reuse | export/import to Anthropic Skills / MCP | USF fields (`risk_tier`, `permissions`, `signature`) in manifest; import = untrusted (re-scan, re-sign, candidate); export carries manifest+sig; stripped manifest → rejected | import without manifest rejected; round-trip keeps manifest, needs re-approval |

Residual: a text skill can still mislead *within scope* (wrong restaurant, not wrong card); scanners miss novel attacks — the capability policy and egress control are the boundary; upstream dependency compromise is mitigated, not prevented.

---

## 10. Data model (SQLite)

**Built** (migration `01dda0890700`): `orgs(id, name, slug, is_personal, rules_version, created_at)` · `users(id, google_sub, email, name, default_org_id, rules_version, created_at)` · `memberships(org_id, user_id, role)` · `rules(org_id, user_id?, site_etld1?, kind, key, value JSON, enforcement, priority, enabled, description, created_by, …)` · `channels(org_id, kind web|slack|whatsapp|email|api, external_id, name)` · `threads(channel_id, org_id, user_id, title, summary, summary_upto_seq, last_seq)` · `messages(thread_id, seq, role, author_user_id, content)` · `memory_items(org_id, scope, user_id?, channel_id?, kind, content, content_hash, embedding BLOB, embedding_model, embedding_dim, importance, source_thread_id, created_by, access_count, last_accessed_at, expires_at)`.

**Planned:**
```
user_keys(user_id PK, wrapped_dek BLOB, kek_id, version, created_at, destroyed_at)
org_keys(org_id PK, wrapped_dek BLOB, kek_id, version, destroyed_at)                      -- Phase 3
credentials(id, user_id, kind api_key|password|oauth|card|totp, label, origin_allowlist JSON,
            ciphertext BLOB, nonce BLOB, aad_hash, dek_version, meta JSON, linked_credential_id,
            shared_channel_id?, created_at, last_used_at, revoked_at)
browser_sessions(id, user_id, site_etld1, ciphertext_storage BLOB, nonce, captured_at, last_verified_at, status)
browser_profiles(user_id PK, provider, provider_profile_id)
tasks(id, org_id, user_id, thread_id, request_text, status planning|awaiting_access|running|awaiting_user|
      awaiting_approval|blocked|verifying|done|failed|cancelled, ledger JSON, result JSON, cost JSON, trace_id)
task_events(id, task_id, ts, type, payload JSON, screenshot_blob_url)                    -- append-only, model-visible = logged
secret_access_log(id, user_id, credential_id, task_id, agent_spiffe_id, origin, action, ts, result)  -- append-only
authz_log(id, ts, org_id, subject, action, resource, decision, reason, trace_id)         -- append-only
channel_members(channel_id, user_id, role owner|member|viewer)   threads.visibility private|channel|org
skills(id, org_id, scope user|org, user_id, site_etld1, intent, version, content JSON, content_sha256,
       manifest JSON, provenance JSON, status candidate|approved|revoked, approved_by, approved_at,
       scan JSON, signature BLOB, success_count, last_used_at, last_verified_at)
skill_events(id, skill_id, task_id, ts, type created|scanned|approved|loaded|blocked|revoked|drifted, payload)
```
Type mapping from the original Postgres draft: BYTEA→BLOB, JSONB/TEXT[]→JSON, ENUM→TEXT+CHECK, UUID→TEXT, tsvector→FTS5, pgvector→sqlite-vec. Chainlit's data layer (users/threads/steps/elements/feedbacks) needs a SQLite DDL variant.

---

## 11. Storage and infrastructure

- **SQLite + Litestream**: DB on local disk; Litestream replicates WAL to Azure Blob; `litestream restore -if-replica-exists` on boot; loss window ~1 s; retention 72 h (crypto-shred completes after retention). **Not** Azure Files (SMB locking unreliable with WAL). Exactly one replica.
- **Redis 8 sidecar**: `redis:8 --save '' --appendonly no --maxmemory 128mb --maxmemory-policy allkeys-lru`; `NAMI_REDIS_URL=redis://localhost:6379/0`. Not Azure Cache for Redis Basic/Standard (no vector support, ~$16/mo). LRU eviction is safe for every key family.
- **Azure**: Container Apps 1 replica (0.5 vCPU/1 GiB ≈ $12–15/mo after free grant; websockets OK; `transports=["websocket"]`), Key Vault Standard (RSA-OAEP-256 wrap, ES256 signing), Blob Hot LRS (cents). Total ≈ $15–20/mo + LLM (~$0.10–0.40/task) + Kernel free credit ($5/mo; headless ≈ $0.06/h, headful ≈ $0.48/h).
- **CI/CD**: GitHub Actions: tests → leak test → `pip-audit` → build (digest-pinned base) → ACR → ACA.

---

## 12. Verified facts and open verifications

**Verified (Oct 2026):**
- browser-use (installed 0.11.13; the 0.13 notes below were not what `uv` resolved; API used here confirmed against 0.11): `Browser(cdp_url, allowed_domains)`; `Agent(... sensitive_data={"https://*.site.com": {...}}, use_vision=False|True|"auto", extend_system_message, enable_planning, planning_replan_on_stall, max_failures, fallback_llm)`; `run(max_steps, on_step_start, on_step_end)`; `Tools()`+`@tools.action` (injected param must be named `browser_session`); `AgentHistoryList.is_successful()/final_result()/screenshots()/urls()/errors()`; `ChatAnthropic`, `ChatOpenRouter`. **`planner_llm`, `message_context` removed.** TOTP: placeholder name ending `bu_2fa_code`.
- Kernel: `browsers.create(stealth, headless, timeout_seconds, kiosk_mode, profile={"name","save_changes"})` → `cdp_ws_url`, `browser_live_view_url` (iframe; `?readOnly=true`), `session_id`; `delete_by_id` persists the profile; `persistence={"id"}` is EOL; one writer per profile; free tier $5/mo, 5 concurrent.
- Chainlit 2.12: `mount_chainlit(app, target, path="/chainlit")`; Google via `OAUTH_GOOGLE_CLIENT_ID/SECRET` + `CHAINLIT_AUTH_SECRET` + `@cl.oauth_callback`; `Depends(chainlit.auth.get_current_user)`; **cookie path follows `CHAINLIT_ROOT_PATH`** → vault under `/chainlit/vault/*` or `CHAINLIT_AUTH_COOKIE_PATH=/`; `cl.CustomElement` → `public/elements/*.jsx` (props are globals; `callAction` payloads persist in step history → never for secrets).
- Azure Key Vault: `CryptographyClient.wrap_key/unwrap_key(KeyWrapAlgorithm.rsa_oaep_256)`; Anthropic IDs `claude-sonnet-5-5`, `claude-opus-5-5`.
- Redis 8.10 (Homebrew) has Vector Sets, not `FT.*`; `VSIM` score = (1+cos)/2; redis-py 8.1 empty-reply bug.

**Verify before relying on (tick when cleared):**
- [x] Chainlit auth cookie reaches `/chainlit/vault/ping` via `get_current_user` — verified over HTTP: 401 without cookie, 200 with (dev login). Vault routes are registered before `mount_chainlit`
- [~] CustomElement JSX `fetch()` same-origin with `credentials:"include"` — verified (Connections sidebar loads vault data in a real browser). Kernel live-view `<iframe>` still unverified (needs a Kernel key)
- [x] browser-use `storage_state=` — not needed: CDP snapshot/restore works on any provider (tested on real Chromium). Note: Chromium drops session cookies on close, so snapshots are required, a profile alone is not enough
- [ ] Google OAuth callback URL with the `/chainlit` mount prefix
- [x] Chainlit `SQLAlchemyDataLayer` on SQLite — DDL in `app/chainlit_schema.py`; separate file `data/chainlit.db` (its `users`/`threads` tables collide with ours). Chainlit 2.12 also writes `autoCollapse`, `modes`, `icon` on steps (missing from older docs; found by running it). Thread history persists across reloads (headless Chromium)
- [ ] Kernel live view for headless browsers (else headful only for takeover)
- [ ] LangSmith OTLP ingest + LangGraph OTel setup
- [ ] Prompt Guard 2 licence / accuracy / FP rate on real pages
- [ ] SPIFFE JWT-SVID claim requirements; SPIRE agent on ACA
- [ ] `RedisVectorIndex` against `redis:8` Docker image
- [x] OWASP AST10 titles re-checked 2026-10-07 (unchanged; v1.0 2026 edition, in public review)

---

## 13. Repo layout (● built, ○ planned)

```
app/
  core/config.py ● container.py ●
  db/base.py ● engine.py ● models.py ●
  rules/engine.py ● policies.py ●
  memory/short_term.py ● long_term.py ● embeddings.py ●
  cache/exact.py ● semantic.py ● llm_cache.py ● redis_client.py ●
  security/secrets.py ●  gateway.py ○ rbac.py ○ scoped_session.py ○ identity.py ○ telemetry.py ○
  tenancy/service.py ●
  vault/{kms,crypto,service,totp,router}.py ○
  browser/{provider,kernel_provider,local_provider,session_manager,broker,executor,tools}.py ○
  agent/{graph,intent,access_planner,planner,validator,verifier,guard,memory_writer}.py ○  agent/prompts/ ○
  skills/{store,scanner,lifecycle}.py ○
  sites/registry.yaml ○
  eval/{golden_tasks.yaml,run_eval.py,drift/,injection_corpus.yaml} ○
  main.py ○  cl_app.py ○
public/elements/*.jsx ○   .chainlit/config.toml ○
migrations/ ● (01dda0890700)   scripts/smoke_storage.py ●
tests/ ● (db, rules, short_term, long_term, cache, secrets, redis_vectors, semantic_calibration)
infra/{Dockerfile,azure-deploy.sh,litestream.yml} ○
docs/ARCHITECTURE.md ● HARNESS.md ●
```

---

## 14. Run and verify

```bash
uv sync --extra embeddings            # deps + local bge-small
alembic upgrade head                  # data/nami.db
redis-server --port 6390 --save '' --appendonly no --daemonize yes
uv run pytest -q                                                   # fakeredis + in-memory vectors
NAMI_TEST_REDIS_URL=redis://localhost:6390/15 uv run pytest -q     # whole suite on real Redis (wipes db 15)
NAMI_REDIS_URL=redis://localhost:6390/0 uv run python -m scripts.smoke_storage
alembic check
```
Smoke result (2026-10-04): rules 7.8 ms cold / 2.4 ms cached; STM from Redis with OTP redacted; recall "what does ravi eat" → "Ravi is vegetarian" 0.86; exact hit; semantic hit 0.987.

End-to-end checks to add as phases land: leak test (fake login site, mock LLM transport: secret appears in zero requests/events/logs; no screenshots during auth subgoal); resume test (kill mid-task → Resume card → completes); UX walkthrough (connect → takeover → approve → result → revoke → re-ask); restore drill (delete DB → Litestream restore → paused task resumes); concurrency (agent events + vault writes, no `database is locked`).

---

## 15. Build plan — tick as you go

Each step lists what to build and the tests that must pass before ticking. Safety tests first.

### Phase 0 — Foundations ✅
- [x] Research, blueprint, decisions (2026-10-04)
- [x] Project skeleton: `pyproject.toml`, uv, pytest, `.gitignore`
- [x] SQLite engine with pragmas, `pool_size=1`, `UTCDateTime`; Alembic + initial migration; `alembic check` clean
- [x] Tenancy: orgs/users/memberships; personal org on first login — tests: pragmas, idempotent login
- [x] Rules engine + policy registry + versioned cache — tests: hard limit can't be loosened; user tightens/overrides soft; domain lists + site-specific; baseline approvals immutable; permissions; cache invalidation; works without Redis
- [x] Short-term memory (thread + channel, RPUSHX, folding) — tests: window + rebuild after flush; redaction; folding w/o overlap; channel spans threads; no-Redis
- [x] Long-term memory — tests: scope visibility incl. personal-org isolation; ranking + access bump; recency; dedupe + secret refusal; forget permissions; expiry
- [x] Exact cache, LLM cache policy, semantic cache (vectorset/search/memory) — tests: roundtrip + tenant isolation; never caches secrets or live-page purposes; paraphrase hit within partition only; plan/summary exact-only; expiry; Redis outage; vectorset roundtrip/cosine/isolation/expiry/cap on real Redis
- [x] Secret detector — tests: common secrets detected, ordinary text ignored, redaction
- [x] Semantic threshold calibration test (real model) — different intents below 0.95; near-verbatim above
- [x] Smoke script against real Redis 8.10 + migrated DB (30 fakeredis / 33 real Redis tests passing)

### Phase 1 — Spine (sign in, see a browser work, store a secret)
- [~] 1.1 Day-1 spikes: clear the first five §12 verifications — **done:** Chainlit cookie reaches `/chainlit/vault/*`; JSX `fetch()` same-origin with cookies; CDP cookie/localStorage restore (chosen over `storage_state=`). **Open:** Google OAuth callback path, Kernel live view in an iframe, LangSmith ingest
- [~] 1.2 FastAPI `main.py` + Chainlit mount + Google-only OAuth → `tenancy.ensure_user`; Chainlit SQLite data layer DDL; `transports=["websocket"]` — tests: non-Google provider rejected; user+personal org created; thread persists
- [x] 1.3 `BrowserProvider` + `KernelProvider` (Profiles, headless default, `delete_by_id`, per-user lock) + `LocalPlaywrightProvider`; CDP cookie/localStorage snapshot & restore — tests: lock prevents two writers; snapshot→restore round-trip on local provider; `close` always called on error
- [~] 1.4 Executor factory + step streaming + `LiveBrowserView` (readOnly) on a read-only demo task — tests: hooks emit `task_events`; screenshots to Blob (mock); step cap honoured
- [x] 1.5a Vault core: `KmsProvider` (`LocalKms`), AES-256-GCM envelope with AAD bound to user|credential|kind|origins, origin binding (registrable domain, offline PSL), credentials store/list/use/revoke/shred, append-only access log (denials logged), TOTP parse/verify/fill-code — migration `1c1da8c1656f`; 84 tests (52 crypto/origin/TOTP + 32 service), 4 mutation checks confirm tests fail when origin check, AAD origins, user check or revoke-wipe is removed (2026-10-04)
- [~] 1.5b Vault edges: **built** — Azure Key Vault `KmsProvider` (code, untested without Azure), `/chainlit/vault/*` routes (list/create/revoke/TOTP preview/sessions/log; CSRF header + origin check), `SecureInput` + `Connections` + `ApprovalCard` + `LiveBrowserView` elements, chat prefilter that redacts the saved message. **Verified by hand over HTTP and in headless Chromium** (see 2026-10-04 build log). **Missing:** automated tests for routes/prefilter, TOTP QR upload

### Phase 2 — The agent
- [~] 2.1 LangGraph graph skeleton (`intent → access_plan → plan → execute ⇄ validate → verify → remember`), `AsyncSqliteSaver` in `nami.db`, `interrupt()` for the three interrupts, `Command(resume)` from Chainlit callbacks; `tasks`/`task_events` tables — tests: pause/resume round-trip; resume after process restart; every model-visible input logged
- [x] 2.2 Intent (LLM cache wired), Access Planner (CaMeL: scope from user text only), Planner (criteria + irreversibility + is_auth), Validator, Verifier — tests: page content in later steps cannot add a domain/credential; validator fails on executor claim without evidence; verifier confidence <0.7 → unverified
- [~] 2.3 Credential Broker (sensitive_data map from approved creds only, unique placeholders, origin binding both frames, TOTP, redaction-by-value) + Guard (4 deterministic layers + classifier) + interrupts incl. interactive takeover and hand-back — tests: **leak test**; wrong-origin/iframe fill refused; lookalike eTLD+1 refused; purchase without approval blocked at all three layers; takeover sends zero screenshots
- [x] 2.4 Session Manager (profile lock, snapshots, health check, re-auth state machine) + site hints for swiggy/vercel/openrouter/google — tests: expired session → re-auth → same subgoal resumes; takeover path when no password; snapshot encrypted with user DEK
- [ ] 2.5 Context management: ephemeral observations, frozen prefix (cache-hit metric), plan recitation, errors retained, tool masking via hooks — tests: prefix byte-identical across steps; old observations stubbed; error record persists until pass — **not built** (needs a live executor to measure)
- [~] 2.6 Cards (Connection/Approval/NeedsYou/Result/Failure), proactive suggestions after connect, Stop/Pause always visible — tests: approval card shows items/amount/address; failure card offers 3 actions — **built:** Approval, Failure, Takeover cards, Result card (Custom Element, redacted props, no screenshot in props). **Missing:** proactive suggestions after connect; NeedsYou card for UPI pushes
- [ ] 2.7 Golden tasks end-to-end: (a) Vercel via Google takeover, second run instant; (b) OpenRouter key → credits/models + honest "no video"; (c) Swiggy reorder with approval + UPI NeedsYou — recorded runs + judge scores — **needs real keys**; `app/eval/golden_tasks.yaml` lists the tasks and pass criteria; the API route for (b) is built and tested against a fake API

### Phase 3 — Security plane (§8)
- [~] 3.1 `Principal`, `can()`, `ScopedSession`, `channel_members`, `thread.visibility`, `authz_log`, `viewer` role — tests: cross-org matrix (every table × role) all denied; admin can't read private thread/personal memory; viewer can't start tasks; unscoped query fails; static import ban — **added:** `ScopedDatabase` (auto org filter on every SELECT/UPDATE/DELETE incl. joins/subqueries/aliases + cross-org write refusal; found and fixed a cached-lambda bug that would have leaked the first org's filter), static allow-list of raw-session modules. Adopted so far by the skills service only
- [ ] 3.2 Org DEKs, org in Redis prefixes, org in Kernel profile names — tests: org A key can't decrypt org B; prefix flush isolation; AAD includes org — **deliberately not built**: credentials are never shared, so there is no firm-owned secret for an org key to protect yet
- [~] 3.3 `IdentityIssuer` + verifier + JWKS (ES256 via Key Vault / local key in dev) — tests: expired/wrong-aud/tampered/replayed rejected; scopes = intersection; replay store down → vault/broker fail closed; exp ≤ 120 s
- [~] 3.4 Gateway L1, L2, L5 — tests: hidden/zero-width payloads removed; secret-shaped egress blocked; off-plan domain blocked; wrong-origin placeholder refused
- [~] 3.5 Gateway L3, L4 + injection corpus (direct, hidden-text, markdown-image exfil, fake tool output, fake system message, password form, lookalike redirect, multi-step) — tests: 0 leaks, 0 off-plan actions across corpus; classifier recall/FP recorded; plan not extendable by page content — **built:** injection corpus (10 attack shapes + 8 benign lines, FP recorded), skill scanner corpus. **Missing:** model-based classifier (L3), per-tool-call capability policy (L4), recall on real pages
- [x] 3.6 OTel + LangSmith + Azure Monitor wiring; redaction before export — tests: exported spans contain no secret/placeholder value/screenshot/page text; every span has org/task/spiffe attrs; denied authz emits span + metric — built: `app/telemetry.py` (OTel, redacting exporter, salted user hash, counters, per-task token usage); 9 tests + smoke checks. **Unverified:** LangSmith OTLP ingest, Azure Monitor

### Phase 4 — Skills (§9)
- [x] 4.1 `skills` + `skill_events` tables, Pydantic schemas, hash + sign + verify — tests: tamper → refuse; schema fuzz; sig round-trip; revoked never loads
- [x] 4.2 Deterministic scanner + corpus — tests: 100% corpus-bad blocked; benign FP rate ≤ agreed bound — corpus is synthetic
- [x] 4.3 Manifest derivation + load-time intersection; placeholder stripping — tests: cannot widen plan/RBAC/token — manifest ∩ access plan built; RBAC/token-scope intersection not
- [x] 4.4 Lifecycle statuses, approval UI, `NAMI_SKILLS_FREEZE`, "Learned workflows" panel, `skills_require_approval` policy — tests: approval matrix by role; freeze rejects writes; UI == DB — approval by role, freeze flag, `skills_require_approval` policy, Learned workflows panel + API
- [x] 4.5 Judge layer — tests: judge-targeting injection cannot raise trust — judge can only demote
- [x] 4.6 `run_cdp_snippet` sandbox in remote browser + egress policy — tests: non-plan fetch blocked; timeout; size cap — policy layer on real Chromium (isolated world, timeout, size cap, off-plan requests aborted)
- [x] 4.7 Nightly re-verify/re-scan; manifest-vs-behaviour drift — tests: stale sig flagged; drift → candidate — background loop wired into app start-up
- [~] 4.8 Supply-chain CI: lock hashes, `pip-audit`, image digest, model-weight hash — CI fails on violation — CI workflow, digest pins and lock-hash tests written; **CI has never run** (no repo/runner), model-weight hash not applicable yet

### Phase 5 — Reliability and evidence
- [x] 5.1 Crash-resume (startup marks `running` → `blocked` + Resume card; reattach by session_id else fresh browser + snapshot), stall detection (same URL+action ×3 → replan), Reflexion retries — tests: resume test; stall triggers replan
- [x] 5.2 Drift harness: fake login site (TOTP) + "FoodCo" reorder flow; injector (renamed selectors, moved buttons, modals, mid-task session expiry, slow loads, A/B layouts, injection text) — metrics per mode: success, leaks (=0), approval adherence (=100%), tokens/success — `app/eval/` FoodCo site with 7 drift modes, brittle vs adaptive scripted agents, real orchestrator; results in `docs/EVAL_RESULTS.json`. Scripted model: measures the system around the model only
- [~] 5.3 Calibrated judge: ~30 hand-labelled trajectories; report agreement + chosen confidence threshold — calibration machinery + 35 synthetic labelled trajectories; **not calibrated on real pages**; 0.7 remains a placeholder
- [~] 5.4 Eval runner over golden tasks × N; replay from event log on prompt/model/compaction change; track tokens/success and cache-hit rate — runner for scripted mode, golden tasks file; `--real` and replay-from-event-log not built

### Phase 6 — Ship
- [~] 6.1 Dockerfile (digest-pinned), Litestream config, Redis sidecar, ACA deploy script, Key Vault + Blob wiring, GitHub Actions pipeline — digest-pinned Dockerfile (non-root, healthcheck), Litestream config + entrypoint, Container Apps manifest, `deploy.sh`, CI workflow; lint-tested only, **never built or deployed** (no Docker/Azure here)
- [x] 6.2 `/admin/metrics` page (tokens, cost, cache-hit, steps, verifier confidence, gateway blocks) — tokens/cost per task, cache hit ratio, steps, verifier histogram, gateway blocks (counts only, no content)
- [~] 6.3 Restore drill, concurrency test, deployed smoke (logs contain no secret values; Key Vault audit entries per task) — concurrency test (vault + events + external writer, no lock errors) and a backup/restore drill built; Litestream restore and deployed smoke not run
- [~] 6.4 2-minute demo; final numbers into `HARNESS.md`; README with run instructions — `ACCESS_MANAGEMENT.md`, `LIMITS.md`, `DEMO_SCRIPT.md`, README done; **the video is not recorded** and live beats need keys

---

## 16. Known limits and tradeoffs (keep current for the submission note)

- Passkeys, push approvals and CAPTCHA need the user (an offered one-time window); SMS/email codes are asked for in chat; Gmail/Drive use Connect Google; session persisted afterwards. Policy, not a bug.
- Live sites change; success is probabilistic; we show verification confidence.
- TOTP seed + password in one vault = one factor at the vault layer; disclosed with a badge; takeover recommended for high-value accounts.
- Card handling is demo-grade (not PCI); production = Stripe Issuing / Link one-time cards.
- One container, one replica; scale-out = worker + queue + Postgres/RLS (ledger and `ScopedSession` already allow it).
- Crypto-shred completes only after Litestream retention (72 h).
- Semantic cache recall is deliberately low (intent-only, ≥0.95).
- Instructions/preferences are guidance; only policies are enforced in code.
- Prompt injection is bounded, not solved; classifiers will miss novel attacks.
- `RedisVectorIndex` untested locally; OpenRouter free models may log prompts → user text only, never page content or vault data.
- Learned text skills can still mislead within scope; approvals and verification keep it small and visible.

---

## 17. Changelog

- 2026-10-04 — Blueprint; stack decisions; harness patterns; LangGraph orchestrator; drift harness; SQLite replaces Postgres; Redis 8 sidecar added (cache-only); storage layer built; semantic cache restricted to intent at 0.95 after calibration; Vector Sets backend replaces Query Engine as default; security plane (§8) and OWASP AST10 skills plan (§9) specified; docs consolidated into `ARCHITECTURE.md` + `HARNESS.md`.

---

## Build log: end-to-end app (2026-10-04)

**Everything skipped, unverified, or done differently is itemised in [`SKIPPED.md`](SKIPPED.md).** `[~]` below means built but not fully tested; `[x]` on 5.1 is verified by `scripts/smoke_graph.py` (restart mid-approval, resume after restart, injection quarantine, off-plan URL, stored-login re-auth, intent cache, spend limit, card release only after approval, roles and task ownership), not by pytest.

Order of work was changed on request: build the whole app first, add test suites after. Code is structured so tests drop in (services take their dependencies in constructors; the model and executor are injectable).

**Built and run** (not yet covered by pytest unless noted)
- `app/main.py` FastAPI + Chainlit mount; migrations on startup; `/healthz`.
- `app/cl_app.py` Chainlit UI: Google-only OAuth (registered only when Google creds exist), dev login behind `NAMI_DEV_AUTH=1`, app refuses to start with neither; secret prefilter; streamed progress; ask / approval / takeover / result / failure cards.
- `app/agent/*` intent, access planner (**plain code**, user text only), planner, validator, verifier, guard, LangGraph orchestrator with pauses as separate pure nodes (LangGraph re-runs a node on resume), retries ×2 → replan ×1 → failure card, SQLite checkpointer.
- `app/browser/*` provider interface (+local, +Kernel), executor (browser-use), broker (placeholders, redaction by value), session manager (encrypted per-site snapshots, health check), CDP snapshot/restore/clear.
- `app/vault/*` crypto, KMS (local, Azure), service, origins, TOTP, HTTP router.

**Verified**
- pytest: 140 passed / 4 skipped (storage 30, vault 84, browser provider/Kernel/local 26). Includes mutation checks on the vault and browser layers.
- `scripts/smoke_graph.py`: 66/66 orchestration checks + `scripts/smoke_authz.py` 568/568 + `scripts/smoke_identity.py` 29/29 with a scripted model and executor on real DB, vault and Chromium (takeover → approval, denial, help hand-back, retries/replan, validator disagreement, unverified result, no secrets in persisted events).
- Live app in headless Chromium: login, pasted key redacted in the saved thread and never sent anywhere, missing-model-key message, thread history persists, sidebar loads vault data through the session cookie. Vault HTTP routes: 401 without cookie, 403 without CSRF header, 400 on invalid input; secret absent from server log.

**Not verified (needs keys or infrastructure)**
- Anything calling Claude for real: the browser-use executor loop, real validator/verifier output quality, success rates. **No real website task has been run.**
- Google OAuth end to end (callback URL under the `/chainlit` mount).
- Kernel provider against the real API; Kernel live-view iframe; Azure Key Vault; Dockerfile build; Litestream/Redis sidecar.
- Card use is stored but never injected into a page yet.

**Known issues**
- Executor hook attributes (`agent.history.model_thoughts()[-1].next_goal`) are guarded by try/except but unconfirmed against a live run.
- Non-standard TOTP (not 6 digits/30 s/SHA1) falls back to asking the user.
- The exit-time `libc++abi` abort is fixed by sharing one Playwright driver (`app/browser/cdp.py`); root cause not isolated.
- Full list: [`SKIPPED.md`](SKIPPED.md).


## Build log: tests, then the remaining phases (2026-10-05)

**Built** (all with tests)
- Test suites for the previously untested app: broker leak test, vault routes, chat prefilter + Google-only auth, access planner, guard, injection gateway corpus, LLM wrapper, agent stages, admin, Chainlit schema drift (drives the real data layer), migrations, executor (fake browser-use agent), session manager, UI end to end in Chromium; smoke scripts wrapped by pytest.
- Phase 3: `ScopedDatabase` (org filter on every query), identity key rotation by `kid` + concurrent replay tests (fake and real Redis), OpenTelemetry with redaction before export (`app/telemetry.py`), per-task token usage.
- Phase 4: skills (`app/skills/`): strict schemas, scanner, ES256 signing, lifecycle (auto-approve text, hash-approved code, admin-approved org skills, freeze, `skills_require_approval`), judge (can only demote), snippet sandbox on real Chromium, nightly re-verify loop, org-scoped lookups, "Learned workflows" panel + API; the orchestrator now guides planning only from approved, verified skills.
- Phase 5: drift harness (`app/eval/`: FoodCo site with 7 drift modes, brittle/adaptive scripted agents, real orchestrator), report `docs/EVAL_RESULTS.json`, calibration machinery (synthetic trajectories), `--real` runner (never run), golden tasks file.
- Phase 6: digest-pinned Dockerfile, Litestream config, Container Apps manifest, `deploy.sh`, CI workflow, supply-chain and production-config tests, richer `/admin/metrics`, concurrency test and backup/restore drill, docs (`ACCESS_MANAGEMENT.md`, `LIMITS.md`, `DEMO_SCRIPT.md`, README).
- New capability: server-side read-only API route for saved API keys (OpenRouter registered), so "check my credits" needs no browser; free-model routing for the intent call (off by default).

**Verified**
- `pytest`: 408 passed / 4 skipped with fakeredis; 411 passed / 1 skipped with real Redis 8.10 (`NAMI_TEST_REDIS_URL=redis://localhost:6390/15`). `alembic check` clean.
- `smoke_graph.py` 81/81 (adds skills, API route, planner failure, telemetry checks), `smoke_authz.py` 568/568, `smoke_identity.py` 29/29.
- Drift harness (scripted model): 32 runs, **0 secret leaks, 100% approval adherence**; the adaptive scripted agent completes all 7 drift modes, the brittle one fails 4 (so the injector bites). This measures the system around the model, not real-world success.

**Real defects the new tests found (fixed, each with a regression test)**: secret-printing `repr` on the broker payload; a pasted API key stored as the thread title; cross-org filter reuse in the first `ScopedDatabase`; `KeyError: 'plan'` when the planner fails; plus guard-rail catches (undeclared dependencies, raw-session allow-list).

**Not verified**: unchanged from before: no live model, Google, Kernel, Azure, or container build. See [`SKIPPED.md`](SKIPPED.md) and [`LIMITS.md`](LIMITS.md).
