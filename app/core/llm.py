"""One place that talks to a model for the agent's own reasoning (intent, plan, validate, verify).

The browser executor uses browser-use's own chat class; this wrapper serves everything else.
Output is always parsed into a Pydantic model; free text never drives control flow.
"""

import base64
import json
import re
from typing import Protocol, TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from app.telemetry import tel

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLM(Protocol):
    usage: dict[str, int]

    async def json(self, *, system: str, user: str, schema: type[T], images: list[bytes] | None = None,
                   model: str | None = None, max_tokens: int = 1500) -> T: ...


def extract_json(text: str) -> str:
    """Pull the outermost JSON object out of a reply that may wrap it in prose or a code fence."""
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LLMError("model reply contained no JSON object")
    return text[start:end + 1]


class AnthropicLLM:
    def __init__(self, api_key: str, default_model: str):
        self.client = AsyncAnthropic(api_key=api_key)
        self.default_model = default_model
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    async def json(self, *, system: str, user: str, schema: type[T], images: list[bytes] | None = None,
                   model: str | None = None, max_tokens: int = 1500) -> T:
        sys_prompt = (f"{system}\n\nReply with ONE JSON object and nothing else, matching this JSON schema:\n"
                      f"{json.dumps(schema.model_json_schema())}")
        content: list[dict] = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                "data": base64.b64encode(img).decode()}} for img in images or []]
        content.append({"type": "text", "text": user})
        last: Exception | None = None
        for attempt in range(2):                      # one repair attempt on malformed output
            msgs = [{"role": "user", "content": content}]
            if attempt:
                msgs += [{"role": "assistant", "content": "{"},
                         {"role": "user", "content": f"That was invalid ({last}). Reply with only the JSON object."}]
            with tel.span("llm.call", **{"llm.model": model or self.default_model, "llm.schema": schema.__name__,
                                         "llm.attempt": attempt, "llm.images": len(images or []), "llm.prompt_chars": len(user)}) as sp:
                resp = await self.client.messages.create(model=model or self.default_model, max_tokens=max_tokens,
                                                         system=sys_prompt, messages=msgs)
                if sp is not None:
                    sp.set_attribute("llm.input_tokens", resp.usage.input_tokens)
                    sp.set_attribute("llm.output_tokens", resp.usage.output_tokens)
            self.usage["calls"] += 1
            self.usage["input_tokens"] += resp.usage.input_tokens
            self.usage["output_tokens"] += resp.usage.output_tokens
            tel.add_usage(resp.usage.input_tokens, resp.usage.output_tokens)
            try:
                return schema.model_validate_json(extract_json("".join(b.text for b in resp.content if b.type == "text")))
            except (LLMError, ValidationError, ValueError) as e:
                last = e
        raise LLMError(f"model did not return valid {schema.__name__}: {last}")


_REASONING = re.compile(r"^(gpt-[5-9]|o\d)")       # models that think before answering (gpt-5, gpt-6, o3...): a larger output budget, and reasoning_effort


class OpenAILLM:
    """OpenAI chat completions, parsed into the same Pydantic schemas as AnthropicLLM. JSON mode, one repair attempt, images as data URLs."""

    def __init__(self, api_key: str, default_model: str, *, reasoning_effort: str | None = "low", client=None):
        if client is None:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=api_key)
        self.client, self.default_model, self.reasoning_effort = client, default_model, reasoning_effort
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    async def json(self, *, system: str, user: str, schema: type[T], images: list[bytes] | None = None,
                   model: str | None = None, max_tokens: int = 1500) -> T:
        from app.security.secrets import redact
        name = model or self.default_model
        thinks = bool(_REASONING.match(name))
        sys_prompt = (f"{system}\n\nReply with ONE JSON object and nothing else, matching this JSON schema:\n"
                      f"{json.dumps(schema.model_json_schema())}")
        content: list[dict] | str = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(img).decode()}} for img in images]
        last: Exception | None = None
        for attempt in range(2):                      # one repair attempt on malformed output
            msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": content}]
            if attempt:
                msgs.append({"role": "user", "content": f"That was invalid ({last}). Reply with only the JSON object."})
            args: dict = {"model": name, "messages": msgs, "response_format": {"type": "json_object"},
                          "max_completion_tokens": max_tokens + (3000 if thinks else 0)}      # a thinking model's reasoning counts against it
            if thinks and self.reasoning_effort:
                args["reasoning_effort"] = self.reasoning_effort
            with tel.span("llm.call", **{"llm.model": name, "llm.schema": schema.__name__, "llm.attempt": attempt,
                                         "llm.images": len(images or []), "llm.prompt_chars": len(user)}) as sp:
                try:
                    resp = await self.client.chat.completions.create(**args)
                except Exception as e:                # auth, quota, rate limit, network: a short plain reason, never a key
                    detail = getattr(e, "message", None) or str(e)
                    raise LLMError(redact(f"the model call failed ({type(e).__name__}): {detail}")[:300]) from None
                u = getattr(resp, "usage", None)
                pt, ct = (getattr(u, "prompt_tokens", 0) or 0), (getattr(u, "completion_tokens", 0) or 0)
                if sp is not None:
                    sp.set_attribute("llm.input_tokens", pt)
                    sp.set_attribute("llm.output_tokens", ct)
            self.usage["calls"] += 1
            self.usage["input_tokens"] += pt
            self.usage["output_tokens"] += ct
            tel.add_usage(pt, ct)
            try:
                return schema.model_validate_json(extract_json(resp.choices[0].message.content or ""))
            except (LLMError, ValidationError, ValueError, IndexError, AttributeError) as e:
                last = e
        raise LLMError(f"model did not return valid {schema.__name__}: {last}")


class OpenRouterLLM:
    """OpenAI-compatible chat call to OpenRouter, parsed into the same Pydantic schemas. Text only (no images)."""

    def __init__(self, api_key: str, model: str, *, base_url: str = "https://openrouter.ai/api/v1", client=None):
        import httpx
        self.model, self.base_url = model, base_url
        self.client = client or httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {api_key}"})
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    async def json(self, *, system: str, user: str, schema: type[T], images: list[bytes] | None = None,
                   model: str | None = None, max_tokens: int = 1500) -> T:
        if images:
            raise LLMError("free route is text-only")
        sys_prompt = (f"{system}\n\nReply with ONE JSON object and nothing else, matching this JSON schema:\n"
                      f"{json.dumps(schema.model_json_schema())}")
        last: Exception | None = None
        for attempt in range(2):
            msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
            if attempt:
                msgs.append({"role": "user", "content": f"That was invalid ({last}). Reply with only the JSON object."})
            with tel.span("llm.call", **{"llm.model": model or self.model, "llm.schema": schema.__name__, "llm.route": "free",
                                         "llm.attempt": attempt, "llm.prompt_chars": len(user)}):
                r = await self.client.post(f"{self.base_url}/chat/completions",
                                           json={"model": model or self.model, "messages": msgs, "max_tokens": max_tokens})
            if r.status_code != 200:
                raise LLMError(f"free model returned HTTP {r.status_code}")
            body = r.json()
            self.usage["calls"] += 1
            u = body.get("usage") or {}
            self.usage["input_tokens"] += u.get("prompt_tokens", 0)
            self.usage["output_tokens"] += u.get("completion_tokens", 0)
            tel.add_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
            try:
                return schema.model_validate_json(extract_json(body["choices"][0]["message"]["content"] or ""))
            except (LLMError, ValidationError, ValueError, KeyError, IndexError) as e:
                last = e
        raise LLMError(f"free model did not return valid {schema.__name__}: {last}")


# Only calls whose whole input is the user's own words may leave for a free model that may log prompts.
FREE_ROUTE_SCHEMAS = frozenset({"Intent"})


class RoutedLLM:
    """Cheap model for user-text-only jobs, Claude for everything that sees page-derived content. Falls back to Claude on any failure."""

    def __init__(self, primary: "LLM", free: "LLM | None"):
        self.primary, self.free = primary, free
        self.usage = primary.usage
        self.routed = {"free": 0, "primary": 0, "fallback": 0}

    async def json(self, *, system: str, user: str, schema: type[T], images: list[bytes] | None = None,
                   model: str | None = None, max_tokens: int = 1500) -> T:
        from app.security.secrets import contains_secret
        if (self.free is not None and schema.__name__ in FREE_ROUTE_SCHEMAS and not images and not contains_secret(user)):
            try:
                out = await self.free.json(system=system, user=user, schema=schema, max_tokens=max_tokens)
                self.routed["free"] += 1
                return out
            except Exception:                                  # 429, 5xx, bad JSON, network: quietly use the reliable model
                self.routed["fallback"] += 1
        self.routed["primary"] += 1
        return await self.primary.json(system=system, user=user, schema=schema, images=images, model=model, max_tokens=max_tokens)
