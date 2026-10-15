# Nami in Slack

The same agent as the web chat, in Slack direct messages. Code: `app/slack/` (`bot.py` logic, `blocks.py` message formats,
`links.py` account linking, `router.py` the one-time web pages (add a login, secure window), `app.py` Socket Mode wiring).

## Set up (about 5 minutes)

1. Slack → **Your Apps → Create New App → From a manifest** → paste `infra/slack-app-manifest.yaml`.
2. **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-…`).
3. **Basic Information → App-Level Tokens → Generate** with scope `connections:write` (`xapp-…`).
4. Put both in `.env`: `NAMI_SLACK_BOT_TOKEN`, `NAMI_SLACK_APP_TOKEN`. Restart the server: the log says `Slack bot connected`.
5. In Slack, open **Nami → Messages** and say hi. Nami reads the email on your Slack profile and, if a Nami user has that email
   (sign in to the web app once with Google first), you are linked straight away. No second sign-in. (The manifest asks for
   `users:read` and `users:read.email`; after adding them to an existing app, reinstall it to the workspace.)

Socket Mode means Slack never calls us, so it works on a laptop and behind any deployment without a public Slack endpoint.
Set `NAMI_PUBLIC_URL` to the deployed address so the link and the "Open Nami" buttons point at the right place.

## What it does

| In Slack | How |
|---|---|
| Ask for a task | Type it. A single message shows what Nami is doing and updates in place. |
| Approve a purchase / send / generate | Card with the amount and a screenshot of the page; **Approve** / **Cancel**. |
| A verification code (SMS, email) | **Enter code** opens a modal. The value never becomes a message. |
| Add a password or API key | Not in Slack: the card has a one-time **Add it securely** link to a small page that asks only for what that site's real sign-in page asks for, then **I've added it**. |
| Result | Summary, facts, "couldn't verify" notes, generated files attached. |
| Stop / disconnect | say `cancel` / `unlink`. |

## Choices and limits (worth saying in the note)

- **Secrets stay out of Slack.** Slack keeps every message, so credentials are never collected there. A pasted secret is refused,
  not forwarded or remembered, and the user is told to delete it and rotate it. Codes use a modal, not a message. Notification
  previews and result cards pass the same redaction as the web chat.
- **Identity is the Slack profile email.** The first DM from an unknown Slack account is matched, case-insensitively, to the Nami user with
  that email (Slack's `users.info`). There is no second confirmation step. An email with no Nami user is never linked and never creates
  a user; the person is told to sign in to the web app first. Trade-off: a Slack workspace admin can set any email on an account, so this
  trusts the workspace's admins. Use it only in a workspace you control. `unlink` removes the link (the next DM links again by email).
- **Nothing about a site is hardcoded: Nami looks.** When a login is needed, a short anonymous browser session opens the site's own
  sign-in page, presses its "Log in" if the form is behind one, and reads only the *type and label* of the visible fields (never a value;
  it types and submits nothing). Plain code classifies them (phone / email / username, password, a one-time-code box, Google offered), not a
  model reading page text, so a hostile page can't talk Nami into asking for more. Results are cached for hours. Sites that block automated
  browsers or hide the form behind another step (seen live: Swiggy's bot check, Vercel's "continue with email") come back as "couldn't look",
  and the form then says so and asks the usual fields with the password optional. Code: `app/browser/login_probe.py`.
- **The add-it page can't be steered.** Its link holds a token bound to your Nami user, the site and the kinds; the site is never taken
  from the page. It must be opened signed in as that user, saves only to that site's allowlist, accepts only the fields listed for the kind,
  and the token works for one successful save (30 minutes).
- **Every button re-checks the owner.** A click carries a task id; it is honoured only if that Slack account is linked to the task's owner
  and the card is the one currently waiting. Stale or repeated clicks do nothing. The orchestrator's own owner check applies as well.
- **Direct messages only.** Results can hold orders, balances, mail. In a channel an @mention gets a pointer to DM and nothing runs.
- **No browser view in Slack.** A CAPTCHA/passkey step can't be handed over here; the card says so and points to the web app's one-time
  secure window.
- **Several requests at once.** A second message while one is running starts alongside it as its own task, with its own progress message
  and **Stop** button (up to `NAMI_MAX_TASKS_PER_USER`, default 3, shared with the web app; past it you are told to wait or stop one).
  Saying `cancel` stops everything that is running. Each task opens its own browser: the first holds your saved browser profile, the others
  start clean and get your saved logins back from the encrypted per-site snapshots. A paused card for an extra task is not rebuilt after a restart.
  The progress message has a **Stop** button while work is running: it stops the run, closes the browser and records the task as cancelled.
  A clarifying question has a Cancel button too.
- **Restarts don't lose a waiting card.** The pause is in the task's saved checkpoint and the DM's thread id is derived from
  (workspace, channel), so after a restart the bot finds the task again. An old button still works (it is checked against the
  waiting task, so a stale one is refused), and the next message shows the card again. Nothing is run by this: a task that crashed
  mid-step is not revived by a message. Only the request text for "Try again" is lost, and the bot says so.
- Not verified against a live workspace yet; the unit tests use a recording Slack client.
