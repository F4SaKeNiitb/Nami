"""Which LLM calls may be cached, and how.

Anything that depends on live page state is never cached: the executor, validator, verifier
and guard all see a website that changes between runs. Only calls whose input is the
user's own words (plus rules) qualify:

  purpose   exact  semantic  why
  intent    yes    yes       "repeat my last swiggy order" ≈ "repeat my last swiggy order please"
  plan      yes    no        keyed on the *structured* intent JSON (site, goal, params), so a
                             plan is only reused for an identical parsed intent
  clarify   yes    no        depends on exact wording
  title     yes    no        different text → different title
  summary   yes    no        a near-match of a different text is a wrong summary

Why only intent is semantic: embedding similarity separates intents poorly. With bge-small,
"show my last Swiggy order" scores 0.927 against "repeat my last Swiggy order", while the
real paraphrase "reorder my previous Swiggy order" scores 0.930. A wrong *intent* hit is
visible and cheap: the parsed intent is shown and confirmed, and the approval gate still
applies. A wrong *plan* hit would act on the wrong goal. See tests/test_semantic_calibration.py.

Partitions are per (org, user). That lowers the hit rate compared with sharing across a
firm, but it guarantees one user's cached plan can never surface for another user.
"""

from dataclasses import dataclass
from typing import Any

from app.cache.exact import ExactCache, digest
from app.cache.semantic import SemanticCache
from app.security.secrets import contains_secret

EXACT_PURPOSES = {"intent", "plan", "clarify", "title", "summary"}
SEMANTIC_PURPOSES = {"intent"}


@dataclass(frozen=True)
class LLMCacheScope:
    org_id: str
    user_id: str
    model: str
    purpose: str
    rules_version: str = ""
    prompt_version: str = "v1"

    def partition(self) -> str:
        return digest(self.org_id, self.user_id, self.model, self.purpose,
                      self.rules_version, self.prompt_version)[:32]


@dataclass(frozen=True)
class LLMCacheHit:
    response: Any
    kind: str  # "exact" | "semantic"
    similarity: float = 1.0


class LLMCache:
    def __init__(self, exact: ExactCache, semantic: SemanticCache | None, ttl_s: int):
        self.exact, self.semantic, self.ttl_s = exact, semantic, ttl_s

    @staticmethod
    def cacheable(scope: LLMCacheScope, prompt: str) -> bool:
        return scope.purpose in EXACT_PURPOSES and not contains_secret(prompt)

    async def lookup(self, scope: LLMCacheScope, prompt: str) -> LLMCacheHit | None:
        if not self.cacheable(scope, prompt):
            return None
        part = scope.partition()
        hit = await self.exact.get_json("llm", digest(part, prompt))
        if hit is not None:
            return LLMCacheHit(hit, "exact")
        if self.semantic and scope.purpose in SEMANTIC_PURPOSES:
            s = await self.semantic.get(part, prompt)
            if s:
                return LLMCacheHit(s.response, "semantic", s.similarity)
        return None

    async def store(self, scope: LLMCacheScope, prompt: str, response: Any) -> None:
        if not self.cacheable(scope, prompt):
            return
        part = scope.partition()
        await self.exact.set_json("llm", digest(part, prompt), response, self.ttl_s)
        if self.semantic and scope.purpose in SEMANTIC_PURPOSES:
            await self.semantic.put(part, prompt, response)
