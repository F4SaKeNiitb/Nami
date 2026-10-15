"""Runtime settings. Every value can be overridden with a NAMI_* environment variable."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAMI_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///data/nami.db"
    # Redis is optional. Without it, every cache is skipped and short-term memory reads from SQLite.
    redis_url: str | None = None
    redis_prefix: str = "nami"

    # Short-term memory (per thread / channel)
    stm_window_turns: int = 20          # turns kept verbatim in the model context
    stm_fold_batch: int = 10            # fold this many older turns into the summary at a time
    stm_channel_window: int = 30        # recent messages across a channel
    stm_redis_ttl_s: int = 7 * 24 * 3600

    # Long-term memory
    ltm_top_k: int = 8
    ltm_dedupe_similarity: float = 0.95
    ltm_recency_half_life_days: float = 30.0
    ltm_min_score: float = 0.25
    ltm_task_top_k: int = 5             # memories handed to the intent and planner steps per task

    # Caching
    cache_ttl_s: int = 24 * 3600
    rules_cache_ttl_s: int = 3600
    # Calibrated on bge-small: different intents ("show"/"cancel" vs "repeat my order") reach
    # 0.927, true paraphrases 0.93–0.99. 0.95 trades paraphrase recall for zero false hits.
    semantic_cache_threshold: float = 0.95
    semantic_cache_ttl_s: int = 7 * 24 * 3600
    # vectorset (any Redis 8) | search (needs Redis Query Engine) | memory
    semantic_cache_backend: str = "vectorset"
    semantic_cache_max_entries: int = 500  # per user/purpose partition

    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # Models. Anything that sees a web page uses `model_main`; user-text-only jobs use `model_fast`.
    # Provider: "auto" = OpenAI when an OpenAI key is set, otherwise Anthropic. Or force one: "openai" | "anthropic".
    llm_provider: str = "auto"
    openai_api_key: str | None = None
    # Used when the provider is OpenAI and the model_* names below are not set explicitly. Measured on the cloud VM (one task): gpt-5 and gpt-5-mini both
    # finished in 2 agent steps where gpt-4.1-mini took 3, at about the same time per step, because the browser work, not the model, dominates a step.
    # main = anything that sees a web page (the browser agent, the planner, the checks). gpt-6-luna (a thinking model: it rejects temperature != 1 and
    # frequency_penalty, and takes reasoning_effort none|low|medium|high|xhigh, not "minimal"; the code handles all of that for gpt-5 and later).
    openai_model_main: str = "gpt-6-luna"
    openai_model_fast: str = "gpt-6-luna"   # user-text-only jobs (reading the request): the decision that picks the sites and the route. Measured: gpt-4.1 ~1 s but missed
    #                                         the site once on a plain request; gpt-5-mini 2-6 s; gpt-5 2-13 s (too slow for a step every task pays for)
    openai_model_strong: str = "gpt-5"      # rare, high-stakes judgements (a refusal's second opinion)
    openai_reasoning_effort: str | None = "low"      # only sent to reasoning models (gpt-5*, o-series); ignored by the others
    anthropic_api_key: str | None = None
    model_main: str = "claude-haiku-4-5-20251001"
    model_strong: str = "claude-sonnet-5-5"          # rare, high-stakes judgements only (a refusal's second opinion); never used per step
    model_fast: str = "claude-haiku-4-5-20251001"     # point at a cheaper/free model via OpenRouter later
    openrouter_api_key: str | None = None

    # Browser
    local_browser: str = "auto"              # auto | chrome | playwright: real Chrome is what Google sign-in accepts
    chrome_path: str | None = None
    browser_provider: str = "local"           # local | kernel | browser_use
    browser_use_api_key: str | None = None
    browser_use_proxy_country: str | None = "in"   # the vendor proxy's country; empty turns the proxy off
    max_tasks_per_user: int = 3               # tasks one person can have running at once (each has its own browser; the 1 GB VM is the limit)
    kernel_api_key: str | None = None
    kernel_region: str | None = None          # where Kernel starts browsers: us-east | eu-west | ap-southeast (Singapore, nearest to India). Needs a paid Kernel plan; None = Kernel's default
    kernel_headless: bool = True              # Kernel browsers are headless (cheaper; a request for a visible one is ignored). Caveat seen live: some sites (Swiggy, Blinkit) refuse Kernel's headless browser; NAMI_KERNEL_HEADLESS=false makes them headful, which loaded fine
    browser_idle_seconds: int = 600           # a browser nobody is using (task paused, or finished work waiting) is closed after this long; resuming reopens it on the page it was on
    # Slack (optional): both tokens present turns the bot on. Socket Mode, so no public URL is needed for Slack to reach us.
    slack_bot_token: str | None = None        # xoxb-...
    slack_app_token: str | None = None        # xapp-... with connections:write
    profiles_dir: str = "data/profiles"
    headless: bool = True
    public_url: str | None = None             # e.g. https://nami.example.com; used for the Connect Google callback URL
    takeover_view: str = "inpage"            # inpage: sign-ins happen inside the chat page (headless browser, streamed);
                                              # window: open a visible browser window on the server machine (old behaviour)
    subgoal_max_steps: int = 20
    subgoal_timeout_seconds: int = 360        # hard wall-clock limit for one executor run. Was 180: with 10-20 s browser steps that cut off 14 of 40 retried runs
    task_max_steps: int = 60                  # browser steps across ALL subgoals, retries and replans of one task
    task_max_seconds: int = 1200              # time spent WORKING on one task; time spent waiting for the user (a code, an approval) is not counted
    explore: str = "auto"                     # auto: read-only tasks that need no login/approval use the pi harness (if installed); off: never
    explore_max_pages: int = 12               # pages the harness may open for one task
    explore_timeout_seconds: int = 240
    ask_credentials_max_retries: int = 2      # "I've added it" with nothing usable: how many times to re-ask before stopping
    otp_code_ttl_seconds: int = 300           # a code the user typed in chat is held in memory this long, used once
    health_ttl_seconds: int = 300             # a passing sign-in check on a saved session is trusted this long (0 = check every task)
    otp_ask_max: int = 3                      # how many times one task may ask for a verification code
    agent_ask_max: int = 2                    # questions the browser agent may put to the user in one task (ask_user tool)
    agent_confirm_max: int = 2                # extra approvals the agent may request on a step the planner did not tag (confirm_action)
    mask_tools: bool = True                   # offer the browser agent only the actions a subgoal needs
    classifier_enabled: bool = False          # L3: a model also reads page text for injection (extra model call per page; off by default)
    classifier_threshold: float = 0.6         # score at or above this counts as an injection
    classifier_timeout_seconds: float = 8.0

    # Vault keys. dev: a base64 32-byte KEK in NAMI_LOCAL_KEK. prod: Azure Key Vault.
    kms_provider: str = "local"               # local | azure
    local_kek: str | None = None
    azure_key_vault_url: str | None = None
    azure_kek_name: str = "nami-kek"

    # Auth. Production is Google-only. dev_auth adds ONE local user for running without Google.
    dev_auth: bool = False
    price_in_per_mtok: float = 2.0            # USD per million input tokens, for the /admin cost estimate only
    price_out_per_mtok: float = 10.0
    free_model: str = ""                      # OpenRouter model id for user-text-only jobs; empty = always use Claude
    otel_endpoint: str = ""                   # OTLP/HTTP traces endpoint (LangSmith or a collector); empty = spans stay in-process
    otel_headers: str = ""                    # "k=v,k2=v2"; never logged
    skills_freeze: bool = False               # NAMI_SKILLS_FREEZE=1: no skill may be created or approved (production hardening)
    admin_emails: str = ""                    # comma separated; empty = nobody can open /admin/metrics
    allowed_email_domains: str = ""           # comma separated; empty = any Google account
    generated_ttl_hours: float = 72           # generated images/audio under data/generated are deleted after this long; 0 = keep forever

    @property
    def provider(self) -> str:
        """The model provider in use: what was asked for, else OpenAI if it has a key, else Anthropic."""
        if self.llm_provider in ("openai", "anthropic"):
            return self.llm_provider
        return "openai" if self.openai_api_key else "anthropic"

    @property
    def llm_api_key(self) -> str | None:
        return self.openai_api_key if self.provider == "openai" else self.anthropic_api_key

    @model_validator(mode="after")
    def _models_for_the_provider(self):
        if self.provider == "openai":                      # explicit NAMI_MODEL_* values always win
            for field, default in (("model_main", self.openai_model_main), ("model_fast", self.openai_model_fast),
                                   ("model_strong", self.openai_model_strong)):
                if field not in self.model_fields_set:
                    setattr(self, field, default)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
