"""Strict shapes for learned skills. Anything derived from a web trajectory is untrusted until scanned and signed."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.schemas import Action

MAX_STEPS = 12
MAX_STEP_LEN = 240


class SkillContent(BaseModel):
    """What a skill says. Guidance text only, unless `code` is set (a snippet, which is gated much harder)."""
    model_config = ConfigDict(extra="forbid")
    site: str = Field(max_length=100)
    intent: str = Field(min_length=3, max_length=200)
    steps: list[str] = Field(min_length=1, max_length=MAX_STEPS)
    pitfalls: list[str] = Field(default_factory=list, max_length=5)
    code: str | None = Field(None, max_length=2000, description="JavaScript for run_cdp_snippet; None for plain workflows")
    params: dict[str, str] = Field(default_factory=dict, max_length=5,
                                   description="slot name -> the user's original wording, for slots like {constraint_1} in steps")

    def texts(self) -> list[str]:
        return [self.site, self.intent, *self.steps, *self.pitfalls, self.code or "", *self.params.values()]


class Manifest(BaseModel):
    """What the skill is allowed to touch. Derived from what the run actually did, then capped by the access plan."""
    model_config = ConfigDict(extra="forbid")
    domains: list[str] = Field(max_length=10)
    actions: list[Action] = Field(default_factory=list)
    has_code: bool = False
    risk_tier: Literal["low", "medium", "high"] = "low"


Status = Literal["candidate", "approved", "revoked", "rejected"]
Scope = Literal["user", "org"]
