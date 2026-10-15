"""Server-side API calls with a saved API key. No browser, no model sees the key.

Two kinds of endpoint, both declared in the site registry:
  * read (GET): runs without asking;
  * generate (POST): may spend the user's credits, so the graph only gets here after an approval card, and `generate()` checks
    again. The model supplies only the declared params (validated here: type, length, pattern, allowed values); the body is
    filled from the registry's template, never written by the model.

Boundaries:
  * the model only chooses an endpoint NAME from the site's fixed list; the URL is built here from the registry;
  * HTTPS only, the host must equal the registry's host AND the credential's origin binding must allow it;
  * redirects are not followed (a redirect to another host would carry the key there);
  * every request made with the key is logged (host, method, path, status; no query, no body) beside the decrypt itself;
  * whether an endpoint needs approval is derived from its HTTP method (app/sites/policy.py), not trusted from its label;
  * the key travels only in the way the site's scheme says (app/browser/api_auth.py: bearer, a named header, basic auth or a URL
    parameter), to that one host; the response is redacted by value and by pattern,
    capped in size, and later passes the prompt-injection gateway like any page text (API descriptions are untrusted).
"""

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.agent.schemas import AccessPlan, CredentialUse
from app.browser import api_auth
from app.browser.api_auth import Scheme, from_registry, try_parse
from app.browser.broker import Broker
from app.security.secrets import redact
from app.sites.loader import hints_for
from app.sites.policy import needs_approval
from app.vault.service import VaultError

log = logging.getLogger("nami.api")
for _noisy in ("httpx", "httpcore"):         # httpx logs every request URL at INFO; a key sent as a URL parameter must not reach a log
    logging.getLogger(_noisy).setLevel(logging.WARNING)
MAX_BYTES = 60_000
MAX_MEDIA_BYTES = 25_000_000
GEN_TIMEOUT = 90                         # one generation request (a job's later polls are short)
_JOB_ID = re.compile(r"[A-Za-z0-9_\-]{1,100}")
_IMAGE_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"RIFF": "webp", b"GIF8": "gif"}
_AUDIO_TYPES = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg",
                "audio/flac": "flac", "audio/aac": "aac", "audio/mp4": "m4a"}


class ApiError(Exception):
    """A reason safe to show the user (never contains the key or response body)."""


@dataclass
class ApiResult:
    url: str
    status: int
    text: str                    # redacted, capped, JSON-ish


@dataclass
class Media:
    kind: str                    # image | video | audio
    path: str | None = None      # a file we saved (images, audio)
    url: str | None = None       # an https link the API returned (video); shown as a link, never fetched


@dataclass
class GenResult:
    url: str
    text: str                    # what was produced, as plain text (generated text, or a one-line description); redacted
    media: list[Media] = field(default_factory=list)


def _dig(obj, path: str):
    """obj['choices'][0]['message']['content'] for 'choices.0.message.content'; None if any step is missing."""
    for part in path.split("."):
        if isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        elif isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def _fill(template, values: dict):
    """Copy the registry's body template, replacing a value that is exactly "{name}"; an unset optional param drops its key."""
    if isinstance(template, dict):
        out = {}
        for k, v in template.items():
            f = _fill(v, values)
            if f is not _DROP:
                out[k] = f
        return out
    if isinstance(template, list):
        return [f for f in (_fill(v, values) for v in template) if f is not _DROP]
    if isinstance(template, str) and re.fullmatch(r"\{[a-z_]+\}", template):
        return values.get(template[1:-1], _DROP)
    return template


_DROP = object()


def check_params(spec: dict, given: dict) -> dict:
    """Validate what the model filled in against the registry's declaration. Unknown names are dropped, bad values refused."""
    out: dict = {}
    for name, rule in (spec.get("params") or {}).items():
        raw = given.get(name)
        if raw in (None, ""):
            if rule.get("default") is not None:
                out[name] = rule["default"]
            elif rule.get("required"):
                raise ApiError(f"I need {rule.get('desc') or name} for this ({name})")
            continue
        if rule.get("type") == "int":
            try:
                val = int(str(raw).strip().split()[0])
            except (ValueError, IndexError):
                raise ApiError(f"{name} must be a whole number") from None
            if not rule.get("min", 0) <= val <= rule.get("max", 10**6):
                raise ApiError(f"{name} must be between {rule.get('min', 0)} and {rule.get('max', 10**6)}")
            out[name] = val
            continue
        val = str(raw).strip()
        if len(val) > rule.get("max_len", 200):
            raise ApiError(f"{name} is too long (limit {rule.get('max_len', 200)} characters)")
        if rule.get("enum") and val not in rule["enum"]:
            raise ApiError(f"{name} must be one of {', '.join(rule['enum'])}")
        if rule.get("pattern") and not re.fullmatch(rule["pattern"], val):
            raise ApiError(f"{name} is not a valid value")
        out[name] = val
    return out


class ApiExecutor:
    def __init__(self, client: httpx.AsyncClient | None = None, out_dir: Path | str = "data/generated"):
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self.out_dir = Path(out_dir)

    def _sender(self, broker: Broker, credential_id: str, key: str, scheme: Scheme, learned: bool = False):
        """A request function bound to one key and one way of sending it: every request it makes is audited, whether or not a
        response came back. A scheme we worked out ourselves is forgotten when the site then rejects the key with a 401, so a
        wrong guess (or a key rotated to another style) is worked out again next time instead of failing forever."""
        async def send(method: str, url: str, **kw) -> httpx.Response:
            status = None
            headers, qs = scheme.apply(key)
            if qs:
                kw["params"] = {**(kw.get("params") or {}), **qs}
            try:
                r = await self.client.request(method, url, headers=headers, **kw)
                status = r.status_code
                return r
            finally:
                u = urlparse(url)
                await broker.audit_request(credential_id, u.hostname or "", method, u.path, status)
                if learned and status == 401:
                    await broker.remember_auth_scheme(credential_id, None)
        return send

    @staticmethod
    def scheme_for(cu: CredentialUse, api: dict) -> tuple[Scheme | None, bool]:
        """(how to send this key, whether we worked it out ourselves). A remembered scheme wins over the registry's own."""
        mine = try_parse(cu.auth_scheme) if cu.auth_scheme else None
        if mine is not None:
            return mine, True
        try:
            return from_registry(api), False
        except ValueError:
            return None, False

    @staticmethod
    def needs_discovery(access: AccessPlan, site: str) -> bool:
        """An API key is saved for a site whose way of taking a key we have neither been told nor learned."""
        api = hints_for(site).api
        cu = next((c for c in access.credentials if c.kind == "api_key"), None)
        return bool(api) and cu is not None and ApiExecutor.scheme_for(cu, api)[0] is None

    async def discover(self, *, broker: Broker, access: AccessPlan, site: str, candidates: list[Scheme], approved: bool) -> Scheme:
        """Try the candidate ways of sending the key on ONE harmless read of the site's API, stop at the first that is accepted,
        and remember it. Only the registry's host ever sees the key. A 401/403 means "wrong way, try the next"; anything else
        (a redirect, a 404, a 5xx, no connection) means we can't tell, so we stop rather than keep sending the key."""
        if not approved:
            raise ApiError("I won't try your key on the site without your approval")
        api = hints_for(site).api
        cu = next((c for c in access.credentials if c.kind == "api_key"), None)
        probe = next((spec for spec in (api or {}).get("endpoints", {}).values()
                      if not needs_approval(spec) and spec.get("method", "GET").upper() == "GET"
                      and not any(p.get("required") for p in (spec.get("params") or {}).values())), None)
        if not api or cu is None or probe is None:
            raise ApiError("there is nothing safe to test the key on for this site")
        url = api["base"].rstrip("/") + probe["path"]
        host = urlparse(url).hostname
        if urlparse(url).scheme != "https" and host not in ("localhost", "127.0.0.1"):
            raise ApiError("the API address is not allowed")
        try:
            secret = await broker.vault_use_api_key(cu.credential_id, host)
        except VaultError as e:
            raise ApiError(f"I couldn't use the saved key ({e})") from None
        key = secret["key"]
        try:
            for scheme in candidates[:api_auth.MAX_TRIES]:
                try:
                    r = await self._sender(broker, cu.credential_id, key, scheme)("GET", url, timeout=20)
                except httpx.HTTPError as e:
                    raise ApiError(f"the API could not be reached ({type(e).__name__})") from None
                if 200 <= r.status_code < 300:
                    await broker.remember_auth_scheme(cu.credential_id, scheme.to_dict())
                    return scheme
                if r.status_code not in (401, 403):
                    raise ApiError(f"I couldn't tell how {host} takes a key (it answered {r.status_code}), so I stopped")
        finally:
            del secret
        raise ApiError(f"{host} refused your key every way I tried it. It may be revoked, mistyped, or for a different service")

    @staticmethod
    def endpoints(site: str) -> dict[str, dict]:
        return (hints_for(site).api or {}).get("endpoints", {})

    async def call(self, *, broker: Broker, access: AccessPlan, site: str, endpoint: str) -> ApiResult:
        api = hints_for(site).api
        spec = (api or {}).get("endpoints", {}).get(endpoint)
        if not api or spec is None or needs_approval(spec) or spec.get("method", "GET").upper() != "GET":
            raise ApiError("that is not something I can do through the API")      # generate endpoints only run via generate()
        url = api["base"].rstrip("/") + spec["path"]
        parsed = urlparse(url)
        loopback = parsed.hostname in ("localhost", "127.0.0.1")            # plain http only for local testing
        if parsed.scheme not in ("https", "http") or (parsed.scheme == "http" and not loopback) \
                or parsed.hostname != urlparse(api["base"]).hostname:
            raise ApiError("the API address is not allowed")
        cu = next((c for c in access.credentials if c.kind == "api_key"), None)
        if cu is None:
            raise ApiError("no API key is available for this site")
        try:
            secret = await broker.vault_use_api_key(cu.credential_id, parsed.hostname)
        except VaultError as e:
            raise ApiError(f"I couldn't use the saved key ({e})") from None
        key = secret["key"]
        scheme, learned = self.scheme_for(cu, api)
        if scheme is None:
            raise ApiError("I haven't worked out how this site takes your key yet")
        try:
            r = await self._sender(broker, cu.credential_id, key, scheme, learned)("GET", url)
        except httpx.HTTPError as e:
            raise ApiError(f"the API could not be reached ({type(e).__name__})") from None
        finally:
            del secret
        if 300 <= r.status_code < 400:
            raise ApiError("the API tried to redirect me elsewhere, so I stopped")
        if r.status_code in (401, 403):
            raise ApiError("the site rejected the saved key (it may be revoked or expired)")
        if r.status_code >= 400:
            raise ApiError(f"the API returned an error ({r.status_code})")
        body = r.text[:MAX_BYTES]
        try:                                                   # normalise so a huge or odd payload becomes plain, bounded text
            body = json.dumps(json.loads(body), indent=1, ensure_ascii=True)[:MAX_BYTES]
        except ValueError:
            pass
        body = body.replace(key, "[redacted]")
        return ApiResult(url=url, status=r.status_code, text=redact(body))

    # ───────────── generation (POST, may spend credits) ─────────────

    def _save(self, user_id: str, data: bytes, ext: str) -> str:
        d = self.out_dir / re.sub(r"[^A-Za-z0-9_-]", "", user_id)[:64]
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{uuid.uuid4().hex}.{ext}"
        path.write_bytes(data)
        return str(path)

    async def _poll(self, send, api: dict, job: dict, jid: str):
        if not _JOB_ID.fullmatch(jid):
            raise ApiError("the API returned a job id I don't accept")
        url = api["base"].rstrip("/") + job["poll_path"].replace("{id}", jid)
        if urlparse(url).hostname != urlparse(api["base"]).hostname:
            raise ApiError("the API address is not allowed")
        deadline = time.monotonic() + job.get("timeout", 600)
        while time.monotonic() < deadline:
            await asyncio.sleep(job.get("interval", 10))
            r = await send("GET", url)
            self._check(r)
            try:
                data = r.json()
            except ValueError:
                raise ApiError("the API sent a reply I couldn't read") from None
            status = str(_dig(data, job["status_path"]) or "").lower()
            if status in job["done"]:
                return data
            if status in job["failed"]:
                raise ApiError(f"the job ended with status '{status}'")
        raise ApiError("the job is still running after the time limit; check the site for the result")

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if 300 <= r.status_code < 400:
            raise ApiError("the API tried to redirect me elsewhere, so I stopped")
        if r.status_code == 401:
            raise ApiError("the site rejected the saved key (it may be revoked or expired)")
        if r.status_code == 403:       # the key was understood but this request was refused (a gated model, a policy): say why
            raise ApiError("the site refused this request" + ApiExecutor._why(r))
        if r.status_code == 402:
            raise ApiError("the account is out of credits")
        if r.status_code == 429:
            raise ApiError("the API is rate limiting this key; try again in a moment")
        if r.status_code >= 400:
            raise ApiError(f"the API returned an error ({r.status_code})" + ApiExecutor._why(r))

    @staticmethod
    def _why(r: httpx.Response) -> str:
        """The provider's own short reason, for the user only: bounded, redacted, never sent to a model."""
        try:
            e = r.json().get("error")
            msg = e.get("message") if isinstance(e, dict) else e
        except (ValueError, AttributeError):
            return ""
        msg = redact(" ".join(str(msg or "").split()))[:200]
        return f": {msg}" if msg else ""

    @staticmethod
    def rank_models(data: dict, wanted: str | None = None, free: bool = False, modality: str = "text") -> list[dict]:
        """Candidate models from a /models reply, best first: [{"id", "name", "free"}]. Done in code from the site's own
        list, so the language model never names a model id. `wanted` is whatever the user typed ("Nemotron",
        "claude sonnet"): every word of it must appear in the model's id or name; an exact id wins; then the newest.
        `free` keeps only models that cost nothing for input and output. Nothing matches -> empty (never a guess)."""
        words = lambda t: set(re.findall(r"[a-z0-9]+", str(t).lower()))          # noqa: E731
        want = words(wanted or "")
        ranked: list[tuple[tuple, dict]] = []
        for m in (data.get("data") or []) if isinstance(data, dict) else []:
            if not isinstance(m, dict):
                continue
            mid, name = str(m.get("id", "")), str(m.get("name", ""))
            pr = m.get("pricing") or {}
            outs = (m.get("architecture") or {}).get("output_modalities") or ["text"]
            if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", mid) or modality not in outs:
                continue
            try:
                is_free = float(pr.get("prompt", "1")) == 0 and float(pr.get("completion", "1")) == 0
            except (TypeError, ValueError):
                is_free = False
            if free and not is_free:
                continue
            if want and not want <= words(f"{mid} {name}"):
                continue
            key = (bool(wanted) and mid.lower() == str(wanted).strip().lower(),
                   0 if wanted else (1 if mid.endswith(":free") else 0),
                   int(m.get("created") or 0) if wanted else int(m.get("context_length") or 0),
                   -len(words(f"{mid} {name}")), mid)
            ranked.append((key, {"id": mid, "name": name[:80], "free": is_free}))
        return [m for _, m in sorted(ranked, key=lambda x: x[0], reverse=True)]

    @staticmethod
    def pick_free_text_model(data: dict) -> str | None:
        found = ApiExecutor.rank_models(data, None, True, "text")
        return found[0]["id"] if found else None

    async def find_models(self, *, broker: Broker, access: AccessPlan, site: str) -> dict | None:
        """The site's own model list (a read, no credits; the use is audited). None when it can't be read."""
        api = hints_for(site).api
        spec = (api or {}).get("endpoints", {}).get("models")
        cu = next((c for c in access.credentials if c.kind == "api_key"), None)
        if not api or spec is None or cu is None:
            return None
        url = api["base"].rstrip("/") + spec["path"]
        try:
            secret = await broker.vault_use_api_key(cu.credential_id, urlparse(url).hostname)
        except VaultError:
            return None
        key = secret["key"]
        scheme, learned = self.scheme_for(cu, api)
        if scheme is None:
            return None
        try:
            r = await self._sender(broker, cu.credential_id, key, scheme, learned)("GET", url, timeout=20)
            self._check(r)
            data = r.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ApiError, ValueError):
            return None
        finally:
            del secret

    async def free_text_model(self, *, broker: Broker, access: AccessPlan, site: str) -> str | None:
        data = await self.find_models(broker=broker, access=access, site=site)
        return None if data is None else self.pick_free_text_model(data)

    async def generate(self, *, broker: Broker, access: AccessPlan, site: str, endpoint: str, params: dict, user_id: str,
                       approved: bool) -> GenResult:
        """One approved generation. `approved` must be True: the graph passes what the Guard says, and this refuses without it."""
        if not approved:
            raise ApiError("I won't spend credits without your approval")
        api = hints_for(site).api
        spec = (api or {}).get("endpoints", {}).get(endpoint)
        if not api or spec is None or spec.get("kind") != "generate" or spec.get("method") != "POST":
            raise ApiError("that is not something I can generate through the API")
        url = api["base"].rstrip("/") + spec["path"]
        parsed = urlparse(url)
        loopback = parsed.hostname in ("localhost", "127.0.0.1")
        if parsed.scheme not in ("https", "http") or (parsed.scheme == "http" and not loopback) \
                or parsed.hostname != urlparse(api["base"]).hostname:
            raise ApiError("the API address is not allowed")
        values = check_params(spec, params)
        body = _fill(spec["body"], values)
        cu = next((c for c in access.credentials if c.kind == "api_key"), None)
        if cu is None:
            raise ApiError("no API key is available for this site")
        try:
            secret = await broker.vault_use_api_key(cu.credential_id, parsed.hostname)
        except VaultError as e:
            raise ApiError(f"I couldn't use the saved key ({e})") from None
        key = secret["key"]
        scheme, learned = self.scheme_for(cu, api)
        if scheme is None:
            raise ApiError("I haven't worked out how this site takes your key yet")
        send = self._sender(broker, cu.credential_id, key, scheme, learned)
        try:
            try:
                r = await send("POST", url, json=body, timeout=GEN_TIMEOUT)
                self._check(r)
                out = await self._collect(r, spec, api, send, user_id)
            except httpx.HTTPError as e:
                raise ApiError(f"the API could not be reached ({type(e).__name__})") from None
        finally:
            del secret
        out.text = redact(out.text.replace(key, "[redacted]"))
        return out

    async def _collect(self, r: httpx.Response, spec: dict, api: dict, send, user_id: str) -> GenResult:
        kind = spec["output"]
        url = api["base"].rstrip("/") + spec["path"]
        if kind == "audio":
            ext = _AUDIO_TYPES.get((r.headers.get("content-type") or "").split(";")[0].strip().lower())
            if ext is None or not r.content or len(r.content) > MAX_MEDIA_BYTES:
                raise ApiError("the API did not return audio I can accept")
            return GenResult(url, f"Generated {len(r.content) // 1024} KB of {ext} audio.", [Media("audio", path=self._save(user_id, r.content, ext))])
        try:
            data = r.json()
        except ValueError:
            raise ApiError("the API sent a reply I couldn't read") from None
        if spec.get("job"):
            jid = str(_dig(data, spec["job"]["id_path"]) or "")
            data = await self._poll(send, api, spec["job"], jid)
            found = _dig(data, spec["job"]["result_path"])
        else:
            found = _dig(data, spec.get("result_path", ""))
        if kind == "text":
            if not isinstance(found, str) or not found.strip():
                raise ApiError("the API returned no text")
            return GenResult(url, found[:6000])
        items = found if isinstance(found, list) else ([found] if found else [])
        if spec.get("item_path"):
            items = [_dig(i, spec["item_path"]) for i in items]
        media: list[Media] = []
        for item in items[:4]:
            if not isinstance(item, str):
                continue
            if kind == "image" and item.startswith("data:image/") and ";base64," in item:
                raw = base64.b64decode(item.split(";base64,", 1)[1], validate=False)
                ext = next((e for magic, e in _IMAGE_MAGIC.items() if raw.startswith(magic)), None)
                if ext is None or len(raw) > MAX_MEDIA_BYTES:
                    continue                                    # not an image we recognise: never saved
                media.append(Media("image", path=self._save(user_id, raw, ext)))
            elif item.startswith("https://") and urlparse(item).hostname:
                media.append(Media(kind, url=item))             # a link only: Nami never fetches or sends the key to it
        if not media:
            raise ApiError(f"the API finished but returned no {kind} I can show")
        return GenResult(url, f"Generated {len(media)} {kind}{'s' if len(media) > 1 else ''}.", media)
