# Access management in Nami: choices, and where they stop

*One page. Everything here is built unless it says otherwise; "not verified" means written and tested against fakes, never run against the real service.*

## The rule everything follows
A secret may only travel from the user's browser to the vault. It never goes through the chat, the model, a log, a trace, a cache or an event. Each of those is enforced in code and covered by a test that tries to break it.

## What I do for each kind of access

| Access | How it is collected | How it is stored | How it is used |
|---|---|---|---|
| **Password** | Secure form → `POST /chainlit/vault/credentials` (cookie auth + CSRF header + Origin check). Not the chat socket. | AES-256-GCM. A per-user key, wrapped by a KEK (Azure Key Vault in production, a local key in dev). The authenticated data names the user, the credential and the allowed sites, so a ciphertext cannot be moved to another row. | Typed by the browser layer via placeholders (`<secret>cred_xxxx_password</secret>`). The model sees the placeholder name, never the value. Only on the website it was saved for. |
| **API key** | Same secure form, bound to the API host. | Same. | Used **server-side** for a fixed list of endpoints (`app/browser/api_executor.py`): no redirects followed, key sent only the way the site takes it (bearer, a named header, basic auth or a URL parameter; declared in the registry, or worked out once by a model that only picks among those shapes, after you approve, on one read-only request, then remembered on the credential), response redacted and screened. The model picks an endpoint *name*, never a URL. Reads (GET) run freely; anything else waits for approval (see choice 4). Every request is logged. |
| **Card** | Same form, with a "demo vault, not PCI" notice. | Same; only brand/last4/expiry shown. | Placeholders exist only for the one irreversible step the user just approved. Never for earlier steps. |
| **2FA seed (TOTP)** | Same form; the form can preview the current code so the user can compare with their app. | Same, linked to its password. | The vault computes the code (one logged `totp` use) and only the code is injected as a `*_2fa_code` placeholder, for standard and non-standard seeds alike. The seed never reaches the agent process. |
| **SMS / email one-time code** | The agent signs in with the saved password, then asks in chat: "What's the code X just sent you?" The reply is scrubbed from the thread. | The code is held in memory for one run (5 min, single use). It is never in the database, an event, a log or the graph state. | The code is typed through a placeholder; the model never sees it. At most 2 asks per task. |
| **Authenticator (2FA) key** | Optional field on the same login form. | Encrypted seed. | Every seed (standard or not): the vault computes the code and only the code is injected (never a `*_bu_2fa_code` seed placeholder), so the seed never sits in the agent process. A code can go stale on a very long run; a retry builds a fresh one. A QR image can be uploaded: it is read in the browser (BarcodeDetector) and never uploaded. |
| **CAPTCHA / passkey** | Nami says it is blocked and *offers* "Open secure window". Nothing opens until the user clicks. | Only the resulting session, encrypted per site. | One-time window (the in-page view), handed back with one click. |
| **Google (Gmail / Drive)** | "Connect Google" link (OAuth). Sites that only offer "Sign in with Google": add an email login if the site has one, otherwise the one-time secure window. | Refresh token in the vault, or the session. | Gmail/Drive use Google's API; no browser. |
| **Revocation** | One click in Connections. | Revoke wipes the ciphertext. "Sign out and forget" deletes the session snapshot and clears that site's cookies in the browser. Account deletion destroys the user's key (crypto-shred). | Every use, allowed or refused, is in an append-only log the user can read. For API keys the log also records each request the key made: host, method, path and status (no query, no body, no key). |

## The choices that matter most
1. **Placeholders, not values.** The model plans with names; values are substituted below it. A leak would need the model to be given a value in the first place, and the tests check it never is (prompts, events, DB rows, logs, final results, traces).
2. **Origin binding in code.** A credential can only be used on the registrable domains the user approved (`swiggy.com`, not `swiggy.com.evil.io`). Checked on every decrypt, and bound into the encryption itself.
3. **The access plan is written before any page is read, from the user's words only** (plain code, not a model). A web page can't add a site or a credential to the task.
4. **Approval is a gate, not a prompt.** The planner marks irreversible steps; the guard stops before them; a URL/text backstop catches drift into checkout. Approval of step 5 does not approve step 6. For API endpoints the need for approval is derived from the HTTP method (`app/sites/policy.py`): GET/HEAD run freely, POST/PUT/PATCH need approval, DELETE counts as destructive, and a registry label can raise that risk but never lower it. A new endpoint is therefore safe by default, and a test lints the registry for contradictions. A spend limit reads the total from the page and stops over-limit orders before asking.
5. **Short-lived identity between parts.** Each decrypt needs a single-use token (≤120 s, one target, one scope, one task). A viewer or a non-owner never gets one. If the replay store is down, decrypts are refused.
6. **Ask in chat first; takeover is the explicit fallback, never the default.** Nothing saved means a follow-up question with a secure form, then Nami signs in by itself in a background browser. A browser is shown to the user only for CAPTCHA/passkey/Google-only sign-in, and only after they click for it.
7. **Learned workflows are untrusted until proven otherwise.** After a verified task, Nami may save the steps as a "skill". It is scanned (hidden characters, secrets, placeholders, off-site links, instructions to skip approval), judged, hash-pinned and signed. Anything with code needs the user to approve the exact hash. A skill can never widen what a task may touch.

## Where it stops (read this before trusting it)
- **No real website task has been run end to end.** The orchestration, vault, approvals and recovery are tested with a scripted model and a stand-in site (`docs/EVAL_RESULTS.json`); real success rates are unmeasured.
- **The executor model sees raw page text.** The injection gateway screens what reaches *my* validator and verifier, and API responses. It cannot screen what the browser agent library feeds its own model. That path is bounded by the access plan, allowed domains, placeholders and the approval gate, not sanitised.
- **Storing a password and its 2FA seed together makes them one factor.** The UI says so. For high-value accounts, sign in yourself.
- **Cards are demo-grade.** Not PCI. Production should use a virtual or one-time card service.
- **Not verified against the real services:** Google sign-in, Kernel hosted browsers and live view, Azure Key Vault wrapping, the OpenRouter endpoint paths, Redis-backed replay protection, the container deployment.
- **A crypto-shred is complete only after backups expire** (72 h retention): an older backup still holds the wrapped key.
- **Single replica.** SQLite and live browser sessions in memory; a restart ends the live session (the task is resumable).

## One credential per site

Only the latest login, API key and 2FA key is kept for a site. Saving a new one revokes the older active ones for that site (ciphertext
deleted, a 2FA key linked to a revoked login goes with it, each logged as `revoke` by `system` with the reason), and for a login also
clears the site's saved browser session so the browser can't stay signed in as the old account. Cards are exempt (a site can have several)
and so is a credential that also covers other sites (its allowlist is sealed into it and can't be narrowed). The planner offers only the
newest of each kind even if older duplicates exist, so the agent is never asked to choose between logins.
