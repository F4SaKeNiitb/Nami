"""The plain-language lines that describe what the agent is doing. One place, so every surface (web chat, Slack) says the same thing."""


def progress_lines(type_: str, p: dict) -> list[str]:
    if type_ == "plan":
        return ["**Plan**\n" + "\n".join(f"{g['id']}. {g['goal']}" for g in p["subgoals"])]
    if type_ == "intent":
        note = " (matched a similar request you made before)" if p.get("cache") == "semantic" else ""
        return [f"🧭 Understood: {p['goal']}{note}"]
    if type_ == "access_plan":
        return [f"ℹ️ {n}" for n in p.get("notes", [])]
    if type_ == "subgoal_start":
        return [f"▸ {p['goal']}"]
    if type_ == "step" and p.get("text"):
        return [f"   · {p['text']}"]
    if type_ == "validated":
        return [("✓ " if p["passed"] else "✗ ") + (p.get("what_changed") or p.get("evidence", ""))]
    if type_ == "retry":
        return [f"↻ Trying again: {p['reason']}"]
    if type_ == "replan":
        return ["↻ That route is stuck; planning a different one."]
    if type_ == "session_expired":
        return [f"🔒 Your {p['site']} session expired."]
    if type_ == "need_credentials":
        return [f"🔐 I need a login for {p['site']}. Add it in the secure form and I'll sign in myself."]
    if type_ == "ask_code":
        return [f"✉️ {p['site']} sent a verification code. I'll ask you for it here."]
    if type_ == "explore_start":
        return [f"🔎 Reading {', '.join(p.get('sites', []))} and working out the answer."]
    if type_ == "explore_step" and p.get("tool") == "web_open":
        return [f"   · Opening {p.get('url', '')}"]
    if type_ == "explore_fallback":
        return ["↩️ That needs more care than a plain read, so I'm switching to the step-by-step route."]
    if type_ == "signin_once_requested":
        return [f"🪟 Opening a one-time secure window for {p['site']}, as you asked."]
    if type_ == "reauth":
        return [f"🔑 Your {p['site']} session expired. Signing in again with your saved login."]
    if type_ == "connected":
        return [f"🔗 Connected to {p['site']}. I saved the session, encrypted.",
                f"💡 Next time you won't need to sign in to {p['site']} again. Revoke it any time under Connections."]
    if type_ == "gateway_block":
        what = "went to a site outside this task" if "off_plan_url" in p.get("rules", []) else "tried to give me instructions"
        return [f"🛡️ A page {what}. I ignored it."]
    if type_ == "navigation_blocked":
        return ["↩️ I won't guess web addresses; clicking through the page's own links instead."]
    if type_ == "approved":
        return ["✓ You approved this step."]
    return []
