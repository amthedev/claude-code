from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _load_dotenv() -> None:
    if _bool_env("GATEWAY_SKIP_DOTENV", False):
        return

    path = Path(os.getenv("ENV_FILE", ".env"))
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(slots=True)
class Settings:
    gateway_api_keys: tuple[str, ...] = ("local-dev-token",)
    allow_unauthenticated: bool = False
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_helper_model: str = "gpt-5.5"
    openai_helper_max_output_tokens: int = 900
    openai_helper_reasoning_effort: str = "low"
    openai_helper_for_customers: bool = False
    request_timeout_seconds: float = 120.0
    enable_agent_orchestration: bool = True
    max_cost_ratio_vs_claude: float = 0.50
    allow_premium_fallback: bool = False
    allow_direct_external_models: bool = False
    max_request_input_chars: int = 120_000
    max_request_output_tokens: int = 16_000
    tool_request_output_tokens: int = 16_000
    customer_accounts: str = ""
    quota_data_file: str = "data/gateway.sqlite3"
    account_data_file: str = "data/gateway.sqlite3"
    customer_profit_margin: float = 0.50
    usd_to_brl: float = 5.50
    cost_reserve_multiplier: float = 2.0
    admin_username: str = "reidelas"
    admin_password: str = ""
    admin_password_hash: str = ""
    rate_limit_window_seconds: int = 60
    auth_rate_limit: int = 10
    api_rate_limit: int = 120
    trusted_hosts: tuple[str, ...] = ("*",)
    admin_trusted_ips: tuple[str, ...] = ()
    trust_proxy_headers: bool = False
    cors_allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8787", "http://localhost:8787")

    economy_public_model: str = "claude-code-economy"
    pro_public_model: str = "claude-code-pro"
    ultra_public_model: str = "claude-code-ultra"
    ui_public_model: str = "claude-code-ui"
    auto_public_model: str = "claude-code-auto"

    router_agent: str = "tencent/hy3-preview"
    cheap_code_agent: str = "deepseek/deepseek-v4-flash"
    code_agent: str = "qwen/qwen3-coder-flash"
    reasoning_agent: str = "deepseek/deepseek-v4-pro"
    ui_agent: str = "moonshotai/kimi-k2.6"
    fast_agent: str = "deepseek/deepseek-v4-flash"
    premium_fallback: str = "moonshotai/kimi-k2.6"
    ultra_fallback: str = "qwen/qwen3.6-flash"

    openrouter_site_url: str = "http://localhost:8787"
    openrouter_app_name: str = "Claude Code"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        return cls(
            gateway_api_keys=_csv_env("GATEWAY_API_KEYS", "local-dev-token"),
            allow_unauthenticated=_bool_env("ALLOW_UNAUTHENTICATED", False),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_helper_model=os.getenv("OPENAI_HELPER_MODEL", "gpt-5.5"),
            openai_helper_max_output_tokens=int(os.getenv("OPENAI_HELPER_MAX_OUTPUT_TOKENS", "900")),
            openai_helper_reasoning_effort=os.getenv("OPENAI_HELPER_REASONING_EFFORT", "low"),
            openai_helper_for_customers=_bool_env("OPENAI_HELPER_FOR_CUSTOMERS", False),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
            enable_agent_orchestration=_bool_env("ENABLE_AGENT_ORCHESTRATION", True),
            max_cost_ratio_vs_claude=float(os.getenv("MAX_COST_RATIO_VS_CLAUDE", "0.50")),
            allow_premium_fallback=_bool_env("ALLOW_PREMIUM_FALLBACK", False),
            allow_direct_external_models=_bool_env("ALLOW_DIRECT_EXTERNAL_MODELS", False),
            max_request_input_chars=int(os.getenv("MAX_REQUEST_INPUT_CHARS", "120000")),
            max_request_output_tokens=int(os.getenv("MAX_REQUEST_OUTPUT_TOKENS", "16000")),
            tool_request_output_tokens=int(os.getenv("TOOL_REQUEST_OUTPUT_TOKENS", "16000")),
            customer_accounts=os.getenv("CUSTOMER_ACCOUNTS", ""),
            quota_data_file=os.getenv("QUOTA_DATA_FILE", "data/gateway.sqlite3"),
            account_data_file=os.getenv("ACCOUNT_DATA_FILE", "data/gateway.sqlite3"),
            customer_profit_margin=float(os.getenv("CUSTOMER_PROFIT_MARGIN", "0.50")),
            usd_to_brl=float(os.getenv("USD_TO_BRL", "5.50")),
            cost_reserve_multiplier=float(os.getenv("COST_RESERVE_MULTIPLIER", "2.0")),
            admin_username=os.getenv("ADMIN_USERNAME", "reidelas"),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            admin_password_hash=os.getenv("ADMIN_PASSWORD_HASH", ""),
            rate_limit_window_seconds=int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
            auth_rate_limit=int(os.getenv("AUTH_RATE_LIMIT", "10")),
            api_rate_limit=int(os.getenv("API_RATE_LIMIT", "120")),
            trusted_hosts=_csv_env("TRUSTED_HOSTS", "*"),
            admin_trusted_ips=_csv_env("ADMIN_TRUSTED_IPS", ""),
            trust_proxy_headers=_bool_env("TRUST_PROXY_HEADERS", False),
            cors_allowed_origins=_csv_env(
                "CORS_ALLOWED_ORIGINS",
                "http://127.0.0.1:8787,http://localhost:8787",
            ),
            economy_public_model=os.getenv("ECONOMY_PUBLIC_MODEL", "claude-code-economy"),
            pro_public_model=os.getenv("PRO_PUBLIC_MODEL", "claude-code-pro"),
            ultra_public_model=os.getenv("ULTRA_PUBLIC_MODEL", "claude-code-ultra"),
            ui_public_model=os.getenv("UI_PUBLIC_MODEL", "claude-code-ui"),
            auto_public_model=os.getenv("AUTO_PUBLIC_MODEL", "claude-code-auto"),
            router_agent=os.getenv("ROUTER_AGENT", "tencent/hy3-preview"),
            cheap_code_agent=os.getenv("CHEAP_CODE_AGENT", "deepseek/deepseek-v4-flash"),
            code_agent=os.getenv("CODE_AGENT", "qwen/qwen3-coder-flash"),
            reasoning_agent=os.getenv("REASONING_AGENT", "deepseek/deepseek-v4-pro"),
            ui_agent=os.getenv("UI_AGENT", "moonshotai/kimi-k2.6"),
            fast_agent=os.getenv("FAST_AGENT", "deepseek/deepseek-v4-flash"),
            premium_fallback=os.getenv("PREMIUM_FALLBACK", "moonshotai/kimi-k2.6"),
            ultra_fallback=os.getenv("ULTRA_FALLBACK", "qwen/qwen3.6-flash"),
            openrouter_site_url=os.getenv("OPENROUTER_SITE_URL", "http://localhost:8787"),
            openrouter_app_name=os.getenv(
                "OPENROUTER_APP_NAME",
                "Claude Code",
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
