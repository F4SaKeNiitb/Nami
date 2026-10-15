"""Small, dependency-light helpers for live local-site evaluations."""

from app.agent.schemas import Intent, Plan, Subgoal
from app.browser.broker import Broker, BrokerPayload


class LocalScenarioLLM:
    """Pin a local fake host and read-only scope after real intent extraction."""

    def __init__(self, delegate, site: str, start_url: str, *, preserve_purchase_action: bool = False):
        self.delegate, self.site, self.start_url = delegate, site, start_url
        self.preserve_purchase_action = preserve_purchase_action

    @property
    def usage(self):
        return self.delegate.usage

    async def json(self, **kwargs):
        result = await self.delegate.json(**kwargs)
        if kwargs.get("schema") is Intent:
            result = result.model_copy(update={"sites": [self.site], "needs_login": True,
                                              "actions": (["purchase"] if self.preserve_purchase_action else []),
                                              "generate": False, "unsupported": None})
        elif kwargs.get("schema") is Plan:
            steps = list(result.subgoals)
            auth_idx = next((i for i, sg in enumerate(steps) if sg.is_auth), None)
            if auth_idx is None:
                auth_idx = next((i for i, sg in enumerate(steps)
                                 if any(word in sg.goal.lower() for word in ("sign in", "sign-in", "log in", "login"))), None)
            if auth_idx is None:
                steps.insert(0, Subgoal(id=1, goal=f"Open {self.start_url} and sign in using the saved login placeholders",
                                        success_criterion="the page shows you are signed in (account menu or dashboard, no login form)",
                                        is_auth=True))
            else:
                auth = steps[auth_idx].model_copy(update={
                    "goal": f"Open {self.start_url} and sign in using the saved login placeholders", "is_auth": True})
                steps[auth_idx] = auth
                if auth_idx:
                    steps.insert(0, auth)
                    del steps[auth_idx + 1]
            for i, step in enumerate(steps, 1):
                step.id = i
            result = Plan(subgoals=steps[:10])
        return result


class LocalScenarioBroker(Broker):
    """Allow credential placeholders on the fake HTTP origin (production sites remain HTTPS-only)."""

    def __init__(self, vault, *, site: str, **kwargs):
        super().__init__(vault, **kwargs)
        self.site = site

    def _allow_local_http(self, payload):
        if self.site in {"127.0.0.1", "localhost"}:
            secure_pattern = f"https://*.{self.site}"
            if values := payload.sensitive_data.pop(secure_pattern, None):
                payload.sensitive_data[f"http*://*.{self.site}"] = values
        return payload

    async def build(self, access, *, include_cards: bool = False):
        payload = await super().build(access, include_cards=include_cards)
        self._allow_local_http(payload)
        return LocalScenarioPayload(payload, site=self.site)


class LocalScenarioPayload(BrokerPayload):
    """Payload that scopes the just-in-time user OTP to the local HTTP fixture."""

    def __init__(self, source: BrokerPayload, *, site: str):
        super().__init__(sensitive_data=source.sensitive_data, placeholders=source.placeholders,
                         notes=source.notes, _values=source._values)
        self.site = site

    def add_one_time(self, site: str, name: str, value: str) -> None:
        if site == self.site and site in {"127.0.0.1", "localhost"}:
            self.sensitive_data.setdefault(f"http*://*.{site}", {})[name] = value
            self.placeholders.append(name)
            self._values.append(value)
            return
        super().add_one_time(site, name, value)
