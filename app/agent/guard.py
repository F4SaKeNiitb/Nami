"""Deterministic gate for irreversible actions. Three layers, none of them a prompt:
  1. the planner marked the subgoal irreversible          -> approval before it starts
  2. the access plan lists the action category            -> approval before it starts
  3. a URL / page-text backstop while the executor runs   -> stop and ask if it drifts into checkout etc.
"""

import re
from dataclasses import dataclass

from app.agent.schemas import AccessPlan, Subgoal

_RISKY_URL = re.compile(r"(checkout|/pay\b|payment|place[-_]?order|confirm[-_]?order|billing|delete|/transfer)", re.I)
_RISKY_TEXT = re.compile(r"\b(place order|pay now|confirm payment|confirm order|buy now|delete account|send money)\b", re.I)


@dataclass(frozen=True)
class Gate:
    needs_approval: bool
    reason: str = ""


class Guard:
    def __init__(self, access: AccessPlan):
        self.access = access
        self._approved: set[int] = set()

    def approve(self, subgoal_id: int) -> None:
        self._approved.add(subgoal_id)

    def approved(self, subgoal_id: int) -> bool:
        return subgoal_id in self._approved

    def gate_subgoal(self, sg: Subgoal) -> Gate:
        if self.approved(sg.id):
            return Gate(False)
        if sg.irreversible:
            return Gate(True, "this step cannot be undone")
        return Gate(False)

    def backstop(self, sg: Subgoal, *, url: str, page_text: str = "") -> Gate:
        """Called on every executor step. Stops a non-approved subgoal that reaches a risky page."""
        if self.approved(sg.id) or sg.irreversible:
            return Gate(False)
        if self.access.approval_actions and (_RISKY_URL.search(url) or _RISKY_TEXT.search(page_text)):
            return Gate(True, "this page looks like a payment or confirmation step")
        return Gate(False)
