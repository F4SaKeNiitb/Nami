"""Typed contracts between agent stages. Every stage returns one of these, never free text."""

from typing import Literal

from pydantic import BaseModel, Field

from app.rules.policies import ACTION_CATEGORIES

Action = Literal["purchase", "send_message", "delete", "account_change", "submit_form", "deploy"]
Login = Literal["none", "session", "password", "takeover", "need_credentials", "api", "google_api", "connect_google"]


class Intent(BaseModel):
    """What the user asked for, read from the user's own words only."""
    goal: str
    sites: list[str] = Field(default_factory=list, description="website domains the user named or clearly implied, e.g. swiggy.com")
    needs_login: bool = False
    generate: bool = Field(False, description="the user asks to CREATE new content (text, image, video, speech) with an AI model or API key")
    actions: list[Action] = Field(default_factory=list, description="irreversible or sensitive things the task will do")
    google_signin: bool = Field(False, description="the user says to sign in WITH Google / 'Continue with Google' on a website that is not Google itself")
    constraints: list[str] = Field(default_factory=list)
    clarify: str | None = Field(None, description="ONE question, only if the answer changes the outcome")
    unsupported: str | None = Field(None, description="plain-language reason if this cannot be done on the web")


class Feasibility(BaseModel):
    """A second opinion on a request the first reader declined."""
    verdict: Literal["possible", "impossible", "harmful"]
    reason: str = ""
    sites: list[str] = Field(default_factory=list, description="up to 3 public starting sites, only when possible and none was named")


class CredentialUse(BaseModel):
    credential_id: str
    kind: str
    label: str
    site: str
    last4: str | None = None
    auth_scheme: dict | None = None      # how an API key is sent, once worked out (a header name or similar, never a secret)


class CredentialAsk(BaseModel):
    """What Nami asks the user to add in chat when nothing usable is saved. Built in code from the user's site only."""
    site: str
    kinds: list[Literal["password", "totp", "api_key"]] = Field(default_factory=list)
    google_signin: bool = False
    allow_takeover: bool = True              # the explicit, opt-in "Sign in once" window
    reason: str = ""


class AccessPlan(BaseModel):
    """What this task may touch. Computed in code from the user's text before any page is read."""
    allowed_domains: list[str]
    login: Login = "none"
    credentials: list[CredentialUse] = Field(default_factory=list)
    cards: list[CredentialUse] = Field(default_factory=list)     # released only for an approved irreversible step
    approval_actions: list[str] = Field(default_factory=list)
    blocked: str | None = None
    connect_site: str | None = None          # set when the user must connect an account first
    notes: list[str] = Field(default_factory=list)
    ask: CredentialAsk | None = None         # set with login == "need_credentials"
    sign_in_via: Literal["google"] | None = None      # "Continue with Google" on allowed_domains[0], using the saved google.com login


class Subgoal(BaseModel):
    id: int
    goal: str
    success_criterion: str
    irreversible: bool = False
    is_auth: bool = False


class Plan(BaseModel):
    subgoals: list[Subgoal] = Field(min_length=1, max_length=10)


class Validation(BaseModel):
    passed: bool
    confidence: float = Field(ge=0, le=1)
    evidence: str
    what_changed: str = ""
    amount: float | None = Field(None, description="the total the user would pay if the page shows a checkout total, as a number")
    currency: str | None = Field(None, description="ISO currency code of that total, e.g. INR, USD")


class Verification(BaseModel):
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(description="the direct answer to the request: for a lookup, the concrete items found; for an action, what was done")
    facts: dict[str, str] = Field(default_factory=dict, description="one entry per confirmed item or key fact: order id, total, address, each listing")
    unverified: list[str] = Field(default_factory=list, description="things that could not be confirmed")


assert set(Action.__args__) == ACTION_CATEGORIES  # schema and rules engine must agree


class ApiChoice(BaseModel):
    """Which read-only API endpoint answers the request. The model picks a NAME from a fixed list, never a URL."""
    endpoint: str = Field(description="one of the offered endpoint names, or 'none' if no offered endpoint can answer this")
    params: dict[str, str] = Field(default_factory=dict, description="values for that endpoint's parameters, taken only from the user's request")
    reason: str = ""
