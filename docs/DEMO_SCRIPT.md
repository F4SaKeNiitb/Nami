# 2-minute demo script

Goal: show chat-first UX, credentials that never touch the chat, honest handling of "needs the human", and the approval gate. Each beat is something the app does today; beats marked ★ need real keys (they can't be shown from the offline build).

| t | You do | The viewer sees | Why it matters |
|---|---|---|---|
| 0:00 | Open the app, sign in (Google) | One chat, three starters, a Connections sidebar | No new interfaces |
| 0:10 | Paste a fake key into the chat: `sk-or-v1-…` | "That looks like a secret, so I didn't send it anywhere", the message in history shows `[REDACTED:api_key]` | Chat can't leak a key |
| 0:25 | **Add a login or API key** → save an OpenRouter key | Secure form closes, Connections lists it (label, site, last used) | Secrets travel one way only |
| 0:40 ★ | "Check my OpenRouter credits" | Plain-language steps, then a Result card with the balance and "Verified on the page" | API route, key never in the transcript |
| 0:55 ★ | "Generate a video with OpenRouter" | "I can't do that through the OpenRouter API. I can: credits, models, key" | Honest about limits |
| 1:10 ★ | "Repeat my last Swiggy order" | Chat asks for a login → secure form → "I've added it" → Nami signs in itself → "What's the code Swiggy sent you?" → type it | No browser to watch; code asked in chat |
| 1:30 ★ | (continuing) | Approval card: items, total, pay-with, address; **Approve** | Gate before every purchase |
| 1:45 | Open Connections → **Revoke** the key; open the access log | Entries for each use, allowed and denied | Revocation and audit |
| 1:55 | Say what's not done | One sentence from `docs/LIMITS.md` | Judgment |

Offline stand-in for the ★ beats: `python -m app.eval.run_eval --agents adaptive --modes none expiry inject --deny` shows the same safety properties against a stand-in shop (approvals, no leaks, session expiry recovery, injected page text ignored).
