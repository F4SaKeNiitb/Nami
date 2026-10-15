# Nami — The harness, and how it protects you

*A chat assistant that does things on websites for you, keeps your keys out of reach of the model, and tells you honestly what it did.* Technical reference: [`ARCHITECTURE.md`](ARCHITECTURE.md).

**Where things stand (2026-10-04):** the storage layer — firm and user rules, conversation memory, long-term memory, caching — is built and tested (30 tests on a fake Redis, 33 against a real Redis 8). The chat UI, browser agent, vault, security plane and skills system are specified and scheduled; each section below says *built* or *planned*.

---

## 1. The idea

You type what you want: "repeat my last Swiggy order", "log into Vercel with my Google account". Nami works out what it needs, asks only for what's missing, and does the work in a cloud browser you never have to watch. When it needs access, it asks through a secure card, never the chat. It remembers that access for next time, and you can revoke it with one click. Before it says "done", it checks the result; if it can't confirm, it says so.

Instinct set the bar for how this should feel: one thread, no new interface to learn. Its reviewers' complaints were that it hides what it's doing and acts without asking. So I kept the chat-first feel and added a live view of the browser, a step log, and approval cards for anything irreversible.

## 2. How a request flows

```
message → secret filter → intent → access plan → plan → [execute ⇄ validate]* → verify → remember
                              │                             │
                        connect cards                 ask · take over · approve
```

1. **Secret filter.** Anything that looks like a key, card number or 2FA seed never reaches the model. It's moved into the vault, the message is redacted, and you're pointed to the secure input.
2. **Intent.** A cheap model turns your sentence into a goal, a site and constraints. At most one clarifying question, only when the answer changes the outcome.
3. **Access plan.** Nami decides which sites, credentials and action types the task needs **from your words alone**, before any page is read (Google's CaMeL idea). A malicious page can't later unlock a credential or add a site; that list is fixed.
4. **Plan.** Claude splits the task into subgoals, each with a success test and a flag for "irreversible".
5. **Execute ⇄ validate.** A browser agent works on one subgoal; a separate check looks at the fresh page and decides whether the test really passed. It doesn't take the executor's word.
6. **Verify.** A final check against the plan's key points yields an evidence card: order ID, screenshot, confidence.
7. **Remember.** What worked is saved as a *learned workflow* for that site — treated as untrusted input (§7).

**Why this shape.** Separating planning, execution and validation is what the research says works on real sites (Skyvern went 45% → 86% largely by adding a validator; independent benchmarks put real-world agents near 60%). So verification is something you see, not an internal detail. The tradeoff: splitting work across agents can lose context, so exactly one executor touches the browser and takes write actions; the validator gets the full plan and trace.

## 3. Credentials — *built*

**Rule zero: secrets never enter the chat or the model.** Enforced by code structure, not prompts.

| You need to give… | How Nami asks | What happens to it |
|---|---|---|
| An API key | Secure input card; posts straight to the vault API | Encrypted; only the last 4 characters are ever shown |
| A website password | Same card, optional "I use an authenticator app" step | Encrypted. The model sees a placeholder like `<<cred_7f3a_password>>`; the real value is typed into the page after the model call |
| SMS / email code | Nami signs in with your saved password, then asks for the code **in chat** | Held in memory for one run, never stored |
| Google / passkey / CAPTCHA | Gmail and Drive: **Connect Google**. Otherwise Nami offers a one-time secure window; nothing opens until you click | Nothing stored except the logged-in session |
| A 2FA authenticator | Paste the setup key or scan the QR (decoded in your browser); Nami shows the current code so you can check it | Encrypted seed; a fresh code is generated at the moment of login |
| A card | Same card, labelled "demo vault, not PCI-certified" | Encrypted; an itemised approval every time it's used |

**How it's stored.** Envelope encryption: each user gets a data key; Azure Key Vault wraps it; each secret is AES-256-GCM, bound to `user | credential | kind | origin` so ciphertext can't be moved between rows. Deleting a user's wrapped key makes every one of their secrets unreadable.

**Where a secret can be used.** Each credential lists its sites; the placeholder is filled only if both the page and the exact frame belong to an allowed site. Lookalike domains and cross-origin iframes are rejected.

**Revoking.** One click deletes the ciphertext, clears that site's cookies, deletes the saved session, and writes an audit entry you can see.

**Deliberate tradeoffs.** I run my own vault instead of Kernel's managed auth, because the vault is the heart of this assignment. Storing TOTP seeds beside passwords turns 2FA into one factor at the vault layer — I say so in the UI. Google sign-in can't be automated by Google's design: Gmail and Drive use Connect Google, anything else needs a one-time secure window you ask for. SMS/email codes are asked for in chat; push approvals, passkeys and CAPTCHAs still need you, and Nami says so plainly and resumes the same step.

## 4. Sessions — *built and tested locally; Kernel profiles not verified*

Each user has a browser profile on Kernel that persists between runs, plus an encrypted per-site snapshot of cookies so one site can be revoked without touching others and so the browser vendor can be swapped. Before each task Nami loads a logged-in-only page; if it lands on a login page it re-authenticates (stored password + TOTP, otherwise it asks you in chat) and resumes the same subgoal. Browsers run headless because that's ~8× cheaper; visible mode only when you'll need to take over.

## 5. Rules and memory — *built*

**Rules come from the firm and from you.** A firm admin sets hard rules ("never spend more than ₹2,000", "never visit gambling sites") and soft defaults ("deliver to the office"). You can tighten a hard rule but not loosen it: a ₹5,000 personal limit under a ₹2,000 firm limit means ₹2,000. You can override soft defaults and add site-specific preferences ("on Swiggy, deliver Home"). Only *policies* are enforced in code; preferences and instructions are guidance in the prompt, and I say so. Approval for purchases, sends and deletes is a baseline nobody can switch off. Every rule change bumps a version that is part of every cache key, so a stale rule is never served.

**Memory has two horizons.**
- *Short-term* is the current chat: the last 20 turns verbatim plus a running summary of everything older. A channel (a Slack channel, say) also gets "what's been happening here" across its threads.
- *Long-term* is facts and preferences that outlive a chat, scoped to you, your firm or a channel — never crossing firms. Ranked by relevance, recency and importance. It refuses to store anything that looks like a secret. You can delete your own memories; admins can't read them.

**Caching is where I changed my mind after measuring.** I planned a semantic cache that reuses answers for requests that *mean* the same thing. With the embedding model I use, "**show** my last Swiggy order" scores 0.927 against "**repeat** my last Swiggy order", while a genuine paraphrase scores 0.930. So the semantic cache now covers only *intent parsing*, at a strict 0.95 threshold; plans are cached only by exact structured intent; anything that reads a live page is never cached; and a test re-measures the threshold on the real model so a model swap can't quietly reintroduce false hits.

**Where it lives.** SQLite is the source of truth; Redis 8 is a disposable accelerator. Flushing Redis makes Nami slower, not wrong — which is why Redis can run as a cheap sidecar with no persistence.

## 6. Reliability — *mostly built; context management is not*

- **Durable state.** The orchestrator is a LangGraph graph with a checkpoint after every step. Asking you something, handing you the browser and waiting for approval are all interrupts; your answer resumes exactly where it stopped, even after a server restart.
- **Retries.** Two retries per subgoal with a note on what failed, then one re-plan, then a plain-language failure card with the last screenshot and three buttons: Retry, Take over, Try another way.
- **Context discipline** (from recent agent harnesses — DeepSeek V3.2, Manus, the Claude Agent SDK): keep reasoning across tool calls within a subgoal; replace old page snapshots with one-line stubs; freeze the prompt prefix so caching works; keep errors visible until the step passes.
- **Guardrails are code hooks.** A pre-action hook enforces approvals, allowed domains and placeholder filling; a post-action hook strips secrets before logging. Fixed rules run first; an LLM classifier only judges the ambiguous remainder.
- **Proof, not promises.** A drift harness — fake sites that rename selectors, move buttons, pop modals, expire sessions mid-task and plant injection text — measures task success, secret leaks (must be 0), approval-gate adherence (must be 100%) and tokens per success. ~30 hand-labelled runs calibrate how far the automated judge can be trusted.

## 7. Security posture and the OWASP lists

Two OWASP lists apply. Status column: **Built** = tested code exists today; **Planned (P#)** = specified in `ARCHITECTURE.md` §15 phase #.

### 7.1 OWASP Top 10 for Agentic Applications (ASI01–ASI10, Dec 2025)

Mapped against the item titles; I did not pull OWASP's per-item mitigation text from the PDF.

| # | Risk | How it could hit Nami | What protects you | Status |
|---|---|---|---|---|
| ASI01 | Agent goal hijack | A web page tells the agent to do something else | Access plan fixed from your words before any page is read; untrusted-content labelling and sanitising; injection classifier; egress limited to plan domains; suspicious text quarantined and shown to you | Planned (P2.2, P3.4–3.5) |
| ASI02 | Tool misuse & exploitation | Executor types a secret into the wrong form, or clicks "pay" unasked | Placeholder fill only on the credential's own origin and frame; four-layer guard with mandatory approval for purchase/send/delete; firm spend and domain policies enforced in code | Rules **built**; guard/broker Planned (P2.3) |
| ASI03 | Identity & privilege abuse | One over-broad agent identity | Each agent role per task gets a SPIFFE ID and a ≤120 s audience-bound token minted *after* the permission check; effective permission = yours ∩ agent role ∩ task scope; roles down to channel and chat; admins can't read private chats or personal memory | Memory privacy **built**; identity/RBAC Planned (P3.1, 3.3) |
| ASI04 | Agentic supply chain | Compromised dependency, model weights, or a learned skill | Hash-locked dependencies, vulnerability scan in CI, pinned image and weights; learned skills signed, hash-pinned, scanned and inventoried (see 7.2) | **Partly** (see AST02) |
| ASI05 | Unexpected code execution | A saved browser script runs something it shouldn't | Only snippets a human approved by exact hash ever run, and only inside the remote browser: no shell, no Node, timeouts, size caps, no network outside the plan's sites; metadata parsed with safe parsers and strict schemas | **Built as a policy layer** (see AST06) |
| ASI06 | Memory & context poisoning | A page plants a "fact" or a workflow step | Memory refuses secret-shaped content and is scoped per user/firm/channel; page-derived content is labelled untrusted; learned workflows are guidance only and scanned; caches partitioned per user and keyed by rules version | Memory scopes/secret refusal/cache partitions **built**; labelling and skill scanning Planned (P3.4, P4.2) |
| ASI07 | Insecure inter-agent communication | Forged hand-offs between planner, executor, validator | Single process; tokens bound to one audience and short-lived; the append-only event log is the only channel between steps; validator receives the full plan and trace | Planned (P2.1, P3.3) |
| ASI08 | Cascading failures | A stuck agent loops, spends or escalates | Step caps per subgoal, bounded retries then a human; stall detection; approvals before anything irreversible; fail-closed vault and broker when the replay store is down; ledger lets you resume instead of restarting | Planned (P2, P5.1) |
| ASI09 | Human-agent trust exploitation | Approval cards that hide what's really happening | Approval cards show the actual items, amount, address and card; takeover shows a "you're in control, I'm not watching" banner and really does stop screenshots; results carry evidence and an honest "unverified" when confidence is low | Planned (P2.3, 2.6) |
| ASI10 | Rogue agents | An agent keeps acting outside its task | Per-task identity that dies with the task; Stop and Pause always visible; budgets (steps, minutes, spend) in rules; every action attributable in the audit log and traces | Rules budgets **built**; identity/audit Planned (P3) |

### 7.2 OWASP Agentic Skills Top 10 (AST01–AST10, v1.0 2026 edition, in public review; titles re-checked against owasp.github.io/www-project-agentic-skills-top-10 on 2026-10-07)

Nami's "skills" are the workflows it learns from successful runs and the occasional saved browser script. Every one was shaped by web pages the agent read, so I treat them as input that may have been tampered with.

| # | Risk | In Nami | What protects you | Status |
|---|---|---|---|---|
| AST01 | Malicious skills | A poisoned page becomes a workflow step ("paste the password into support chat") | Skills are scanned on creation; steps touching credentials, outside domains or "first install this" never auto-approve; scripts need a human to approve the exact hash; workflows are guidance, never executed | **Built** (scanner, lifecycle, hash-approved snippets; corpus is synthetic) |
| AST02 | Supply chain compromise | Our dependencies, and our own skill store acting as a registry | Locked and audited dependencies, pinned image and weights; skills signed with a Key Vault key, with an append-only log of every create/approve/load/revoke | **Partly**: locked hashes, digest-pinned image, `pip-audit` in CI (CI not yet run), skills signed with a *local* key (Key Vault signing not built), `skill_events` log built |
| AST03 | Over-privileged skills | A Swiggy workflow steering a Vercel action | A skill can only *narrow* scope: its manifest is intersected with the task's plan, your permissions and the agent token; skills get no credential handles | **Partly**: manifest ∩ access plan and placeholder stripping built; user-rights and token-scope intersection not |
| AST04 | Insecure metadata | Malformed or smuggled skill definitions | Strict schema validation before use; safe YAML; hidden-character detection; declared sites must match the sites actually observed | **Built** (strict schema, size caps, manifest-vs-site check) |
| AST05 | Untrusted external instructions | Pages *are* the external instructions; skills that say "read the docs at…" | The whole gateway (§6); skills may not contain fetchable references; site hints are inlined and reviewed | **Partly**: gateway covers my validator/verifier and API responses, not the executor's own page input |
| AST06 | Weak isolation | Where skill code runs | Scripts run only inside the remote browser sandbox; the CDP socket is authenticated and never exposed; Redis on localhost; no hot-reload in production | **Built as a policy layer** (isolated JS world, timeout, size cap, off-plan requests aborted); not a hardened VM |
| AST07 | Update drift | The agent rewrites skills after each success | Every rewrite is a new version that must pass scanning again; production can be frozen; nightly re-verification; inventory shows version and hash | **Built** (new version per rewrite, freeze flag, nightly re-verify/re-scan, drift demotion) |
| AST08 | Poor scanning | Prose + code defeats regex | Two-layer scanner: deterministic rules on both layers plus an LLM reviewer that may lower trust but never raise it (it can be lied to as well) | **Built** (two-layer scanner, judge can only lower trust; accuracy measured on a synthetic corpus only) |
| AST09 | No governance | Nobody can see what the agent learned | "Learned workflows" panel: provenance, last used, scan status, Revoke; sharing to a firm needs an admin; every load is logged and linked to traces | **Built** (Learned workflows panel, approvals by role, event log) |
| AST10 | Cross-platform reuse | Exporting/importing skills to other agent platforms | Security metadata (risk tier, permissions, signature) travels with the skill; imports are untrusted and re-scanned; a skill that lost its manifest is rejected | **Built** (`app/skills/exchange.py`: signed export; import verifies the signature, requires the manifest, re-validates and re-scans, and always lands as a `candidate` the user approves). Only bundles signed by this installation's key verify; trusting other publishers' keys is not built |

**What this doesn't solve.** A learned workflow can still nudge the agent toward a wrong choice *inside* its allowed scope (the wrong restaurant, not the wrong card). Scope limits, approvals and final verification keep that mistake small and visible. Scanners, including the LLM reviewer, will miss novel attacks; the deterministic scope and egress rules are the real boundary.

## 8. Observability — *built (OpenTelemetry with redaction before export); LangSmith/Azure Monitor ingest not verified*

OpenTelemetry spans through LangGraph into LangSmith and Azure Monitor answer "which agent did what, for whom, and why was it allowed". Because a third party receives traces, secrets and page content are scrubbed before export; the complete audit log stays in Nami's own database.

## 9. Stack, and why

| Choice | Why | What I gave up |
|---|---|---|
| Python end to end | best browser-agent library; one runtime | React-level UI freedom |
| Chainlit on FastAPI | streaming chat, Google OAuth, custom cards | styling limits; secure inputs bypass the chat socket |
| browser-use as executor | indexed DOM + screenshot; native secret placeholders and TOTP | Browser Use itself now suggests a coding-harness outer loop; revisit after demo |
| Kernel browsers | persistent profiles, embeddable live view, stealth; free tier | vendor dependency behind a small interface |
| Claude Sonnet 5.5 + OpenRouter free models | Claude sees pages; free models only see your text | free models may log prompts, so never page content or vault data |
| LangGraph | checkpoints, interrupts and re-planning are exactly pause/resume | a dependency |
| SQLite (WAL) + Litestream → Blob | one file, ~$0, single-container deploy | exactly one replica; Postgres/RLS is the scale path |
| Redis 8 sidecar, cache only | exact cache, hot memory, semantic cache on core Vector Sets | Azure's Basic/Standard Redis lacks vector support; nothing in Redis may be a source of truth |
| Own vault + Azure Key Vault | the vault is the assignment; origin binding, audit, crypto-shred | faster wiring with a vendor's managed auth |
| Azure Container Apps, 1 replica | websockets; ~$12–15/mo | horizontal scale |

## 10. Honest limits

- Google, passkeys, SMS/push and CAPTCHAs need you once per session; Nami persists the session afterwards.
- Live sites change; success is a probability, shown as confidence.
- TOTP seeds beside passwords = one factor at the vault layer; disclosed.
- Card handling is demo-grade; production should use one-time virtual cards.
- Revocation is complete only after the 72-hour backup retention window.
- Semantic cache recall is deliberately low; prompt injection is bounded, not solved.
- Only policies are enforced in code; preferences and instructions are guidance.
- The security plane and the skills system are built and tested offline; the parts that need real services (Google, Kernel, Azure, a live model) are listed in `docs/LIMITS.md`.

## 11. What's next

Phases 1–5 are built to the extent they can be without live keys; `docs/LIMITS.md` lists what remains. The drift harness numbers are in `docs/EVAL_RESULTS.json` (scripted model: they measure safety properties around the model, not real-world success). Beyond the assignment: fine-tune a small executor on judge-labelled runs from the drift harness so known workflows run cheaply; move the outer loop to the Claude Agent SDK with the browser as a tool; expose Nami as an MCP server so Slack-native agents like Luffy can use it.
