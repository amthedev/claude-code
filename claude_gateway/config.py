from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is normally present via httpx
    certifi = None


DEFAULT_RUNPOD_CODER_MODEL_ID = "qwen25-coder-14b"
DEFAULT_RUNPOD_VLLM_PORT = "8001"


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


def _runpod_vllm_base_url(pod_id: str, port: str = DEFAULT_RUNPOD_VLLM_PORT) -> str:
    return f"https://{pod_id}-{port}.proxy.runpod.net/v1"


def _looks_like_gateway_url(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    if not host:
        return False
    return host.endswith(".squareweb.app") or "claude-code-api" in host


def _looks_like_runpod_proxy_url(value: str) -> bool:
    return (urlparse(value).hostname or "").lower().endswith(".proxy.runpod.net")


def _discover_active_runpod_pod_id(current_pod_id: str, port: str) -> str:
    if not _bool_env("RUNPOD_AUTO_DISCOVER_ACTIVE", True):
        return current_pod_id

    api_key = os.getenv("RUNPOD_API_KEY", "").strip()
    if not api_key:
        return current_pod_id

    query = {
        "query": (
            "query { myself { pods { id name desiredStatus runtime { ports { privatePort type } } } } }"
        )
    }
    request = Request(
        "https://api.runpod.io/graphql",
        data=json.dumps(query).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "claude-code-gateway/1.0",
        },
        method="POST",
    )
    try:
        timeout = float(os.getenv("RUNPOD_AUTO_DISCOVER_TIMEOUT_SECONDS", "4"))
        context = ssl.create_default_context(cafile=certifi.where()) if certifi else None
        with urlopen(request, timeout=timeout, context=context) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return current_pod_id

    pods = (((data.get("data") or {}).get("myself") or {}).get("pods") or [])
    if not isinstance(pods, list):
        return current_pod_id

    def pod_has_port(pod: dict[str, object]) -> bool:
        runtime = pod.get("runtime") if isinstance(pod, dict) else None
        ports = (runtime or {}).get("ports") if isinstance(runtime, dict) else []
        if not isinstance(ports, list):
            return False
        return any(str((port_info or {}).get("privatePort") or "") == str(port) for port_info in ports)

    current = next((pod for pod in pods if isinstance(pod, dict) and pod.get("id") == current_pod_id), None)
    if isinstance(current, dict) and current.get("desiredStatus") == "RUNNING" and pod_has_port(current):
        return current_pod_id

    running = [
        pod
        for pod in pods
        if isinstance(pod, dict) and pod.get("desiredStatus") == "RUNNING" and pod_has_port(pod)
    ]
    if not running:
        return current_pod_id

    current_name = str(current.get("name") or "") if isinstance(current, dict) else ""
    if current_name:
        current_base = current_name.removesuffix("-migration")
        for pod in running:
            name = str(pod.get("name") or "")
            if name == f"{current_name}-migration" or name.removesuffix("-migration") == current_base:
                return str(pod.get("id") or current_pod_id)

    for pod in running:
        name = str(pod.get("name") or "").lower()
        if "qwen" in name or "vllm" in name:
            return str(pod.get("id") or current_pod_id)
    return str(running[0].get("id") or current_pod_id)


def _model_backend_env() -> dict[str, str]:
    runpod_pod_id = os.getenv("RUNPOD_POD_ID", "").strip()
    runpod_port = os.getenv("RUNPOD_VLLM_PORT", DEFAULT_RUNPOD_VLLM_PORT).strip() or DEFAULT_RUNPOD_VLLM_PORT
    base_url = os.getenv("VPS_MODEL_BASE_URL", "http://127.0.0.1:8000").strip()
    model_id = os.getenv("VPS_MODEL_ID", "local-model").strip()
    api_format = os.getenv("VPS_MODEL_API_FORMAT", "anthropic").strip()
    fast_base_url = os.getenv("VPS_FAST_MODEL_BASE_URL", "").strip()
    fast_model_id = os.getenv("VPS_FAST_MODEL_ID", "").strip()
    fast_api_format = os.getenv("VPS_FAST_MODEL_API_FORMAT", "").strip()
    strong_base_url = os.getenv("VPS_STRONG_MODEL_BASE_URL", "").strip()
    strong_model_id = os.getenv("VPS_STRONG_MODEL_ID", "").strip()
    strong_api_format = os.getenv("VPS_STRONG_MODEL_API_FORMAT", "").strip()

    if runpod_pod_id and (_looks_like_gateway_url(base_url) or _looks_like_runpod_proxy_url(base_url)):
        runpod_pod_id = _discover_active_runpod_pod_id(runpod_pod_id, runpod_port)
        base_url = _runpod_vllm_base_url(runpod_pod_id, runpod_port)
        model_id = (
            fast_model_id
            if fast_model_id and not (_looks_like_gateway_url(fast_base_url) or _looks_like_runpod_proxy_url(fast_base_url))
            else DEFAULT_RUNPOD_CODER_MODEL_ID
        )
        api_format = "openai-chat"
        if not fast_base_url or _looks_like_gateway_url(fast_base_url) or _looks_like_runpod_proxy_url(fast_base_url):
            fast_base_url = base_url
        if not fast_model_id or fast_model_id in {"local-model", "claude-code-pro"}:
            fast_model_id = model_id
        if not fast_api_format:
            fast_api_format = api_format
        strong_base_url = ""
        strong_model_id = ""
        strong_api_format = ""

    return {
        "vps_model_base_url": base_url,
        "vps_model_id": model_id,
        "vps_model_api_format": api_format,
        "vps_fast_model_base_url": fast_base_url,
        "vps_fast_model_id": fast_model_id,
        "vps_fast_model_api_format": fast_api_format,
        "vps_strong_model_base_url": strong_base_url,
        "vps_strong_model_id": strong_model_id,
        "vps_strong_model_api_format": strong_api_format,
    }


@dataclass(slots=True)
class Settings:
    gateway_api_keys: tuple[str, ...] = ("local-dev-token",)
    allow_unauthenticated: bool = False
    allow_admin_model_access: bool = False
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api"
    openrouter_emergency_fallback: bool = False
    vps_model_base_url: str = "http://127.0.0.1:8000"
    vps_model_id: str = "local-model"
    vps_model_api_format: str = "anthropic"
    vps_model_api_key: str = ""
    vps_fast_model_base_url: str = ""
    vps_fast_model_id: str = ""
    vps_fast_model_api_format: str = ""
    vps_fast_model_api_key: str = ""
    vps_strong_model_base_url: str = ""
    vps_strong_model_id: str = ""
    vps_strong_model_api_format: str = ""
    vps_strong_model_api_key: str = ""
    vps_model_timeout_seconds: float = 55.0
    vps_code_timeout_seconds: float = 8.0
    vps_model_slow_fallback_seconds: float = 6.0
    vps_openai_chat_context_tokens: int = 24_576
    runpod_api_key: str = ""
    runpod_pod_id: str = ""
    vps_scheduler_interval_seconds: int = 60
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_helper_model: str = "gpt-5.4-mini"
    openai_helper_max_output_tokens: int = 900
    openai_helper_reasoning_effort: str = "low"
    openai_helper_for_customers: bool = False
    enable_openai_design_director: bool = True
    enable_openai_decision_director: bool = True
    enable_web_search: bool = False
    legacy_public_model_label: str = "Claude Sonnet 4.5"
    public_model_label: str = "Claude Sonnet 4.6"
    web_search_model: str = "gpt-5.5"
    web_search_openrouter_model: str = ""
    web_search_context_size: str = "low"
    web_search_for_customers: bool = True
    web_search_max_output_tokens: int = 900
    web_search_timeout_seconds: float = 8.0
    web_search_allowed_domains: tuple[str, ...] = ()
    web_search_blocked_domains: tuple[str, ...] = ()
    request_timeout_seconds: float = 120.0
    simple_request_max_output_tokens: int = 768
    vps_disable_qwen_thinking: bool = True
    enable_agent_orchestration: bool = True
    enable_stream_agent_orchestration: bool = False
    max_cost_ratio_vs_claude: float = 0.50
    allow_premium_fallback: bool = False
    allow_direct_external_models: bool = False
    max_request_input_chars: int = 120_000
    max_request_output_tokens: int = 16_000
    tool_request_output_tokens: int = 4_096
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
    api_rate_limit: int = 600
    rate_limit_token_scope: str = "token_ip"
    trusted_hosts: tuple[str, ...] = ("*",)
    admin_trusted_ips: tuple[str, ...] = ()
    trust_proxy_headers: bool = False
    cors_allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8787",
        "http://localhost:8787",
    )
    expose_openapi: bool = False
    expose_detailed_health: bool = False
    mercado_pago_access_token: str = ""
    mercado_pago_webhook_secret: str = ""
    mercado_pago_webhook_tolerance_seconds: int = 600
    mercado_pago_public_url: str = ""
    public_trial_enabled: bool = False
    public_trial_end_at: str = ""
    public_trial_plan_id: str = "ultra"
    public_trial_daily_limit: int = 1_200_000
    public_trial_label: str = "Teste grátis 24h"

    economy_public_model: str = "claude-code-pro"
    pro_public_model: str = "claude-code-pro"
    ultra_public_model: str = "claude-sonnet-4.6"
    ui_public_model: str = "claude-code-pro"
    auto_public_model: str = "claude-code-pro"

    router_agent: str = "tencent/hy3-preview"
    cheap_code_agent: str = "deepseek/deepseek-v4-flash"
    code_agent: str = "qwen/qwen3-coder-next"
    reasoning_agent: str = "deepseek/deepseek-v4-pro"
    ui_agent: str = "qwen/qwen3-coder-next"
    fast_agent: str = "deepseek/deepseek-v4-flash"
    premium_fallback: str = "deepseek/deepseek-v4-pro"
    ultra_fallback: str = "qwen/qwen3-coder-next"
    frontend_coder_agent: str = "qwen/qwen3-coder-next"
    frontend_fix_agent: str = "deepseek/deepseek-v4-flash"
    frontend_reasoning_agent: str = "tencent/hy3-preview"
    backend_partner_agent: str = "deepseek/deepseek-v4-pro"
    project_reasoning_agent: str = "deepseek/deepseek-v4-pro"
    deep_reasoning_agent: str = "deepseek/deepseek-v4-pro"
    gemini_code_helper_agent: str = "google/gemini-2.5-flash-lite"
    enable_gemini_code_helper: bool = False

    openrouter_site_url: str = "http://localhost:8787"
    openrouter_app_name: str = "Claude Code"

    def __post_init__(self) -> None:
        if self.vps_model_id and self.vps_model_id != "local-model":
            for name in (
                "economy_public_model",
                "pro_public_model",
                "ultra_public_model",
                "ui_public_model",
                "auto_public_model",
            ):
                if getattr(self, name) == "local-model":
                    setattr(self, name, "claude-code-pro")

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        model_backend = _model_backend_env()
        vps_model_id = model_backend["vps_model_id"]
        public_model_id = os.getenv("PUBLIC_MODEL_ID", "claude-code-pro")
        return cls(
            gateway_api_keys=_csv_env("GATEWAY_API_KEYS", "local-dev-token"),
            allow_unauthenticated=_bool_env("ALLOW_UNAUTHENTICATED", False),
            allow_admin_model_access=_bool_env("ALLOW_ADMIN_MODEL_ACCESS", False),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api"),
            openrouter_emergency_fallback=False,
            vps_model_base_url=model_backend["vps_model_base_url"],
            vps_model_id=vps_model_id,
            vps_model_api_format=model_backend["vps_model_api_format"],
            vps_model_api_key=os.getenv("VPS_MODEL_API_KEY", ""),
            vps_fast_model_base_url=model_backend["vps_fast_model_base_url"],
            vps_fast_model_id=model_backend["vps_fast_model_id"],
            vps_fast_model_api_format=model_backend["vps_fast_model_api_format"],
            vps_fast_model_api_key=os.getenv("VPS_FAST_MODEL_API_KEY", ""),
            vps_strong_model_base_url=model_backend["vps_strong_model_base_url"],
            vps_strong_model_id=model_backend["vps_strong_model_id"],
            vps_strong_model_api_format=model_backend["vps_strong_model_api_format"],
            vps_strong_model_api_key=os.getenv("VPS_STRONG_MODEL_API_KEY", ""),
            vps_model_timeout_seconds=float(os.getenv("VPS_MODEL_TIMEOUT_SECONDS", "55")),
            vps_code_timeout_seconds=float(os.getenv("VPS_CODE_TIMEOUT_SECONDS", "8")),
            vps_model_slow_fallback_seconds=float(
                os.getenv("VPS_MODEL_SLOW_FALLBACK_SECONDS", "6")
            ),
            vps_openai_chat_context_tokens=int(os.getenv("VPS_OPENAI_CHAT_CONTEXT_TOKENS", "24576")),
            runpod_api_key=os.getenv("RUNPOD_API_KEY", ""),
            runpod_pod_id=os.getenv("RUNPOD_POD_ID", ""),
            vps_scheduler_interval_seconds=int(os.getenv("VPS_SCHEDULER_INTERVAL_SECONDS", "60")),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_helper_model=os.getenv("OPENAI_HELPER_MODEL", "gpt-5.4-mini"),
            openai_helper_max_output_tokens=int(os.getenv("OPENAI_HELPER_MAX_OUTPUT_TOKENS", "900")),
            openai_helper_reasoning_effort=os.getenv("OPENAI_HELPER_REASONING_EFFORT", "low"),
            openai_helper_for_customers=False,
            enable_openai_design_director=_bool_env("ENABLE_OPENAI_DESIGN_DIRECTOR", True),
            enable_openai_decision_director=_bool_env("ENABLE_OPENAI_DECISION_DIRECTOR", True),
            enable_web_search=(
                _bool_env("ENABLE_WEB_SEARCH", False)
                and _bool_env("ALLOW_WEB_SEARCH_CREDITS", False)
            ),
            legacy_public_model_label=os.getenv("LEGACY_PUBLIC_MODEL_LABEL", "Claude Sonnet 4.5"),
            public_model_label=os.getenv("PUBLIC_MODEL_LABEL", "Claude Sonnet 4.6"),
            web_search_model=os.getenv("WEB_SEARCH_MODEL", "gpt-5.5"),
            web_search_openrouter_model=os.getenv("WEB_SEARCH_OPENROUTER_MODEL", ""),
            web_search_context_size=os.getenv("WEB_SEARCH_CONTEXT_SIZE", "low"),
            web_search_for_customers=_bool_env("WEB_SEARCH_FOR_CUSTOMERS", True),
            web_search_max_output_tokens=int(os.getenv("WEB_SEARCH_MAX_OUTPUT_TOKENS", "900")),
            web_search_timeout_seconds=float(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "8")),
            web_search_allowed_domains=_csv_env("WEB_SEARCH_ALLOWED_DOMAINS", ""),
            web_search_blocked_domains=_csv_env("WEB_SEARCH_BLOCKED_DOMAINS", ""),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
            simple_request_max_output_tokens=int(os.getenv("SIMPLE_REQUEST_MAX_OUTPUT_TOKENS", "768")),
            vps_disable_qwen_thinking=_bool_env("VPS_DISABLE_QWEN_THINKING", True),
            enable_agent_orchestration=_bool_env("ENABLE_AGENT_ORCHESTRATION", True),
            enable_stream_agent_orchestration=_bool_env("ENABLE_STREAM_AGENT_ORCHESTRATION", False),
            max_cost_ratio_vs_claude=float(os.getenv("MAX_COST_RATIO_VS_CLAUDE", "0.50")),
            allow_premium_fallback=_bool_env("ALLOW_PREMIUM_FALLBACK", False),
            allow_direct_external_models=_bool_env("ALLOW_DIRECT_EXTERNAL_MODELS", False),
            max_request_input_chars=int(os.getenv("MAX_REQUEST_INPUT_CHARS", "120000")),
            max_request_output_tokens=int(os.getenv("MAX_REQUEST_OUTPUT_TOKENS", "16000")),
            tool_request_output_tokens=int(os.getenv("TOOL_REQUEST_OUTPUT_TOKENS", "4096")),
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
            api_rate_limit=max(int(os.getenv("API_RATE_LIMIT", "600")), 600),
            rate_limit_token_scope=os.getenv("RATE_LIMIT_TOKEN_SCOPE", "token_ip"),
            trusted_hosts=_csv_env("TRUSTED_HOSTS", "*"),
            admin_trusted_ips=_csv_env("ADMIN_TRUSTED_IPS", ""),
            trust_proxy_headers=_bool_env("TRUST_PROXY_HEADERS", False),
            cors_allowed_origins=_csv_env(
                "CORS_ALLOWED_ORIGINS",
                "http://127.0.0.1:8787,http://localhost:8787",
            ),
            expose_openapi=_bool_env("EXPOSE_OPENAPI", False),
            expose_detailed_health=_bool_env("EXPOSE_DETAILED_HEALTH", False),
            mercado_pago_access_token=os.getenv("MERCADO_PAGO_ACCESS_TOKEN", ""),
            mercado_pago_webhook_secret=os.getenv("MERCADO_PAGO_WEBHOOK_SECRET", ""),
            mercado_pago_webhook_tolerance_seconds=int(
                os.getenv("MERCADO_PAGO_WEBHOOK_TOLERANCE_SECONDS", "600")
            ),
            mercado_pago_public_url=os.getenv("MERCADO_PAGO_PUBLIC_URL", ""),
            public_trial_enabled=_bool_env("PUBLIC_TRIAL_ENABLED", False),
            public_trial_end_at=os.getenv("PUBLIC_TRIAL_END_AT", ""),
            public_trial_plan_id=os.getenv("PUBLIC_TRIAL_PLAN_ID", "ultra"),
            public_trial_daily_limit=int(os.getenv("PUBLIC_TRIAL_DAILY_LIMIT", "1200000")),
            public_trial_label=os.getenv("PUBLIC_TRIAL_LABEL", "Teste grátis 24h"),
            economy_public_model=os.getenv("ECONOMY_PUBLIC_MODEL", public_model_id),
            pro_public_model=os.getenv("PRO_PUBLIC_MODEL", public_model_id),
            ultra_public_model=os.getenv("ULTRA_PUBLIC_MODEL", "claude-sonnet-4.6"),
            ui_public_model=os.getenv("UI_PUBLIC_MODEL", public_model_id),
            auto_public_model=os.getenv("AUTO_PUBLIC_MODEL", public_model_id),
            router_agent="tencent/hy3-preview",
            cheap_code_agent="deepseek/deepseek-v4-flash",
            code_agent="qwen/qwen3-coder-next",
            reasoning_agent="deepseek/deepseek-v4-pro",
            ui_agent="qwen/qwen3-coder-next",
            fast_agent="deepseek/deepseek-v4-flash",
            premium_fallback="deepseek/deepseek-v4-pro",
            ultra_fallback="qwen/qwen3-coder-next",
            frontend_coder_agent="qwen/qwen3-coder-next",
            frontend_fix_agent="deepseek/deepseek-v4-flash",
            frontend_reasoning_agent="tencent/hy3-preview",
            backend_partner_agent="deepseek/deepseek-v4-pro",
            project_reasoning_agent="deepseek/deepseek-v4-pro",
            deep_reasoning_agent="deepseek/deepseek-v4-pro",
            gemini_code_helper_agent="google/gemini-2.5-flash-lite",
            enable_gemini_code_helper=False,
            openrouter_site_url=os.getenv("OPENROUTER_SITE_URL", "http://localhost:8787"),
            openrouter_app_name=os.getenv(
                "OPENROUTER_APP_NAME",
                "Claude Code",
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
