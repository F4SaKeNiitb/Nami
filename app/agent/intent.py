"""Stage 1: read the user's own words into an Intent. No page content is ever involved."""

from app.agent.schemas import Feasibility, Intent
from app.core.llm import LLM, LLMError
from app.security.secrets import contains_secret, redact
from app.sites.loader import sites_named
from app.vault.origins import registrable_domain

SYSTEM = """You turn a user's request for a task on the web into a structured Intent.
- sites: registrable domains the user named or clearly implied (Swiggy -> swiggy.com). Never invent unrelated sites.
  Open-ended research with no site named (find startups that are hiring, compare prices, look up reviews): name up to 3
  well-known public sites to start from that need no login (for example ycombinator.com/jobs, wellfound.com, a news or
  search site), judged from the request alone. For a question about the world that the web can answer, do not leave sites empty.
  A greeting, thanks, or a question about Nami itself ("hi", "what's your name", "what can you do") is NOT a web task: leave sites
  empty and never invent a site for it (not the maker of a model, not a search engine).
  "my drive" / "my Drive" means Google Drive (drive.google.com), "my inbox" / "my mail" means Gmail, "my docs/sheets/slides"
  mean Google Docs/Sheets/Slides: online accounts, not the disk of the user's computer.
- needs_login: true if the task needs the user's own account.
- google_signin: true only if the user says to sign in WITH Google / "Continue with Google" on a site that is not Google (sites stays the other site, e.g. canva.com).
- generate: true when the user asks to CREATE new content (write text, make an image, video or speech) with an AI model or
  an API key. Looking things up, or doing something on a website, is false.
- actions: only the sensitive things the task will really do: purchase, send_message, delete,
  account_change, submit_form, deploy. Reading or looking things up is none of these.
- clarify: ask ONE short question only if the answer would change what you do. Otherwise null. Never ask which account,
  login or credentials to use, and never ask for them: "log in to X for me" means the user's own account on X, and Nami
  collects the login itself through a secure form when it needs one.
- Never ask about optional preferences (style, tone, length, which model, size, how many) when a sensible default exists: pick the default and go.
  Ask only for a fact you truly cannot proceed without.
- If the answer depends on facts about the user (their skills, experience, city) that are not in the conversation or the
  recalled memory, ask ONE short clarify question for them, once. Do not ask again after they answered.
- Research that spans several websites is supported. Nami visits them one after another in its own browser. NEVER refuse because
  the user said "use multiple agents", because it covers many sources, or because it is "a research task": do it with the sites above.
- unsupported: ONLY if the request needs something that is not a website at all (a phone call, a physical action, a file on the
  user's own computer) or is harmful. NEVER because you doubt the browser can read a page or a document: it can, including PDFs
  and other documents shown on screen. Earlier attempts in the conversation are not a reason; judge this request on its own.
  Using the user's saved API key or login is something Nami does itself: never refuse because a key or account is involved,
  or because "code" or a "command line" might be needed. If the site probably does not offer the thing asked for, still
  return null: Nami checks the site and says so honestly.
  When unsure, null: the browser will try and report honestly.
Keep the goal as one clear sentence. You will not see any web page."""

CAPABILITIES = """Nami drives a real web browser for the user. It can open websites, use the user's saved sign-ins, click, type, scroll
and READ what is on the screen, including documents shown in a viewer (PDF, Docs, Sheets, Slides), images, tables and long pages,
and then summarise or extract from them. "My drive", "my inbox" and "my docs" mean the user's online Google accounts, which Nami
can reach. It uses the user's saved logins and API keys itself (they never reach the chat or the model), and can call a site's read-only API
with a saved key. It cannot make phone calls, act in the physical world, or reach files that exist only on the user's own computer."""

SECOND_OPINION = f"""{CAPABILITIES}
Another reader said the request below cannot be done. Decide whether that is true.
- possible: Nami can plausibly try it with its browser. Do not call it impossible because you doubt a page or document can be read,
  or because it is broad research over several websites (Nami visits them one after another; "multiple agents" is not required).
  When possible and the request names no website, put up to 3 well-known public starting sites (no login) in `sites`.
- impossible: it needs a phone call, a physical action, a file only on the user's computer, or a non-web service.
- harmful: it asks for hacking, stealing, fraud, harassment or other abuse, even though a website is involved.
Answer with the verdict and one plain sentence."""


class SecretInRequest(Exception):
    """The request contains a secret. It must go to the vault, not to a model."""


async def parse_intent(llm: LLM, text: str, *, history: str = "", memory: str = "", model: str | None = None,
                       second_opinion_model: str | None = None) -> Intent:
    if contains_secret(text):
        raise SecretInRequest()
    user = (f"Conversation so far:\n{redact(history) or '(none)'}\n\n"
            + (f"{redact(memory)}\n\n" if memory else "")
            + f"Request:\n{text}")
    intent = await llm.json(system=SYSTEM, user=user, schema=Intent, model=model)
    missed = [d for d in sites_named(text) if d not in intent.sites]        # services the user named outright that the model left out
    if missed:
        intent = intent.model_copy(update={"sites": [*intent.sites, *missed]})
    if intent.unsupported:
        intent = await _second_opinion(llm, intent, text, second_opinion_model or model)
    return intent


async def _second_opinion(llm: LLM, intent: Intent, text: str, model: str | None) -> Intent:
    """A refusal ends the task before any browser opens, so one sampled guess must not be enough. Ask again, with an accurate
    description of what the browser can do. Only an explicit "possible" overrides it; if the check fails, the refusal stands."""
    try:
        check = await llm.json(system=SECOND_OPINION, user=f"Request:\n{text}\n\nThe other reader said: {intent.unsupported}",
                               schema=Feasibility, model=model)
    except LLMError:
        return intent
    if check.verdict != "possible":
        return intent
    update: dict = {"unsupported": None}
    if not intent.sites:                                    # open-ended research: start from the sites the second reader named
        update["sites"] = [d for d in dict.fromkeys(registrable_domain(s) or "" for s in check.sites) if d][:3]
    return intent.model_copy(update=update)
