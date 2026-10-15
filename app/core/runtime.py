"""Builds the whole running app once: storage, vault, browser provider, agent, orchestrator."""

import asyncio
import base64
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.graph import Orchestrator
from app.browser.executor import Executor
from app.browser.browser_use_provider import BrowserUseProvider
from app.browser.kernel_provider import KernelProvider
from app.slack.links import SlackLinks
from app.browser.local_provider import LocalPlaywrightProvider
from app.browser.provider import BrowserProvider
from app.browser.session_manager import SessionManager
from app.connect.google_oauth import GoogleOAuth
from app.core.config import Settings, get_settings
from app.core.container import Services, build_services
from app.core.llm import LLM, AnthropicLLM, LLMError, OpenAILLM, OpenRouterLLM, RoutedLLM
from app.security.identity import IdentityIssuer, IdentityVerifier, InMemoryReplayStore, RedisReplayStore, load_or_create_key
from app.security.identity_service import IdentityService
from app.skills.service import SkillService
from app.telemetry import tel
from app.skills.signing import Signer
from app.vault.kms import AzureKeyVaultKms, KmsProvider, LocalKms
from app.vault.service import VaultService

log = logging.getLogger("nami.runtime")


class MissingKeyLLM:
    """Lets the app boot without a model key and says so clearly the first time a task needs one."""
    usage: dict[str, int] = {}

    async def json(self, **_):
        raise LLMError("No model key configured. Set NAMI_OPENAI_API_KEY (or NAMI_ANTHROPIC_API_KEY) and restart.")


def migrate() -> None:
    """Bring the database to the latest schema (Alembic is the only thing that creates tables)."""
    from alembic import command
    from alembic.config import Config
    command.upgrade(Config("alembic.ini"), "head")


def build_kms(s: Settings) -> KmsProvider:
    if s.kms_provider == "azure":
        if not s.azure_key_vault_url:
            raise RuntimeError("NAMI_AZURE_KEY_VAULT_URL is required when NAMI_KMS_PROVIDER=azure")
        return AzureKeyVaultKms(s.azure_key_vault_url, s.azure_kek_name)
    if s.local_kek:
        return LocalKms(base64.b64decode(s.local_kek))
    path = Path("data/dev_kek.key")                       # stable across restarts so dev secrets stay readable
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(32))
        path.chmod(0o600)
        log.warning("created a dev-only key at %s; production must use Azure Key Vault", path)
    return LocalKms(path.read_bytes())


def build_provider(s: Settings) -> BrowserProvider:
    if s.browser_provider == "browser_use":
        if not s.browser_use_api_key:
            raise RuntimeError("NAMI_BROWSER_USE_API_KEY is required when NAMI_BROWSER_PROVIDER=browser_use")
        return BrowserUseProvider(api_key=s.browser_use_api_key, proxy_country=s.browser_use_proxy_country or None,
                                  timeout_seconds=s.browser_idle_seconds + 300)
    if s.browser_provider == "kernel":
        if not s.kernel_api_key:
            raise RuntimeError("NAMI_KERNEL_API_KEY is required when NAMI_BROWSER_PROVIDER=kernel")
        return KernelProvider(api_key=s.kernel_api_key, headless=s.kernel_headless, region=s.kernel_region,
                              timeout_seconds=s.browser_idle_seconds + 300)    # Kernel's own idle timeout is only a safety net: ours closes first, and closing is what saves the profile
    return LocalPlaywrightProvider(s.profiles_dir, browser=s.local_browser, chrome_path=s.chrome_path)


def build_llm(s: Settings) -> LLM:
    """The model for the agent's own reasoning (intent, plan, validate, verify), by provider. No key: a stand-in that says so on first use."""
    key = s.llm_api_key
    if not key:
        return MissingKeyLLM()
    llm: LLM = (OpenAILLM(key, s.model_main, reasoning_effort=s.openai_reasoning_effort) if s.provider == "openai"
                else AnthropicLLM(key, s.model_main))
    log.info("model provider: %s (%s)", s.provider, s.model_main)
    if s.openrouter_api_key and s.free_model:
        llm = RoutedLLM(llm, OpenRouterLLM(s.openrouter_api_key, s.free_model))
    return llm


@dataclass
class Runtime:
    settings: Settings
    services: Services
    vault: VaultService
    sessions: SessionManager
    provider: BrowserProvider
    llm: LLM
    orchestrator: Orchestrator
    identity: IdentityService
    _stack: AsyncExitStack
    slack_links: "SlackLinks | None" = None

    @property
    def live_relay(self):
        return self.orchestrator.live_relay

    async def close(self) -> None:
        from app.browser import cdp
        shutdown = getattr(self.provider, "shutdown", None)
        if shutdown:
            await shutdown()          # first: closing Chrome cleanly (flushing the profile) needs the shared CDP driver
        await cdp.shutdown()
        if self.orchestrator.harness is not None:
            await self.orchestrator.harness.tools.stop()
        tel.shutdown()
        await self._stack.aclose()
        await self.services.close()


_runtime: Runtime | None = None


async def start_runtime(settings: Settings | None = None) -> Runtime:
    global _runtime
    s = settings or get_settings()
    services = build_services(s)
    await asyncio.to_thread(migrate)
    vault = VaultService(services.db, build_kms(s))
    provider = build_provider(s)
    sessions = SessionManager(services.db, vault, provider=provider)
    key = s.llm_api_key
    llm = build_llm(s)
    executor = Executor(api_key=key or "", provider=s.provider, model=s.model_main, max_steps=s.subgoal_max_steps,
                        timeout_seconds=s.subgoal_timeout_seconds)
    stack = AsyncExitStack()
    headers = dict(kv.split("=", 1) for kv in s.otel_headers.split(",") if "=" in kv)
    tel.configure(otlp_endpoint=s.otel_endpoint or None, headers=headers)      # redaction runs before any export
    Path("data").mkdir(exist_ok=True)
    saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string("data/graph_checkpoints.db"))
    issuer = IdentityIssuer(load_or_create_key("data/identity_key.pem"))     # prod: ES256 key in Key Vault (not built)
    replay = RedisReplayStore(services.redis, s.redis_prefix) if services.redis is not None else InMemoryReplayStore()
    vault.verifier = IdentityVerifier.for_issuer(issuer, replay)
    identity = IdentityService(issuer, services.authz)
    from app.harness.pi_runner import PiHarness
    from app.harness.tool_server import ToolServer
    harness = PiHarness(ToolServer(), api_key=key, provider=s.provider, model=s.model_main, max_calls=s.explore_max_pages,
                        timeout=s.explore_timeout_seconds) if s.explore != "off" else None
    orch = Orchestrator(services=services, vault=vault, sessions=sessions, provider=provider, llm=llm,
                        executor=executor, settings=s, checkpointer=saver, identity=identity, harness=harness,
                        skills=SkillService(services.db, Signer(issuer._key), freeze=s.skills_freeze),
                        google_oauth=GoogleOAuth(client_id=os.getenv("OAUTH_GOOGLE_CLIENT_ID"),
                                                 client_secret=os.getenv("OAUTH_GOOGLE_CLIENT_SECRET"),
                                                 signing_key=os.getenv("CHAINLIT_AUTH_SECRET") or base64.b64encode(os.urandom(32)).decode()))
    n = await orch.recover_on_boot()
    if n:
        log.warning("%d task(s) were interrupted by the last shutdown; owners can resume them", n)
    _runtime = Runtime(s, services, vault, sessions, provider, llm, orch, identity, stack, slack_links=SlackLinks(services.db))
    return _runtime


def runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime not started")
    return _runtime
