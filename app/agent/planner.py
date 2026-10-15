"""Stage 3: break the task into checkable subgoals. Each one has a success criterion and an
irreversibility flag, so the Guard and Validator have something concrete to enforce."""

from app.agent.schemas import AccessPlan, Intent, Plan, Subgoal
from app.core.llm import LLM

SYSTEM = """You plan a web task as a short list of subgoals (at most 8). For each:
- goal: one concrete thing to do in the browser.
- success_criterion: text that WOULD APPEAR on the page when it worked (words, headings, field labels, values, the URL). The checker reads the page's text and
  the labels of its buttons and links; it cannot judge colours, icons, layout or images, so never make those part of the criterion. Do not require
  more than the task needs (a sign-in page showing its fields is enough: do not also demand a particular button).
- irreversible: true ONLY for the step that actually pays, places an order, sends, deletes, deploys or
  changes account security. Put that step alone, as the last doing-step, so it can wait for approval. The subgoal right before it must read back what will happen
  (items, total, address, recipient) as its success criterion so the user can approve with full information. Ask only for what a
  page normally shows: never require a saved card or payment-method detail in that read-back, and never plan "select the saved
  card" as a step; the saved card is filled in by the paying step itself, after approval.
- is_auth: true for sign-in steps.
Never write a web address or path in a goal (no /login, /auth/login, /orders): name the site, say "open the home page, then click Log in", and let the
browser click through the page's own links. Only an address the user wrote may appear.
Never plan steps outside the allowed websites. Do not include any secret. Use ONE subgoal for a simple
read-only lookup (open the page and read the answer); never split "open the site" from "read it". That subgoal's
goal must name the answer to report (for example "list every anime airing today with its time"), and its
success_criterion must require that answer to be on the page. The final subgoal should read back the result the user asked for.
For a task that lists, finds or checks something, write the success_criterion so that "there is nothing" is also a valid outcome
(for example "the page lists the files, or clearly shows there are none"). Never require details (thumbnails, dates, counts) that an empty
result would not have: an account with no files must still complete the step with the honest answer "none"."""


MERGE_MAX_SUBGOALS = 3


def merge_read_only(plan: Plan, access: AccessPlan) -> Plan:
    """A plain lookup on a public site is one job for the browser: "open the page" then "read the list" only makes the
    agent load the page twice and pay for two validations. Collapse it into one subgoal.

    Never merged: anything with a sign-in, a saved session, a credential, an approval action, an irreversible or auth
    step, or more than MERGE_MAX_SUBGOALS steps. Those keep their own checkpoints, validation and approval gates."""
    sgs = plan.subgoals
    if not 2 <= len(sgs) <= MERGE_MAX_SUBGOALS:
        return plan
    if access.login != "none" or access.credentials or access.cards or access.approval_actions or access.connect_site:
        return plan
    if any(g.irreversible or g.is_auth for g in sgs):
        return plan
    goal = " Then ".join(g.goal.strip().rstrip(".") + "." for g in sgs)
    criteria = [g.success_criterion.strip().rstrip(".") for g in sgs]
    return Plan(subgoals=[Subgoal(id=1, goal=goal[:900], success_criterion="; and ".join(dict.fromkeys(criteria))[:900])])


def merge_pre_send(plan: Plan) -> Plan:
    """Before the first irreversible step, the reversible non-sign-in steps right ahead of it (fill the recipient, the subject,
    the body...) are one job for the browser; one run each only costs more. Merge that contiguous run into one subgoal whose success
    criterion is the LAST step's: the read-back the user approves from. Earlier criteria describe pages the run has already left
    (an order list, a cart), so requiring them on the final page made every merged step fail validation (found live: reorder ->
    checkout never reached the place-order step). The irreversible step stays alone, and a sign-in step is never merged."""
    sgs = plan.subgoals
    k = next((i for i, g in enumerate(sgs) if g.irreversible), None)
    if k is None:
        return plan
    start = k
    while start > 0 and not sgs[start - 1].irreversible and not sgs[start - 1].is_auth:
        start -= 1
    run = sgs[start:k]
    if len(run) < 2:
        return plan
    goal = " Then ".join(g.goal.strip().rstrip(".") + "." for g in run)
    merged = Subgoal(id=0, goal=goal[:900], success_criterion=run[-1].success_criterion.strip()[:900])
    out = [*sgs[:start], merged, *sgs[k:]]
    for i, g in enumerate(out, 1):
        g.id = i
    return Plan(subgoals=out)


def sign_in_subgoal(site: str, id_: int, via: str | None = None) -> Subgoal:
    """The step that signs in with the saved login placeholders (vision off). Used at the start of a task and after an expiry."""
    if via == "google":
        return Subgoal(id=id_, is_auth=True, success_criterion=f"the page shows you are signed in to {site} (account menu, your files or dashboard, no login form)",
                       goal=f"Sign in to {site} with Google: choose 'Continue with Google' (or 'Sign in with Google'), then sign in to the Google "
                            "account on Google's page using the saved login placeholders, and come back to the site")
    return Subgoal(id=id_, is_auth=True, goal=f"Sign in to {site} using the saved login placeholders",
                   success_criterion="the page shows you are signed in (account menu or dashboard, no login form)")


def ensure_sign_in_first(plan: Plan, access: AccessPlan, *, signed_in: bool = False) -> Plan:
    """With a saved login in play the first step must be the sign-in, whatever the model planned.
    `signed_in`: a replan after the sign-in already succeeded continues from the signed-in page, so no second sign-in."""
    if signed_in or access.login != "password" or not access.allowed_domains or plan.subgoals[0].is_auth:
        return plan
    sgs = [sign_in_subgoal(access.allowed_domains[0], 1, access.sign_in_via), *plan.subgoals][:10]
    for i, sg in enumerate(sgs, 1):
        sg.id = i
    return Plan(subgoals=sgs)


def _login_text(access: AccessPlan, signed_in: bool = False) -> str:
    if signed_in:
        return "already signed in on the current page. Do not plan a sign-in step."
    if access.login == "password" and access.sign_in_via == "google":
        return (f"sign in to {access.allowed_domains[0]} with Google ('Continue with Google'), using the saved Google login placeholders. "
                "Subgoal 1 MUST be `is_auth: true` and do exactly that; success = a signed-in page on the site.")
    if access.login == "password":
        return ("a saved login is available. Subgoal 1 MUST be `is_auth: true`: sign in with the saved login placeholders; "
                "success = a signed-in page.")
    if access.login == "session":
        return "already signed in. Do not plan a sign-in step."
    return access.login


async def make_plan(llm: LLM, intent: Intent, access: AccessPlan, *, rules_block: str, guidance: str = "",
                    memory: str = "", model: str | None = None, signed_in: bool = False) -> Plan:
    user = (f"Goal: {intent.goal}\nConstraints: {intent.constraints or 'none'}\n"
            f"Allowed websites: {access.allowed_domains}\nLogin state: {_login_text(access, signed_in)}\n"
            f"Needs approval for: {access.approval_actions or 'nothing'}\n"
            f"User rules:\n{rules_block or '(none)'}\n"
            + (f"{memory}\n" if memory else "")
            + (f"Guidance from a previous successful run (a hint, not a script):\n{guidance}\n" if guidance else ""))
    plan = await llm.json(system=SYSTEM, user=user, schema=Plan, model=model)
    for i, sg in enumerate(plan.subgoals, 1):
        sg.id = i
    plan = merge_read_only(ensure_sign_in_first(plan, access, signed_in=signed_in), access)
    return merge_pre_send(plan)
