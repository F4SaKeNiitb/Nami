"""Stage 2: decide what this task may touch. Plain code, computed from the user's text only.

CaMeL idea: the sites, credentials and approvals are fixed here, before any page is read, so a
page that says "also log in to bank.com" has nothing to attach to. Later stages receive this
object and cannot widen it.
"""

import re

from app.agent.schemas import AccessPlan, CredentialAsk, CredentialUse, Intent
from app.rules.engine import EffectiveRules
from app.sites.loader import SiteHints, hints_for
from app.vault.origins import origin_allowed, registrable_domain
from app.vault.service import CredentialView


def usable_credentials(site: str, creds: list[CredentialView], rules: EffectiveRules) -> list[CredentialUse]:
    """Stored logins the agent may type on `site`: right origin and allowed by the user's rules."""
    if not rules.policies.get("allow_stored_passwords", True):
        return []
    return [CredentialUse(credential_id=c.id, kind=c.kind, label=c.label, site=site)
            for c in _newest_per_kind([c for c in creds if c.kind in ("password", "totp") and origin_allowed(f"https://{site}", c.origins)])]


def _newest_per_kind(creds: list[CredentialView]) -> list[CredentialView]:
    """One login (and one 2FA key) per site: the most recently saved. Saving a new one already retires the old; this also covers
    duplicates saved before that rule, without deleting anything, so the agent is never asked to pick between several."""
    best: dict[str, CredentialView] = {}
    for c in creds:
        if c.kind not in best or c.created_at >= best[c.kind].created_at:
            best[c.kind] = c
    return list(best.values())


def api_credentials(site: str, creds: list[CredentialView], rules: EffectiveRules) -> list[CredentialUse]:
    """Saved API keys bound to this site's API host. Not blocked by allow_stored_passwords (a key is not a password)."""
    api = hints_for(site).api
    if not api:
        return []
    host = api["base"].split("//", 1)[-1].split("/", 1)[0]
    return [CredentialUse(credential_id=c.id, kind="api_key", label=c.label, site=site, last4=c.meta.get("last4"),
                          auth_scheme=c.meta.get("auth_scheme"))
            for c in _newest_per_kind([c for c in creds if c.kind == "api_key" and origin_allowed(f"https://{host}", c.origins)])]


_WORKSPACE_HOSTS = ("mail.google.com", "gmail.com", "googlemail.com", "drive.google.com", "docs.google.com",
                    "sheets.google.com", "slides.google.com")
_WORKSPACE_WORDS = re.compile(r"\b(gmail|inbox|e-?mails?|mail|drive|docs?|documents?|sheets?|spreadsheets?|slides?|pdfs?|files?|"
                              r"resume|cv|attachments?)\b", re.I)


def is_google_workspace(intent: Intent) -> bool:
    """A Gmail/Drive task, judged from the user's own words (before gmail.com is folded into google.com)."""
    hosts = [s.lower().split("//")[-1].split("/")[0] for s in intent.sites]
    if any(h == w or h.endswith("." + w) for h in hosts for w in _WORKSPACE_HOSTS):
        return True
    return any(registrable_domain(h) == "google.com" for h in hosts) and bool(_WORKSPACE_WORDS.search(intent.goal or ""))


def google_credentials(creds: list[CredentialView]) -> list[CredentialUse]:
    return [CredentialUse(credential_id=c.id, kind="oauth", label=c.label, site="googleapis.com") for c in creds
            if c.kind == "oauth" and c.meta.get("provider") == "google" and origin_allowed("https://googleapis.com", c.origins)]


_GOOGLE_SSO = re.compile(r"\b(sign(ed)?[ -]?in|log ?in|login|continue|connect)\b.{0,40}?\b(with|using|via|through|by)\s+(my\s+)?google\b|"
                         r"\bgoogle\s+(sign[ -]?in|log ?in|sso)\b", re.I)


def wants_google_signin(intent: Intent) -> bool:
    """The user asked to sign in WITH Google on another site. Judged from the user's own words (the intent), never from a page."""
    return bool(intent.google_signin or _GOOGLE_SSO.search(intent.goal or ""))


_API_KEY_WORDS = re.compile(r"\bapi[\s_-]?keys?\b", re.I)


def wants_api_route(intent: Intent, request: str) -> bool:
    """The user asked to CREATE something with a model, or said to use their API key, on a site that has an API definition.
    The intent reader often leaves needs_login false for this ("generate a poem with my OpenRouter key"), which would send the
    task to the website. Judged from the user's own words and the intent, never from a page."""
    if not intent.sites or not hints_for(intent.sites[0]).api:
        return False
    return bool(intent.generate or _API_KEY_WORDS.search(request or ""))


def google_sso_login(site: str, intent: Intent, creds: list[CredentialView], rules: EffectiveRules) -> list[CredentialUse]:
    """The saved google.com login to use for 'Continue with Google' on `site`: only when the user asked for it, only for a
    site that is not Google, only if the rules allow both saved passwords and google.com."""
    if site == "google.com" or not wants_google_signin(intent) or not rules.domain_allowed("google.com"):
        return []
    return usable_credentials("google.com", creds, rules)


def with_google(plan: AccessPlan, creds: list[CredentialUse]) -> AccessPlan:
    """Sign in on plan.allowed_domains[0] through Google. google.com joins the allowed sites (after the main one) and only
    the Google login is released; the placeholders only ever work on google.com."""
    plan.login, plan.credentials, plan.sign_in_via = "password", creds, "google"
    if "google.com" not in plan.allowed_domains:
        plan.allowed_domains = [*plan.allowed_domains, "google.com"]
    return plan


_REASONS = {
    "sms": "{site} texts you a code when you sign in. Add your login below and I'll ask you for the code here when it arrives.",
    "email": "{site} emails you a code when you sign in. Add your login below and I'll ask you for the code here.",
    "google": "{site} may sign in with Google, which blocks automated sign-in. If your {site} account also has an email and password, add that below and I'll sign in myself.",
}


NO_SITE_MESSAGE = ("I'm not sure which website that's for. Tell me the site, or what you want done and where, for example "
                   "“check my OpenRouter credits” or “reorder my last Swiggy order”.")


def credential_ask(site: str, hints: SiteHints, rules: EffectiveRules, intent: Intent, *, google: bool = False) -> CredentialAsk:
    """The follow-up question for a site with nothing usable saved. `site` is always the plan's site, never page text."""
    kinds: list = []
    if not google and wants_google_signin(intent) and site != "google.com":      # no saved Google login to use: say which one is missing
        return CredentialAsk(site=site, kinds=[], google_signin=True, allow_takeover=True, reason=(
            f"You asked me to sign in to {site} with Google, but I don't have a saved Google login. Add one first under "
            "\"Add a login or API key\" (website: google.com), then ask again. Or I can open a one-time secure window for you to sign in."))
    if not google and intent.generate and hints.api:       # making something with the site's model needs the key, not a login (found live: the card asked for a
        return CredentialAsk(site=site, kinds=["api_key"], google_signin=False, allow_takeover=False, reason=(      # password, 2FA and explained Google sign-in)
            f"To generate this with {site} I need your {site} API key. Add it below (stored encrypted; I never see it) and I'll use it for this request. "
            "I always ask before spending credits."))
    if not google:
        if hints.api and (not intent.actions or intent.generate):
            kinds.append("api_key")
        if rules.policies.get("allow_stored_passwords", True):
            kinds += ["password", "totp"]
    if google:
        reason = "I need your Google account for this, but Connect Google isn't available here. You can sign in once in a secure window."
    elif not rules.policies.get("allow_stored_passwords", True):
        reason = f"Your rules don't let me use saved passwords, so I can't sign in to {site} on my own. You can sign in once in a secure window."
    elif kinds and kinds[0] == "api_key":                   # the simplest route first; the login is the alternative (found live: the card led with a Google-sign-in aside)
        reason = (f"The simplest way to do this on {site} is with your API key. Add it below (stored encrypted; I never see it)."
                  + (" Or add an email login instead." if "password" in kinds else "")
                  + (f" ({site} often signs in with Google, which I can't automate, so a key or an email login is what works.)" if hints.google_signin else ""))
    else:
        reason = (_REASONS.get(hints.human_step or "", "") or f"I need a login for {site}. Add it below, stored encrypted and never shown to me or in chat, "
                  "and I'll sign in myself.").format(site=site)
    return CredentialAsk(site=site, kinds=kinds, google_signin=hints.google_signin, allow_takeover=True, reason=reason)


def build_access_plan(intent: Intent, creds: list[CredentialView], rules: EffectiveRules, *,
                      session_sites: set[str], google_workspace: bool = False, google_connect: bool = False) -> AccessPlan:
    domains = sorted({d for s in intent.sites if (d := registrable_domain(s))})
    plan = AccessPlan(allowed_domains=domains)
    if not domains:
        plan.blocked = NO_SITE_MESSAGE
        return plan
    denied = [d for d in domains if not rules.domain_allowed(d)]
    if denied:
        plan.blocked = f"Your rules do not allow me to use {', '.join(denied)}."
        return plan
    for a in intent.actions:
        if rules.blocked(a):
            plan.blocked = f"Your rules do not allow me to do this: {a.replace('_', ' ')}."
            return plan
    plan.approval_actions = sorted({a for a in intent.actions if rules.needs_approval(a)})

    for d in domains:
        plan.notes += hints_for(d).notes
    if "purchase" in intent.actions and rules.policies.get("allow_card_use", True):
        plan.cards = [CredentialUse(credential_id=c.id, kind="card", label=c.label, site=d, last4=c.meta.get("last4"))
                      for d in domains for c in creds if c.kind == "card" and origin_allowed(f"https://{d}", c.origins)]
    if not intent.needs_login:
        return plan

    site = domains[0]
    if google_workspace and site == "google.com":             # Gmail/Drive: Google's API with the connected account, no browser
        google = google_credentials(creds)
        if google:
            plan.login, plan.credentials = "google_api", google[:1]
            plan.notes.append("I'll use your connected Google account for this (no browser, no password).")
            return plan
        if google_connect:
            plan.login = "connect_google"
            return plan
        plan.login, plan.connect_site = "need_credentials", site
        plan.ask = credential_ask(site, hints_for(site), rules, intent, google=True)
        return plan
    hints = hints_for(site)
    usable = usable_credentials(site, creds, rules)
    api_keys = api_credentials(site, creds, rules)
    # a read-only task or a generation request, with a saved key: use the API, no browser (generation still waits for approval)
    if hints.api and api_keys and (not intent.actions or (intent.generate and set(intent.actions) <= {"submit_form"})):
        plan.login, plan.credentials = "api", api_keys[:1]
        plan.notes.append(f"I'll use your saved {site} API key for this (read-only), without opening a browser.")
    elif site in session_sites:
        plan.login = "session"
    elif sso := google_sso_login(site, intent, creds, rules):
        with_google(plan, sso)
        plan.notes.append(f"I'll sign in to {site} with 'Continue with Google', using your saved Google login.")
    elif usable:
        plan.login, plan.credentials = "password", usable
    else:
        plan.login, plan.connect_site = "need_credentials", site
        plan.ask = credential_ask(site, hints, rules, intent)
    return plan
