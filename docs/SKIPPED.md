# Skipped, unverified and shortcut items

Everything that is **not done, not tested, or done differently from the plan** (updated 2026-10-07, after the live tests with the real model).
If it is not on this list and not ticked in `ARCHITECTURE.md`, assume it is not done.

Legend: **Close it** = the smallest thing that finishes it. **Risk** = what can go wrong meanwhile.

---

## A. Tests: what exists, what is still missing

Last full run (2026-10-07, fakeredis): `uv run pytest` → **1010 passed, 4 skipped, 0 failed** (4 min 48 s, after the live-test fixes). The first run after the test pause had 30 failures; all were fixed (see "Test run after the pause" and "Live tests with the real model" at the end). The real-Redis run (`NAMI_TEST_REDIS_URL=redis://localhost:6390/15`) has **not** been repeated since 2026-10-05.
Mutation habit kept: for each safety rule I broke it on purpose and confirmed a test fails (broker repr, CSRF check, prefilter, schema, vision-off, redaction, snapshot AAD, scanner, signature, egress guard, approval gate, card release, scoped filter cache).

| # | Area | Status |
|---|---|---|
| A1 vault routes | **Done** (`test_vault_router.py`) |
| A2 chat prefilter, thread-name/message redaction | **Done** (`test_cl_app.py`, `test_ui_e2e.py`) |
| A3 Google-only auth | **Done** (unit + subprocess import check). Real Google not tested (B3) |
| A4 access planner | **Done** (`test_agent_gates.py`) |
| A5 guard | **Done** |
| A6 orchestrator | **Partly.** Covered by `smoke_graph.py` (81 checks, run under pytest by `test_smoke_scripts.py`) and the drift harness. **Missing:** native pytest unit tests per node (pause/resume at node level, retry/replan counters in isolation) |
| A7 broker leak test | **Done** (`test_broker.py`); found a real leak (A-found 1) |
| A8 executor | **Done with a fake browser-use Agent.** The real library path is unverified (B1) |
| A9 session manager | **Done** |
| A10 gateway | **Done** on a small corpus (10 attacks, 8 benign). **Recall on real pages unknown** |
| A11 intent/validator/verifier | **Done** with a recording model. Real model behaviour unmeasured |
| A12 llm wrapper | **Done** (+ OpenRouter client, routing) |
| A13 admin | **Done** |
| A14 Chainlit schema drift | **Done** (drives the real data layer) |
| A15 migrations | **Done** (`alembic check`, round trip) |
| A16 UI | **Partly.** Login, wrong password, redaction, reload history, vault via same-origin fetch, no server errors. **Missing:** approval card, takeover card, resume prompt, Connections list/revoke clicks, secure-form UI |
| A17 exit code | **Done** |
| A18 authz | **Done via `smoke_authz.py` (568 checks)** wrapped in pytest, plus `ScopedDatabase` tests. **Missing:** a DB-level matrix "every org-owned table × every role" through real services (only skills adopt the scoped session) |
| A19 identity | **Done**, incl. concurrent replay (fake and real Redis), expiry, rotation by `kid`, fail-closed. Not done: Key Vault–held signing key |
| Skills, API route, telemetry, drift harness, calibration, ops, supply chain | **Done** (see test files) |

**Not covered anywhere:** the real browser-use agent loop; Kernel; Azure Key Vault; Google OAuth; a real model; the Dockerfile build.

---

## B. Built but never run against the real thing

| # | Item | Why | Close it | Risk |
|---|---|---|---|---|
| B1 | browser-use executor loop with Claude | **Verified live (2026-10-07) on public sites and a local fake shop with Haiku 4.5:** read tasks, multi-step reads, saved-password login, typing, approval, TOTP (see the last section). **Not yet:** real sites with logins, `request_help` at a code box, the golden tasks; each case ran 1-4 times | Run the golden tasks; read the logs | Only one trivial public page tried; `agent.stop()` and `request_help` unconfirmed live; hook failures are swallowed |
| B2 | Real validator/verifier quality, success rate, cost per task | **Partly measured (2026-10-07):** planner+validator ≈ $0.01/task, but the browser agent's own tokens are not counted, and the validator passed a wrong answer once. No success rate | Golden tasks × N; `/admin/metrics` | No success number may be presented as measured |
| B3 | Google OAuth end to end | No credentials | Create the OAuth client; redirect `https://<host>/chainlit/auth/oauth/google/callback` | Callback path under the mount unverified |
| B4 | `KernelProvider` against the real API | No key | One headless run + one takeover; confirm `delete_by_id` persists the profile | Written against SDK 0.119 fields; unit-tested with a fake client |
| B5 | Kernel live view iframe in `LiveBrowserView` | Same | Check embedding and `?readOnly=true` | Hosted takeover may not render |
| B6 | Headless takeover | Plan assumed a live view exists for headless | Test on Kernel | Unknown; connect step uses `headless=False` (≈8× cost on Kernel) |
| B7 | Azure Key Vault KMS | No Azure | Create vault + RSA key; `NAMI_KMS_PROVIDER=azure` | Untested path for wrapping every user's key |
| B8 | Dockerfile, Litestream, Container Apps manifest, `deploy.sh`, CI workflow | No Docker/Azure/GitHub runner | Build, deploy, run CI once | Digests were fetched from the registries on 2026-10-04 but the image was never built; `deploy.sh` never run; `pip-audit` never run |
| B9 | Redis 8 vector set in the running app | `NAMI_REDIS_URL` unset in dev | Set it | Caches off; correct, slower |
| B10 | `RedisVectorIndex` (Query Engine) | Not in Homebrew Redis | Test on `redis:8` image | Unused code |
| B11 | Chainlit behind a proxy / ACA (websocket, cookie path) | Not deployed | Smoke after first deploy | Cookie scope may break `/chainlit/vault/*` |
| B12 | Prompt Guard / model-based classifier | Classifier built 2026-10-07 (off by default). Tried live on 12 snippets (5/6 injections flagged, 1/6 benign flagged, +0.87 s median); too few to call an accuracy; not run on live pages | Evaluate on real pages | Regex heuristics only |
| B13 | LangSmith OTLP ingest | No account | Point `NAMI_OTEL_ENDPOINT` at it | Exporter code is tested with a fake sink only |
| B14 | OWASP AST10 final titles | Not re-checked | Re-fetch the list | Doc may cite stale titles |
| B15 | OpenRouter API paths (`/credits`, `/models`, `/key`) | Never called against the real host; the call path was tested against a local mock on 2026-10-07 | Call once with a real key | The registry entry follows public docs and is marked unverified |

---

## C. Planned steps not (fully) built

| Step | What is missing | Notes |
|---|---|---|
| 1.4 | Screenshots to Blob; read-only `LiveBrowserView` demo | Final screenshot is shown from memory and not stored |
| 1.5b | ~~TOTP QR upload~~ **Closed 2026-10-07** (BarcodeDetector, no jsQR) | Chromium only; the UI test checks the source, not a real QR image |
| 2.1 | ~~Agent-side `ask_user`/`confirm_action` tools~~ **Closed 2026-10-07** | Tests pass; whether the model uses them sensibly is unmeasured |
| 2.3 | Guard classifier for the ambiguous remainder; frame-level origin check for placeholders | browser-use matches only the URL pattern. Spend total is read by a model |
| 2.4 | More site hints; rejected stored password not distinguished from other failures | |
| 2.5 | ~~Context management~~ **Closed 2026-10-07** (compaction, plan recitation, tool masking) | Tests pass; the effect on a live executor is unmeasured |
| 2.6 | Proactive suggestions after connect; NeedsYou card for UPI pushes | |
| 2.7 | Golden tasks end to end on real sites; recorded runs; judge scores | Needs keys and accounts (`golden_tasks.yaml` is the spec) |
| 3.1 | `channel_members`; enforcing `read_thread/read_task`; `ScopedDatabase` adopted outside skills; RBAC inside vault routes | Services still filter by hand. A static allow-list stops *new* modules from using raw sessions |
| 3.2 | Org DEKs / org in Redis prefixes / org in Kernel profile names | **Deliberate:** credentials are never shared, so nothing org-owned needs an org key yet |
| 3.3 | Tokens enforced only at the vault audience; signing key is a local PEM (not Key Vault); not SPIRE | |
| 3.4 | Gateway covers my validator/verifier and API responses, **not the executor's own page input** | Library limitation |
| 3.5 | ~~Model-based classifier (L3); per-tool-call capability policy (L4)~~ **Built 2026-10-07**, tests pass | L3 is off by default and its accuracy on real pages is unmeasured; L4 runs after the action |
| 3.6 | LangSmith / Azure Monitor not verified; no metrics exporter (counters are in-process and shown in `/admin/metrics`) | |
| 4.x | ~~Skills import/export (AST10)~~ **Closed 2026-10-07.** Still open: signing with Key Vault; skill sharing UI beyond approve/revoke; judge never run against a real model | |
| 5.3 | Real calibration | The 35 trajectories are synthetic and labelled by construction. 0.7 is still a placeholder |
| 5.4 | ~~`--real` runner; replay from the event log~~ **Closed 2026-10-07** (`--replay` re-plans a stored task and diffs; planning only) | |
| 6.1–6.3 | Anything needing Docker/Azure: build, deploy, Litestream restore, deployed smoke | |
| 6.4 | The recorded 2-minute demo; final real numbers in `HARNESS.md` | Docs written; no video |
| — | Free-model routing is wired (`NAMI_FREE_MODEL` + OpenRouter key) but default is off | Only the `Intent` call may go to a free model; never anything derived from a page |
| — | ~~Workflow memory as AWM with parameterised steps~~ **Closed 2026-10-07** | Old rows still load |

### C2. Found by the 2026-10-05 re-audit against the original blueprint (were missing from this file)

| Item | State |
|---|---|
| OAuth connect flow (`/oauth/google/start`, callback, Gmail read for email OTPs) | **Built later (see "Connect from the website"); still needs real Google credentials to verify.** Original note: **Not built.** The vault accepts `kind="oauth"` (refresh_token) but nothing obtains or uses a token; no Gmail OTP extraction. Needs Google OAuth credentials to verify |
| `/tasks/*` routes (ledger read, resume, cancel over HTTP) | **List, ledger and cancel built 2026-10-07 (tests pass); resume over HTTP deliberately not built.** Original note: **Not built.** Resume/cancel work only through the chat (`on_message`, buttons, `on_stop`) |
| Screenshots/recordings in Azure Blob | **Not built** (also C 1.4); no `azure-storage-blob` dependency |
| Postgres, `docker-compose.yml` | Replaced by SQLite + Litestream (decision, D1); no compose file needed |

Layout differences from the blueprint (function exists, file name differs): secret detector is `app/security/secrets.py`; memory is `app/memory/`; there is no `core/db.py` or `core/events.py` (`app/db/`, orchestrator events). The blueprint's separate `ConnectionCard`, `NeedsYouCard`, `FailureCard` and `TotpSetup` elements do not exist: a missing connection is a takeover/ask message, failures are a message with Try again / I'll do it myself buttons, and TOTP setup is a `kind` inside `SecureInput` with a live code preview.

---

## D. Deliberate shortcuts and deviations

| # | What was done instead | Consequence |
|---|---|---|
| D1 | Chainlit data in `data/chainlit.db`, LangGraph checkpoints in `data/graph_checkpoints.db` | Three SQLite files; Litestream config lists all three |
| D2 | Access plan is plain code, not an LLM | Cannot infer sites the intent step did not name |
| D3 | Dev login (`NAMI_DEV_AUTH=1`) | Never set in production; the manifest test asserts it is absent; app refuses to start with no auth |
| D4 | ~~Non-standard TOTP is not injected~~ **Closed 2026-10-05:** Nami generates the code from the vault (`vault.totp_code`) and injects it as `cred_*_2fa_code` | Verified live 2026-10-07 on a fake site (6-digit/30 s and 8-digit/60 s) |
| D5 | Live browser sessions held in memory | Restart loses the live session; tasks resume from profile + snapshot |
| D6 | Takeover with the local provider opens a headful window on the server machine | Dev only |
| D7 | Vault auth reads `metadata.user_id` from the Chainlit JWT | `CHAINLIT_AUTH_SECRET` must be strong and secret |
| D8 | ~~`/admin/metrics` anonymous callers get 401~~ **Closed 2026-10-07:** anonymous and non-admin callers both get 404 (checked on the running server) | |
| D9 | Alembic runs at start-up | One replica only |
| D10 | Executor hook errors are swallowed | Failures in progress streaming are silent |
| D11 | The drift harness uses a scripted model and scripted agents | It measures the system around the model; it says nothing about real-world success |
| D12 | A skill's "observed domains" come from the final URL of each validated subgoal | Coarse; a page visited and left mid-subgoal is not counted |
| D13 | API-key use is limited to a fixed read-only endpoint list per site (only OpenRouter registered) | Other API providers need a registry entry |

---

## E. Honest limits to state in the write-up

1. No task has been run end to end on a real website that needs a login. Public read tasks (example.com, Wikipedia) and a local fake shop with login, TOTP, approval and an API mock were run with the real model on 2026-10-07, 1-4 runs each; those are not success rates.
2. Single-user safety is the strongest part. Multi-org isolation has a pure decision function, an org-scoped session and tests, but most services still filter by hand.
3. Prompt-injection defence is layered but partial: deterministic limits are real; sanitising the executor's own page input is not possible with the current library.
4. Semantic-cache and storage numbers in `HARNESS.md` are measured; nothing about real task success is.

---

## F. Known defects

| # | Defect | Status |
|---|---|---|
| F1 | `libc++abi: recursive_mutex lock failed` at exit | **Fixed** (one shared Playwright driver per loop) |
| F3 | `n_run` health check navigates the real auth-check URL once per task on session sites | **Fixed 2026-10-07** (passing check cached 300 s per user+site; cleared on revoke). Tests pass |
| F4 | A replan restarts at subgoal 1 and re-does early steps | **Fixed 2026-10-07** (evidence kept, planner told to continue from the current page, no second sign-in, old approvals cleared). Tests pass |
| F6 | `/admin/metrics` returns 401 to anonymous callers, which reveals the route | **Fixed 2026-10-07** (404 for anonymous and non-admin). Tests pass |

### Found by the first live run (2026-10-05)
1. Claude 5 models reject the forced `tool_choice` browser-use 0.11 sends (HTTP 400), so the executor never left `about:blank`. browser-use 0.13 fixes this upstream but pins `pyotp==2.9.0` (we need >=2.10) and ~15 other exact versions, so we stayed on 0.11 and added `app/browser/llm.py`: `tool_choice` -> `auto`, a format reminder, and a deterministic repair when the model sends one action's arguments at the top level or the string "null". `strict` tools were tried and rejected by the API (grammar too large).
2. The agent no longer has `evaluate` (model-written JavaScript), file actions, `find_elements` or `search`; browser-use's own judge is off (our Validator/Verifier judge). 19 tests + 3 mutation checks.
3. `NAMI_ALLOWED_EMAIL_DOMAINS` compared only the domain, so a full address (as the owner set it) would have locked them out. Now accepts both.
4. Google sign-in works with the `/chainlit` mount (callback path verified).
5a. Google showed "This browser or app may not be secure" during takeover sign-in: Playwright's Chromium is launched with `--enable-automation` and `navigator.webdriver = true` (measured). The local provider now starts real Google Chrome as a plain process (own profile folder, debugging port, no automation flags) and attaches over CDP later; `webdriver` is False (measured), and the process command line has no automation switch (tested). Chrome is closed with the DevTools `Browser.close`, because a bare SIGTERM lost cookies written seconds earlier (found by a test). Fallback without Chrome: Playwright Chromium with the automation switches off. **Not verified:** that Google actually accepts the sign-in; that needs a real attempt. Config: `NAMI_LOCAL_BROWSER=auto|chrome|playwright`, `NAMI_CHROME_PATH`. Kernel (hosted) is a different browser and was not touched.
5b. After Google accepted the sign-in, Nami said "I did not see a sign-in to gmail.com. Nothing was saved." Cause (measured in the profile): the sign-in set 50 cookies on google.com (SID, SAPISID, __Secure-1PSID...) and 0 on gmail.com, and the snapshot only keeps cookies of the requested site. Also the access plan allowed only gmail.com, so mail.google.com would have been blocked. Fix: `aliases` in `app/sites/registry.yaml` (gmail.com, googlemail.com -> google.com) applied in the access step; Google got an auth-check URL; saving a session now requires the site's own sign-in check to pass (a cookie existing is not proof: Google sets cookies for anyone). **Not verified:** a real Gmail task end to end (opening Gmail, composing, the approval before send). Other multi-domain logins (e.g. a site that signs in on a different registrable domain) need an alias entry each.
5c. Starting Chrome as a plain process means it outlives a crashed server and keeps the profile folder locked (found when a crashed smoke run left one behind). Each launch now writes `<profile>.chrome.json` (pid, port); the next open of that profile stops a leftover Chrome first, but only if its command line carries that exact profile folder (a recycled pid never gets an unrelated process killed). Not covered: Windows (no reaping), and a Chrome the user started by hand on the Nami profile folder would be stopped too.
5d. Gmail task, first live run after sign-in worked (session saved as google.com, subgoal 1 validated: compose window open). Two more failures found in the log: (1) with `tool_choice: auto` the model sometimes writes its whole answer (reasoning plus the JSON with the `action` list) inside the single `thinking` field; `repair_tool_input` now finds a JSON object with an `action` list inside any string field, with reasoning that contains other braces tolerated and the search bounded. (2) Attaching to Chrome over CDP failed once right after the sign-in ("Browser context management is not supported"), the check and then the save both threw, and the graph crashed with a raw error. `cdp.connect` now retries the attach (4 tries, backoff), and an unreadable browser at hand-back re-prompts "click Done again" (max 3, browser stays open) instead of crashing. **Root cause of the attach failure is not identified** (it was transient: the next attach a minute later worked); the retry is a mitigation. **Still open from this task:** the planner splits "send a mail" into one subgoal per field (recipient, subject, body), each costing a run; merging the pre-send steps (keeping the irreversible send separate) would cut cost, not done.
5e. Drive task ("summarize the internships in ManishFinal.pdf"): subgoals 1 and 2 passed, subgoal 3 reported it could not find the resume. Cause (reproduced live): each subgoal is a fresh browser-use agent, and browser-use opens any URL found in the task text first; our "Stay on: google.com" made every subgoal start with `navigate(https://google.com)`, wiping the page the previous subgoal had left open (subgoal 2 re-navigated and spent ~12 steps; subgoal 3 left the open PDF). Fix: `directly_open_url=False`; the agent is told which page is already open (or that it is blank); the verified results of earlier subgoals are passed as labelled data (latest 4, bounded, redacted). **Not verified:** that the agent can then READ a PDF in the Drive viewer (text may be canvas-rendered and not in the DOM); the Drive task has not been rerun. Also noticed: `model_main` in `app/core/config.py` is now Haiku 4.5 (earlier runs used Sonnet 5.5); Haiku is cheaper but weaker at multi-step browsing; I did not change it.
5f. Drive task hit "This browser or app may not be secure" and Nami said "a browser window is open" when none was. Measured with the real profile, headless AND visible: the saved Google session had EXPIRED (59 google.com cookies present, but myaccount.google.com -> google.com/account/about/ and drive.google.com -> accountchooser). Not a headless problem. Two Nami faults: (1) the sign-in health check only knew accounts.google.com as "signed out", so the signed-out landing page /account/about passed as signed in and the task ran instead of asking to sign in again; now Google has `signed_out_patterns` and `expect_url` (the check must end on myaccount.google.com), plus a short wait for client-side redirects; (2) a help takeover from a headless browser had no window; the browser is now relaunched with a window on the page the agent was on (hosted browsers with a live view are left alone). **Why the session expired is not known.** Likely suspects: cookie rotation lost when Chrome was stopped before saving, or Google's own risk checks; I did not prove either. Expect to sign in again whenever Google decides so; the saved-session approach has no guarantee. Also noticed, not fixed: the event scrubber turns a long digit run inside a URL parameter into `[REDACTED:card_number]` (cosmetic, but it alters stored URLs).
5g. Drive resume task looped (summary rejected, retry, replan, restart at subgoal 1). Cause in Nami: the validator prompt carried `executor_claim[:500]`, so a correct multi-item summary was cut mid-sentence and rejected as "incomplete, covers 1 of 4". Now the claim goes whole (up to 6000 chars) inside an untrusted-data wrapper, and the validator is told not to judge a report by its length. Second cause: the model's nested answer sometimes lacks its final closing bracket; recovery now completes up to 3 missing brackets with a string-aware scanner (never when cut inside a string), bounded in time and size (a deeply nested reply raised RecursionError in a test, now caught). **Not changed, still open:** the planner made 4 read-only subgoals (merge limit is 3 and requires no saved session), a replan still restarts at subgoal 1, and the per-task budget (10 minutes / 45 steps) is the only brake on a task that keeps failing validation. **Not verified:** whether the validator can judge a PDF shown in Drive's viewer (the text may not be in the page text; it sees one screenshot).
5h. Nami answered "I cannot read the contents of PDF files" and never opened a browser. Cause: the text-only intent step (fast model) sampled a wrong `unsupported` guess, and acting on it ended the task. I could not reproduce it on demand with the exact text (0/32 refusals after the prompt fix; the fast model's wrong reading found in the second-opinion probe was "my drive" = the user's LOCAL disk, 3/3). Fixes: the intent prompt says what "my drive/inbox/docs" mean and not to refuse over doubts about reading documents; a refusal on a request that names a website now gets a second opinion from the STRONG model (`NAMI_MODEL_STRONG`, default Sonnet 5.5; one call, only when a refusal is claimed), told what the browser can do; only an explicit "possible" overrides it, a failed check keeps the refusal; refusals are never cached. Measured live (3 runs each): 3 false refusals (PDF, spreadsheet, image in Drive) overridden, 4 real ones kept (phone call, 2 harmful, file only on a laptop). The earlier test gap is closed (the intent-level refusal path had no test). **Open:** 7 cases is a small sample, not a measured false-refusal rate; the intent cache can still return a similar earlier intent for a paraphrase, skipping the check.
5. A real task (anime timetable) ran 37+ steps over 10+ minutes: the only limits were per attempt (15 steps x 2 retries x 1 replan). Added a per-task budget (`NAMI_TASK_MAX_STEPS`=45, `NAMI_TASK_MAX_SECONDS`=600), a hard 180 s timeout per executor run, and an honest "I stopped, here is what I confirmed" result. The budget is in memory, so a restart gives a task a fresh budget, and an abandoned paused task leaves a tiny entry until restart.
6. The executor overclaimed (14 entries; the page had 6) and the validator caught it. Added a grounding rule to the executor prompt ("report only what you can see on the current page now"). Whether that reduces overclaiming is **not measured**; the validator remains the real control.
7. Still open from that run: a replan restarts at subgoal 1 (F4); repeated scroll/screenshot loops waste steps (bounded now, not prevented).
8. Measured honestly: asked about an `<h1>` that example.com does not have, the agent said so instead of inventing one.

### Defects found by the tests written this round (all fixed, each now has a regression test)
1. `repr(BrokerPayload)` printed every secret value (a traceback or debug log would have leaked passwords). Fixed with `repr=False`.
2. A pasted API key became the **thread title** in `data/chainlit.db` in plaintext (Chainlit names a thread from the first message). Fixed with a redacting data layer at the single write point; a test greps every database file.
3. `ScopedDatabase` as first written reused the first org's filter for later queries (SQLAlchemy caches the loader-criteria lambda and my default-argument capture was not tracked). Caught by test ordering; fixed with a closure variable and a regression test.
4. A planner failure sent the graph into the gate node with no plan (`KeyError: 'plan'`). Fixed with a conditional edge and a smoke scenario.
5. The drift harness's own fake site served UTF-8 without a charset, mangling `₹` and `⋯` (test-tool bug, fixed).

### Connect from the website, no browser pop-ups (2026-10-05) — tests written, NOT run (the user paused test runs)
1. **Memory wiring restored.** `app/agent/graph.py` imported `app/memory/commands.py`, which had been deleted (only its `.pyc` remained) while `intent.py`, `planner.py` and `cl_app.py` had been reverted: every task would have crashed with a `TypeError`. The module was rebuilt from the orphaned bytecode (byte-identical: every function's code and constants compared equal), `memory=` added back to `parse_intent`/`make_plan`, and the chat handles "remember / forget / what do you remember" again. `tests/test_memory_commands.py` (20 tests, run once and all passed, 4 mutation checks caught, before the pause).
2. **In-page sign-in (no window on the server).** `NAMI_TAKEOVER_VIEW=inpage` (default): takeover browsers start headless and are streamed into the chat card over `/chainlit/live/ws/{ticket}` (`app/browser/live_relay.py`, `live_router.py`, `LiveBrowserView.jsx`). A viewer needs a 256-bit ticket (task-bound, 30 min, never stored in the checkpoint or events) AND the same signed-in user's cookie; hand-back/cancel/close revoke it. Only frames out, input in. Headless Chrome is started with the normal Chrome user agent (no "HeadlessChrome"). `window` restores the old behaviour. **Not verified:** that Google accepts sign-in in headless Chrome with the UA override (other headless signals may still trip its checks); keyboard layouts beyond US; IME input; drag gestures; Kernel is unchanged (it has its own live view).
3. **Connect Google (OAuth) + Gmail/Drive via API.** "Connect Google" in the Connections sidebar (or the chat card) runs Google's consent page with fixed scopes (drive.readonly, gmail.readonly, gmail.send), PKCE, signed single-use state bound to the user; the refresh token is stored in the vault (kind `oauth`, bound to google.com/googleapis.com). Gmail/Drive tasks then use `app/connect/google_api.py` through a bounded read-only loop (`app/agent/google_runner.py`, 6 calls max); email/document text is wrapped as untrusted and passes the gateway. Sending is a separate irreversible subgoal: the approval card shows the exact draft and only that draft is sent. **Needs setup before it works:** in Google Cloud Console add the redirect URI `http://localhost:8000/chainlit/connect/google/callback` (and the production one), enable the Gmail API and Google Drive API, add the three scopes to the consent screen and add yourself as a test user. **Not verified:** any live call to Google; restricted-scope verification for production users; PDF text extraction on real files (pypdf; scanned PDFs have no text); Disconnect only deletes Nami's copy and does not yet call Google's revoke endpoint (also remove Nami at myaccount.google.com/permissions).
4. Sites that only offer "Sign in with Google" (Vercel, OpenRouter) and SMS/passkey sites still need one sign-in by the user, now inside the chat page instead of a window.

### Sign in through chat questions, no browser to watch (2026-10-05) — tests written, NOT run (the user paused test runs)
1. **Default flow.** Nothing saved for a site: Nami asks in chat (`need_credentials` card with the secure form preselecting what fits: API key for a read-only task on a site with an API, otherwise login + optional 2FA key), the user clicks "I've added it", and the same task continues with a sign-in step (the planner is forced to start with it). `takeover` is no longer produced by the access planner; "Sign in once instead" is an explicit button.
2. **Codes.** A site that texts or emails a code makes the agent call `request_help(kind="code")`; the run ends, the graph pauses, and Nami asks "What's the verification code…?". The reply is accepted only while that question is open, only if the whole message is a 4-8 character code, is scrubbed to "[code provided]", is held in memory (`Orchestrator._codes`, 300 s, single use) and typed through the `otp_code` placeholder on a **re-run of the same subgoal** (browser-use cannot be suspended mid-run). Max 2 asks per task.
3. **CAPTCHA / passkey / other.** A card offers "Open secure window"; no browser view is shown until the owner clicks (`Orchestrator.surface`).
4. **Non-standard TOTP (closes D4).** Generated by the vault, injected as `cred_*_2fa_code` (a name browser-use will not re-derive).
5. **Not verified:** any live site; that browser-use calls `request_help(kind="code")` at a code box instead of finishing or guessing (prompt-bound, not guaranteed); a short-lived code page may time out during the re-run; Vercel and OpenRouter may not offer an email login at all (the card says so and offers the one-time window); a server restart drops an in-memory code (the user is asked again); the code crosses the chat websocket once before it is scrubbed.
6. **Tests:** smoke scenarios D, F, U, V, W and Q were rewritten and X added; unit tests updated and added (see tests/test_agent_gates.py, test_need_credentials.py). None has been run.

### API generation with a saved key (2026-10-05) — tests written, NOT run (the user paused test runs)
1. **What it is.** `kind: generate` endpoints in `app/sites/registry.yaml` (OpenRouter: text, image, video, speech). The model picks an endpoint NAME and fills the declared params from the user's own words; `check_params` validates them (type, length, pattern, allowed values) and the POST body comes from the registry template. Host and path are fixed by the registry; no redirects; key only in the header; origin binding and the audit log apply.
2. **Approval first.** A generation is planned as an irreversible subgoal, so the approval card always appears ("uses credits on your account"); `_run_generate` and `ApiExecutor.generate(approved=...)` refuse without it, and a generate endpoint cannot run through the read-only `call()`. `Intent.generate` (from the user's words) routes such requests to the API when a key is saved, otherwise the chat asks for the key.
3. **Outputs.** Text goes through the injection gateway. Images (data URLs) are saved only if the bytes look like PNG/JPEG/WebP/GIF, audio only for an audio content type, under `data/generated/<user>/`; only those files are attached to the chat. Video comes back as an https link shown with its host and is never fetched. Video is an async job: polled with a validated job id until done/failed/timeout (10 min).
4. **Not verified:** every OpenRouter path, body field, default model id (`google/veo-3.1`, `google/gemini-2.5-flash-image`, `openai/gpt-4o-mini-tts`) and response shape is from public docs and has never been called; the real cost is unknown to Nami (the card says so, and the spend limit cannot check it); generated files are not cleaned up; rules have no separate "allow generation" switch (a blocked `purchase` does not block it).

### pi agent harness for open-ended reading (2026-10-05) — tests written, NOT run (the user paused test runs)
1. **What it is.** `app/harness/`: a router (`router.explore_eligible`, plain code over the access plan) sends a task to the pi harness (pinned `@earendil-works/pi-coding-agent@1.0.3` in `tools/pi`, run over its RPC mode) only when it is read-only on sites the user named: no login, credential, card, approval action, sensitive action, generation, or saved session for that site. Everything else takes the deterministic graph unchanged. `NAMI_EXPLORE=off` disables it; it is also off when pi, node or the model key is missing.
2. **Box around pi.** `--tools web_open,escalate` (no shell, no file tools), `--no-extensions -e pi_extension.ts`, no skills/templates/context files/session, `--offline`, throwaway HOME and working folder, an environment of only PATH + the model key + the run's tool URL, no vault and no saved logins. Its only reach to the web is a loopback tool server (`tool_server.py`) with a one-run token (task, allowed domains, page budget, deadline; revoked at the end): domain check before and after redirects, injection gateway and untrusted wrapping on every page, links filtered to the plan, a login wall is not read and escalates. An unexpected tool name aborts the run.
3. **Result handling.** The harness's answer is data: it goes to the existing verifier as a claim, next to the text of the pages actually read (kept, capped at 24k). No page read, an error, a timeout or an escalate means the deterministic path takes over (`explore_fallback`). Nothing is learned as a skill from an exploration.
4. **Measured live (manual, not the test suite):** one run read example.com and answered from it; a request to open another site and a file:// URL were refused by the tool server (both recorded as `off_plan_url`).
5. **Not verified:** accuracy on real research tasks versus the browser agent (no comparison run); cost per task (the pi model call is not in the per-task cost counters); that `--tools` really hides every built-in under every pi version (pinned to 1.0.3, but only the tool-name check at run time enforces it); pi is not in the Dockerfile (needs node and `npm ci --ignore-scripts` in `tools/pi`); a text-only read cannot see content drawn on a canvas or shown only in images; Haiku 4.5 is the default model for it.

### Audit trail and derived approval policy for API calls (2026-10-07) — tests written, NOT run (the user paused test runs)
1. **Request audit.** Every request made with a decrypted API key (the read call, the generation POST, each job poll) is written to the access log as action `request`: host, method, path, status (no query, no body, no key; the path is redacted). A request that got no response is logged as `-> no response`. A failing audit write never fails the call (the decrypt itself was already logged by `vault.use`). Code: `VaultService.log_request`, `Broker.audit_request`, `ApiExecutor._sender`.
2. **Derived approval policy.** `app/sites/policy.py`: risk comes from the HTTP method (GET/HEAD read; POST/PUT/PATCH write; DELETE destructive); the registry label can raise it, never lower it; unknown means a change. The graph and `ApiExecutor.call` use it, and `lint()` (tested) flags contradictory registry entries. A write we have no executor for is declined ("I can't do that through the API yet").
3. **Not verified / not done:** the access log is only readable through `GET /chainlit/vault/log` (no panel in the UI yet); the log is not shipped to the customer's own storage; POST/PUT/DELETE endpoints other than `generate` have no executor, so the policy only protects future ones; the standard-TOTP seed still sits in the agent process (see ACCESS_MANAGEMENT.md).

### Request audit and derived approval policy for API endpoints (2026-10-07) — tests written, NOT run (the user paused test runs)
1. **What it is.** Borrowed from Infisical Agent Vault (per-request audit) and Executor (approval derived from the protocol). Every request made with a decrypted API key now adds a `secret_access_log` row (`action=request`): host, method, path (query and fragment dropped, secret-shaped text redacted), and status, with no body and no key. A request that got no response is logged as `no response`. Logging is after the decrypt (already logged) and can never fail or block the call. `app/sites/policy.py` derives approval from the HTTP method: GET/HEAD read, POST/PUT/PATCH write, DELETE destructive; a registry label can raise risk, never lower it. The graph, `ApiExecutor.call` and a registry lint test all use it.
2. **Visible to the user** in the Connections panel's new **Activity** section (replaces "Recent use"): `GET /chainlit/vault/activity` turns log rows into sentences such as "Used “OpenRouter key” on openrouter.ai: POST /chat/completions (it worked)", explains 401/402/429, hides the decrypt row of an API key (its request row says more) unless it was refused, and can filter to one saved item via the "Activity" link on each row. `app/vault/activity.py` is pure. The raw `GET /log` stays. Not verified: the panel was never opened in a browser.
3. **Not done / limits.** Write or destructive endpoints that are not `kind: generate` are refused as "can't do that yet" (no safe executor exists for them). The audit is a row per request, not a per-session export to the user's own storage like Infisical's S3 logs. The log shows what was sent where, not what the response contained. The browser path is not covered: typed placeholders are logged as uses, page requests are not. Standard TOTP seeds still enter the agent process (docs corrected).
4. **Tests:** tests/test_site_policy.py and tests/test_activity.py (new), audit tests appended to tests/test_api_generate.py, activity route tests appended to tests/test_vault_router.py. None has been run.

### Offline items closed (2026-10-07) — everything below is written, NOT run (test pause); cloud items and the missing-test list in section A were left out on request
1. **Standard TOTP seed no longer enters the agent process.** The broker asks the vault for every code (`vault.totp_code`) and injects `cred_*_2fa_code`; no seed in the broker. Caveat: a code made at build time can go stale on a long run (min 10 s left).
2. **Disconnect Google revokes the grant** at Google (best effort, 5 s timeout, token never logged) and drops the saved google.com session. Not tried against real Google.
3. **Scrubber** keeps Luhn-valid digit runs inside URLs unless the parameter name looks like a card; cards in prose still redacted. The key-name heuristic is a judgment call.
4. **TOTP QR upload** in `SecureInput.jsx` through the browser's `BarcodeDetector` (no upload, no new dependency; Chromium only, otherwise "paste the key").
5. **`/chainlit/tasks`** list, ledger and cancel (owner only, redacted, CSRF on cancel). Resume over HTTP not built; approvals stay in chat.
6. **Skills export/import**: signed export, import lands as an unapproved candidate after signature, size and scan checks. Only bundles signed by this installation's key verify; other publishers' keys are not trusted.
7. **Parameterised workflow steps** (`app/skills/params.py`): the user's constraint words are saved as named slots and filled on reuse. Old skill rows still load.
8. **Eval**: `run_eval --replay TASK_ID` re-plans a stored task and diffs the plans (planning only, no browser steps). `--real` already existed.
9. **Generated files** older than `generated_ttl_hours` (72) are removed at start-up and hourly.
10. **Planner**: pre-send reversible steps merge into one; a replan after sign-in does not re-add it; a rejected saved login asks for an updated one instead of retrying; the executor gets a "Plan: step i of n" line; suggestions after a connect come from fixed wording and the registry.
11. **pi cost** counted into the task cost (token field names are a guess; flagged estimated when pi reports none).
12. **Dockerfile** installs node and pi's dependencies (never built). OWASP AST10 titles checked: unchanged (v1.0, still in public review).
13. **Still open from this round:** context compaction and tool masking (only plan recitation added); agent-side `ask_user`/`confirm_action` tools (pauses stay graph nodes); `ScopedDatabase` outside skills and RBAC in vault routes; model-based injection classifier; real calibration; anything needing a live site, model key or cloud (B1-B13, B15, 6.x); the Canva deck.
14. **Tests**: new files tests/test_replan_keeps_progress.py, test_health_cache.py, test_planner_pre_send_merge.py, test_harness_cost.py, test_totp_vault_side.py, test_google_revoke.py, test_scrub_urls.py, test_admin_hidden.py, test_vault_qr_ui.py, test_tasks_router.py, test_skills_io.py, test_awm_params.py, test_generated_cleanup.py, test_eval_replay.py; test_broker.py and test_admin_and_schema.py edited; smoke_graph.py sets `health_ttl_seconds=0`. A skills test with an exact-dict check on skill content may need the new `params` field.

### Remaining open items closed (2026-10-07, second round) — written, NOT run (test pause)
1. **Context management (2.5).** `app/agent/context.py`: older notes shrink to their first sentence, newest 3 kept whole, hard 1200-char cap, the user's last 2 answers kept as data; no model calls. Tool masking: sign-in steps lose extract/read_long_content/search_page/screenshot; public lookups lose the dropdown tools (`mask_tools` setting turns it off). Not verified: that browser-use behaves with the tools masked.
2. **Agent-side `ask_user` and `confirm_action` (2.1).** Both end the run like `request_help` and pause through the existing graph nodes; questions are redacted, scanned and refused if they ask for a credential; answers come back as data. `confirm_action` only requests approval, never grants it, and cannot skip a planner-tagged approval or unlock cards. Caps: `agent_ask_max`/`agent_confirm_max` = 2. The credential-word check is deliberately broad (a harmless "is the pin code right?" is refused). Not verified: whether the model uses the tools sensibly.
3. **Model-based injection classifier (L3, B12/3.5).** `app/security/classifier.py`, off by default (`classifier_enabled`). Runs after the regex gateway on the executor's page text in `n_run`; an error or timeout means "unknown" and the regex verdict stands; a classifier hit alone quarantines the text; "clean" never clears a regex hit. Recall and precision on real pages are **unmeasured** (only a two-case scripted test).
4. **Per-action capability policy (L4).** `app/security/capabilities.py`, called after every browser step from the executor: a sign-in step may type only placeholders; an irreversible step needs approval before clicks or typing; navigation must stay in the plan; file transfer, evaluate and unknown actions are denied. A denial that approval cannot fix fails the step ("stopped by policy"). Caveat: the check runs after the action (browser-use gives no pre-action hook), so it stops the NEXT step, it does not undo the one just taken. Action names were checked against browser-use 0.11 source, not a live run.
5. **Tenancy (3.1).** `channel_members` table + migration `a3c5d7e9b102`; `app/tenancy/access.py` enforces `read_thread`/`read_task` through the existing decision function (logged); viewers get 403 on `POST /credentials` (credentials stay personal); `ScopedDatabase` now used by the tasks router, long-term memory and rules resolve; the static allow-list test updated. Not done: vault credentials and the secret log have no `org_id`; `forget`, `set_enabled`, `delete_rule` still use raw sessions. Not verified: `alembic check` on the hand-written migration; the loader-criteria behaviour on scoped selects.
6. **Tests (not run):** tests/test_context_mgmt.py, test_agent_tools.py, test_classifier.py, test_tenancy_channel_members.py, test_tenancy_vault_rbac.py, test_tenancy_matrix.py, test_tenancy_static_guard.py.
7. **Left open on purpose:** real calibration (5.3) and every item needing a live site, model key, Docker or cloud (B1-B13, B15, 6.x) cannot be done from here; the Canva deck needs your design. Section A's missing-test items were excluded by request. There is no ticket tracker in this repo, so "closed" means marked here.

### Test run after the pause (2026-10-07)
The user lifted the pause. First full run: 30 failed, 947 passed. After fixes: **977 passed, 4 skipped, 0 failed** (fakeredis). Everything marked "written, NOT run" above has now run; "not verified" notes about live sites, models, Google, Kernel, Docker and cloud still stand.
Real bugs found and fixed:
1. A password-shaped question ("Please enter your password for example.org") was scrubbed by the injection filter first, so the secret check missed it and the question was shown. `_needs_user` now also checks the text before scrubbing.
2. `GET /chainlit/tasks/{id}` held a database session while the permission check opened another; with a one-connection pool it hung 15 s. Fixed in `app/tasks_router.py`.
3. `next_steps` cut off its closing line when the registry had three reads. Fixed in `app/agent/suggest.py`.
4. `starlette` was imported but not declared in `pyproject.toml` (added; `uv.lock` still needs `uv lock` with network).
Stale tests updated (the code was right): redaction of a 2FA seed (the seed no longer enters the broker), audit-row counts in the TOTP and API tests, a fake guard in the Google test, the OpenRouter endpoint set, merged pre-send steps in replay, the eval harness and `smoke_graph` (now 149 checks).
**Still open from this run:**
- A site whose login page changes layout makes Nami ask for a new password, because a failed sign-in step is treated like a rejected login. Fix: ask only when the failure looks like a rejection.
- `uv.lock` is stale for the root package.
- The real-Redis run was not repeated.
- Mutation checks were not repeated for this round's fixes.
- Still excluded, by nature: real calibration (5.3), B1-B11, B13, B15, 6.x, the Canva deck, and section A's missing-test items.

### Live tests with the real model (2026-10-07)
Real Haiku 4.5 and real browser-use through the orchestrator, throwaway scripts under `/tmp/nami_live/`, a temporary database and throwaway users (the user's vault and accounts were not touched). The login cases used a local copy of the fake shop served over https as `foodco.com`; the API cases used a local mock server, so no real key was used. Each case ran 1-4 times, so none of this is a success rate.
**Public sites.** example.com: verified in 32 s, 2 steps, no policy denials, but the agent called the heading "example.com" (it is "Example Domain") and the validator passed it (the pi path got it right). Wikipedia (birth year plus next section; click through to Bletchley Park and scroll): both correct, 36 s/2 steps and 51 s/4 steps, no false denials by the capability policy. pi explore: verified in 8 s with the correct heading.
**Injection classifier (L3), 12 snippets.** Regex caught 3 of 6 injections, the model 2 more (5 of 6); the sixth was an HTML comment the filter strips before the model sees it. One benign snippet (a tutorial quoting "ignore previous instructions") was flagged, by the regex layer. Median extra latency 0.87 s, max 1.47 s.
**Logins and typing.** Saved-password login: verified in 98-137 s, placeholders typed, the password was absent from model inputs, events, logs, database and checkpoint files. A reversible form step (address) worked. A wrong saved password: one attempt, then a request for an updated login.
**Approvals.** Before every approval card, orders placed and place requests were both 0. Declining placed nothing. Approving completed in 2 of 4 runs; the other 2 failed after 10+ minutes with no order, because the planner asked for details the page never shows. The card number never appeared in prompts, events or logs.
**TOTP.** Standard (6 digits, 30 s) and non-standard (8 digits, 60 s) both logged in on the first code; the vault logged each generation; the seed and codes were absent from model inputs, events, logs and files; the name `bu_2fa_code` appeared 0 times.
**API keys (mock server).** The read call returned 200 with the key only in the `Authorization` header; the audit row has no key; generation made 0 requests before approval and 1 after; the Activity text read correctly.
**Off-plan navigation: partial.** A passive injection was ignored. A lure telling the agent the data had moved to another site made it try that site: the `navigate` action was blocked before any request, and the policy stopped the run, but the agent then clicked the link and the other site's server saw one request before browser-use reset the tab. The check runs after the action, so a click can cost one GET to a third party (origin-only Referer). Stopping it needs network-level blocking, which is not built.
**Defects found and fixed (each with a test):**
1. Sign-in could never be confirmed: the validator got no page text for sign-in steps, so every saved password looked rejected. It now gets redacted page text, still no screenshot (`observe_flags`; `tests/test_signin_validation.py`).
2. `send_keys("Enter")` in a sign-in step was refused as typed text (`keys_type_text` in `app/security/capabilities.py`).
3. The policy checked only the last action of a step; a typed literal followed by a click got through. It now checks every new action (a mutation check failed two tests).
4. A wrong saved password was submitted 2-3 times. Now at most 2 password typings per run, plus a prompt hint to call `request_help`; the live run then made 1 request.
5. Chrome kept typed form values (username, card number, a one-time code) in plain text in `Default/Web Data` after closing; the password was never saved. `harden_profile` in `app/browser/local_provider.py` turns autofill off and deletes old form history. Local provider only, not Kernel.
**Still open from these runs:**
- A failed sign-in step of any cause (a site being down, for instance) still asks for a new login.
- The planner invents success criteria the page cannot meet (order dates, a delivery address at checkout), which made several runs slow or failing.
- The validator can pass a wrong answer (the example.com heading).
- The browser agent's own token cost is not recorded.
- The generate approval card had an empty summary on the mock endpoint.
- Not tested live: real OpenRouter, Kernel, SMS codes, CAPTCHA, the classifier on live pages, `request_help` at a code box, tool masking on a real sign-in step, whether the 2-typing hint holds across model runs.
- `uv.lock` is still stale for the root package; the real-Redis run and mutation checks for these fixes were not repeated.
