"""Provider and instance settings, and the model catalogue.

Second router out of main.py. Same rules as system.py: no prefix, full literal
paths, guards carried verbatim, shared names from app.runtime_config, never
`from app.main import ...`.

NO router-level dependencies=[] specifically because the four routes do NOT
share a level: /api/models is any-authenticated (its guard is a decorator
kwarg, with no current_user param in the signature to remind you), while the
three /api/settings routes are require_owner. Flattening them onto the router
would silently downgrade three owner-only routes - the exact class the
level-aware pin in test_route_authz_wiring.py now catches.

OLLAMA_BASE comes from app.providers. main.py used to carry its own with a
different default, shadowed by a mid-file re-import; that whole statement is
dead after this commit and goes with it.
"""
import os

import requests
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.config import set_config
from app.logger import log
from app.jwt_auth import get_current_user, require_owner
from app.providers import (ENABLE_OLLAMA, ENABLE_ANTHROPIC, ENABLE_OPENAI,
                           OLLAMA_BASE, ANTHROPIC_KEY, OPENAI_COMPAT,
                           compat_key_configured, _compat_base, _compat_headers,
                           _get_runtime)
from app.runtime_config import (_config_or_default, _ollama_get, DEFAULT_MODEL,
                                RAG_SIMILARITY_THRESHOLD)

router = APIRouter()


# -- Provider / Instance Settings ---------------------------------------------

class ProviderSettingsRequest(BaseModel):
    ollama_enabled: bool | None = None
    anthropic_enabled: bool | None = None
    openai_enabled: bool | None = None
    ollama_base_url: str | None = None
    anthropic_api_key: str | None = None
    # One optional key slot per OPENAI_COMPAT registry provider (openai's
    # predates the registry; the rest follow the same name pattern).
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    mistral_api_key: str | None = None
    groq_api_key: str | None = None
    xai_api_key: str | None = None
    deepseek_api_key: str | None = None
    default_model: str | None = None
    rag_similarity_threshold: float | None = None


def _settings_dict() -> dict:
    out = {
        "ollama_enabled":           _get_runtime("provider_ollama_enabled",    "ENABLE_OLLAMA",    "true" if ENABLE_OLLAMA    else "false") == "true",
        "anthropic_enabled":        _get_runtime("provider_anthropic_enabled", "ENABLE_ANTHROPIC", "true" if ENABLE_ANTHROPIC else "false") == "true",
        "openai_enabled":           _get_runtime("provider_openai_enabled",    "ENABLE_OPENAI",    "true" if ENABLE_OPENAI    else "false") == "true",
        "ollama_base_url":          _get_runtime("ollama_base_url",   "OLLAMA_BASE",   OLLAMA_BASE),
        "anthropic_key_set":        bool(_get_runtime("anthropic_api_key", "ANTHROPIC_API_KEY", ANTHROPIC_KEY)),
        "default_model":            _config_or_default("default_model", DEFAULT_MODEL),
        "rag_similarity_threshold": float(_config_or_default("rag_similarity_threshold", str(RAG_SIMILARITY_THRESHOLD))),
    }
    for name in OPENAI_COMPAT:  # openai_key_set + the newer registry providers
        out[f"{name}_key_set"] = compat_key_configured(name)
    return out


@router.get("/api/settings")
def get_settings(current_user: dict = Depends(require_owner)):
    return _settings_dict()


@router.put("/api/settings")
def update_settings(body: ProviderSettingsRequest, current_user: dict = Depends(require_owner)):
    _MASKED = {"***", "········", ""}
    if body.ollama_enabled is not None:
        set_config("provider_ollama_enabled", "true" if body.ollama_enabled else "false")
    if body.anthropic_enabled is not None:
        set_config("provider_anthropic_enabled", "true" if body.anthropic_enabled else "false")
    if body.openai_enabled is not None:
        set_config("provider_openai_enabled", "true" if body.openai_enabled else "false")
    if body.ollama_base_url is not None:
        set_config("ollama_base_url", body.ollama_base_url.strip())
    if body.anthropic_api_key is not None and body.anthropic_api_key.strip() not in _MASKED:
        set_config("anthropic_api_key", body.anthropic_api_key.strip())
    for name in OPENAI_COMPAT:
        val = getattr(body, f"{name}_api_key", None)
        if val is not None and val.strip() not in _MASKED:
            set_config(f"{name}_api_key", val.strip())
    if body.default_model is not None:
        set_config("default_model", body.default_model.strip())
    if body.rag_similarity_threshold is not None:
        if 0.0 <= body.rag_similarity_threshold <= 1.0:
            set_config("rag_similarity_threshold", str(body.rag_similarity_threshold))
    log("settings_update", admin_id=current_user["id"])
    return _settings_dict()


@router.get("/api/settings/test-ollama")
def test_ollama_connection(current_user: dict = Depends(require_owner)):
    # base resolved here, not inside _ollama_get - both return paths report
    # it.
    base = _get_runtime("ollama_base_url", "OLLAMA_BASE", OLLAMA_BASE)
    try:
        resp = _ollama_get("/api/tags", timeout=5)
        models = resp.json().get("models", [])
        return {"ok": True, "model_count": len(models), "base_url": base}
    except Exception as e:
        return {"ok": False, "error": str(e), "base_url": base}

# Fallback list, used only when Anthropic's /v1/models call fails (no key,
# offline). Live models are discovered dynamically - see
# _fetch_anthropic_models().
_ANTHROPIC_FALLBACK = [
    {"value": "claude-opus-4-8",           "label": "Claude Opus 4.8",   "badge": "Best"},
    {"value": "claude-sonnet-4-6",         "label": "Claude Sonnet 4.6", "badge": "Smart"},
    {"value": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5",  "badge": "Fast"},
]

_anthropic_models_cache: dict = {"ts": 0.0, "models": None}


def _anthropic_badge(model_id: str) -> str:
    mid = model_id.lower()
    if "opus" in mid:   return "Best"
    if "sonnet" in mid: return "Smart"
    if "haiku" in mid:  return "Fast"
    return "Anthropic"


def _fetch_anthropic_models() -> list:
    """Live model list from Anthropic's /v1/models, cached 1h. Falls back to
    a static list when the API is unreachable so the picker is never empty."""
    import time as _time
    now = _time.time()
    cached = _anthropic_models_cache["models"]
    if cached is not None and now - _anthropic_models_cache["ts"] < 3600:
        return cached
    try:
        from app.providers import _anthropic_headers
        resp = requests.get("https://api.anthropic.com/v1/models?limit=100",
                            headers=_anthropic_headers(), timeout=5)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        models = [
            {"value": m["id"], "label": m.get("display_name", m["id"]),
             "badge": _anthropic_badge(m["id"])}
            for m in data
        ] or _ANTHROPIC_FALLBACK
    except Exception:
        models = _ANTHROPIC_FALLBACK
    _anthropic_models_cache.update(ts=now, models=models)
    return models

_OPENAI_MODELS = [
    {"value": "gpt-4o",      "label": "GPT-4o",      "badge": "Best"},
    {"value": "gpt-4o-mini", "label": "GPT-4o mini", "badge": "Fast"},
    {"value": "o3-mini",     "label": "o3-mini",      "badge": "Reason"},
]

# Static fallbacks for registry providers when their live /models call fails
# (no network, provider outage) - the picker must never be empty for a keyed
# provider. The LIVE list from _fetch_compat_models is what users normally
# see.
_COMPAT_FALLBACK_MODELS: dict = {
    "gemini":   [{"value": "gemini-3.6-flash", "label": "Gemini 3.6 Flash", "badge": "Fast"},
                 {"value": "gemini-2.5-pro",   "label": "Gemini 2.5 Pro",   "badge": "Best"}],
    "mistral":  [{"value": "mistral-large-latest", "label": "Mistral Large", "badge": "Best"},
                 {"value": "mistral-small-latest", "label": "Mistral Small", "badge": "Fast"}],
    "groq":     [],  # no unique prefix - live list only (values are namespaced groq:<id>)
    "xai":      [{"value": "grok-4.5", "label": "Grok 4.5", "badge": "Best"}],
    "deepseek": [{"value": "deepseek-v4-flash", "label": "DeepSeek V4 Flash", "badge": "Fast"},
                 {"value": "deepseek-v4-pro",   "label": "DeepSeek V4 Pro",   "badge": "Best"}],
}

_compat_models_cache: dict = {}  # provider -> {"ts": float, "models": list}


def _fetch_compat_models(provider: str) -> list:
    """Live model list from an OpenAI-compatible provider's /models, cached
    1h, falling back to the static seed list above. Mirrors
    _fetch_anthropic_models.

    Two registry-specific rules:
    - Gemini returns ids prefixed "models/..." - stripped so they round-trip
      through _resolve_model's prefix routing.
    - A provider WITH routing prefixes gets its list filtered to ids matching
      them (drops embedding/image models from mixed lists); a provider
      WITHOUT prefixes (groq) keeps everything, with values namespaced
      "provider:id" so routing works.
    """
    import time as _time
    now = _time.time()
    cached = _compat_models_cache.get(provider)
    if cached is not None and now - cached["ts"] < 3600:
        return cached["models"]
    entry = OPENAI_COMPAT[provider]
    try:
        resp = requests.get(f"{_compat_base(provider)}/models",
                            headers=_compat_headers(provider), timeout=5)
        resp.raise_for_status()
        ids = [m.get("id", "") for m in resp.json().get("data", [])]
        models = []
        for mid in ids:
            if provider == "gemini" and mid.startswith("models/"):
                mid = mid[len("models/"):]
            if not mid:
                continue
            if entry["prefixes"]:
                if not mid.startswith(entry["prefixes"]):
                    continue
                value = mid
            else:
                value = f"{provider}:{mid}"
            models.append({"value": value, "label": mid, "badge": entry["label"]})
        models = models or _COMPAT_FALLBACK_MODELS.get(provider, [])
    except Exception:
        models = _COMPAT_FALLBACK_MODELS.get(provider, [])
    _compat_models_cache[provider] = {"ts": now, "models": models}
    return models

# Models never offered in the picker for LICENSE reasons - their weights are
# not clean to redistribute to a client on their own infra. Baked in so they
# cannot leak into a client deployment regardless of what is pulled into
# Ollama.
_LICENSE_BLOCKED_MODELS = {"qwen2.5-coder:3b"}

# Hidden by default because unwanted, not for a hard license reason. This is
# a preference - to bring one back, just remove it from this set.
_HIDDEN_BY_DEFAULT_MODELS: set = set()


def _is_blocked_model(model_name: str) -> bool:
    """True if a model should be hidden from the picker: the baked-in
    license-blocked set (never shippable) + the hidden-by-default set
    (unwanted) + any per-instance MODEL_BLOCKLIST env entries
    (comma-separated). Matches a full `name:tag` or a bare base name."""
    name = model_name.lower()
    blocked = {m.lower() for m in (_LICENSE_BLOCKED_MODELS | _HIDDEN_BY_DEFAULT_MODELS)} | {
        m.strip().lower() for m in os.getenv("MODEL_BLOCKLIST", "").split(",") if m.strip()
    }
    return name in blocked or name.split(":")[0] in blocked


@router.get("/api/models", dependencies=[Depends(get_current_user)])
def get_available_models():
    """Returns grouped models for all enabled providers. Covered by
    AuthMiddleware when ENABLE_AUTH=true."""
    groups = []
    if ENABLE_OLLAMA:
        try:
            data = _ollama_get("/api/tags", timeout=5).json()
            models = [
                {"value": m["name"], "label": m["name"], "badge": "Local"}
                for m in data.get("models", [])
                if not _is_blocked_model(m["name"])
            ]
        except Exception:
            models = []
        groups.append({"provider": "ollama", "label": "Local", "models": models})
    # Anthropic/OpenAI follow the registry's dormant-until-keyed rule: a
    # configured key activates them, the legacy ENABLE_* flags still can too.
    if ENABLE_ANTHROPIC or bool(_get_runtime("anthropic_api_key", "ANTHROPIC_API_KEY", ANTHROPIC_KEY)):
        groups.append({"provider": "anthropic", "label": "Anthropic", "models": _fetch_anthropic_models()})
    if ENABLE_OPENAI or compat_key_configured("openai"):
        groups.append({"provider": "openai", "label": "OpenAI", "models": _OPENAI_MODELS})
    # Registry providers appear the moment their key is configured - no
    # enable flag; dormant (unkeyed) providers stay out of the picker
    # entirely.
    for name, entry in OPENAI_COMPAT.items():
        if name == "openai":  # legacy ENABLE_OPENAI flag handles it above
            continue
        if compat_key_configured(name):
            groups.append({"provider": name, "label": entry["label"],
                           "models": _fetch_compat_models(name)})
    return {"groups": groups}
