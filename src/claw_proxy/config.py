"""Configuration from environment variables."""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

_REQUIRED = [
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
]

_missing = [k for k in _REQUIRED if not os.environ.get(k)]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")

SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_PUBLIC_URL: str = os.environ.get("SUPABASE_PUBLIC_URL") or SUPABASE_URL
SUPABASE_ANON_KEY: str = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_ROLE_KEY: str = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

http_client = httpx.AsyncClient(timeout=30)
_supabase_client = None


def get_supabase_client():
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client

        _supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _supabase_client


# --- Server config ---

ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:8081").split(",")
    if o.strip()
]

ALLOWED_ORIGIN_REGEX = os.environ.get("ALLOWED_ORIGIN_REGEX") or None

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

# --- ZeroClaw config (optional — only needed when ZeroClaw proxy is active) ---


def _get_optional(name: str) -> str | None:
    return os.environ.get(name)


def _get_network_mode() -> str:
    network_mode = os.environ.get("ZEROCLAW_NETWORK_MODE", "host")
    if network_mode not in {"host", "shared"}:
        raise RuntimeError(
            "Invalid ZEROCLAW_NETWORK_MODE: expected 'host' or 'shared', "
            f"got {network_mode!r}"
        )
    return network_mode


def _get_zeroclaw_config() -> dict | None:
    """Load ZeroClaw config if TOKEN_ENCRYPTION_KEY is set (feature flag)."""
    key = _get_optional("TOKEN_ENCRYPTION_KEY")
    if not key:
        return None
    network_mode = _get_network_mode()
    config = {
        "token_encryption_key": key,
        "zeroclaw_image": os.environ.get("ZEROCLAW_IMAGE", "zeroclaw:latest"),
        "zeroclaw_data_dir": os.environ.get("ZEROCLAW_DATA_DIR", "/data/zeroclaw"),
        "zeroclaw_host_data_dir": os.environ.get(
            "ZEROCLAW_HOST_DATA_DIR",
            os.environ.get("ZEROCLAW_DATA_DIR", "/data/zeroclaw"),
        ),
        "admin_data_dir": os.environ.get("ADMIN_DATA_DIR", "data/admin"),
        "network_mode": network_mode,
        "network_name": os.environ.get("ZEROCLAW_NETWORK_NAME", "lifeatlas-net"),
        "push_webhook_base_url": os.environ.get(
            "ZEROCLAW_PUSH_WEBHOOK_BASE_URL", "http://172.17.0.1:8000"
        ),
        "lifecycle_check_interval_minutes": int(
            os.environ.get("LIFECYCLE_CHECK_INTERVAL_MINUTES", "60")
        ),
        "inactive_container_days": int(os.environ.get("INACTIVE_CONTAINER_DAYS", "30")),
        "templates_dir": os.environ.get("ZEROCLAW_TEMPLATES_DIR", ""),
    }
    # LLM provider config passed to containers (all optional — defaults to Ollama)
    llm_api_key = os.environ.get("ZEROCLAW_LLM_API_KEY")
    if llm_api_key:
        config["llm_api_key"] = llm_api_key
        config["llm_provider"] = os.environ.get("ZEROCLAW_LLM_PROVIDER", "openrouter")
        config["llm_model"] = os.environ.get("ZEROCLAW_LLM_MODEL", "")
    return config


ZEROCLAW_CONFIG = _get_zeroclaw_config()
