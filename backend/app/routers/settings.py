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
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import model_catalog
from app.config import set_config, encrypt_secret
from app.logger import log
from app.jwt_auth import get_current_user, require_owner, require_step_up
from app.security import validate_outbound_url
from app.providers import (ENABLE_OLLAMA, ENABLE_ANTHROPIC, ENABLE_OPENAI,
                           OLLAMA_BASE, ANTHROPIC_KEY, OPENAI_COMPAT, offered_providers,
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
    # The caller's OWN password - the step-up (ruled 2026-09-21): this route
    # holds the egress address and every provider key. Optional at the schema
    # so a missing value is the route's 400 (a string detail), never
    # pydantic's 422 (a list detail the admin panel cannot render). Stored
    # nowhere: the handler writes fields by name.
    current_password: str = ""


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
    # STEP-UP FIRST (ruled 2026-09-21). A bearer proves possession of a browser;
    # the password proves the person - and this is the route that points the
    # instance's egress somewhere and holds every provider key, so a stolen
    # Owner session on an unlocked device must not be enough to redirect it.
    require_step_up(current_user, body.current_password, "change provider settings")
    _MASKED = {"***", "········", ""}
    # VALIDATE BEFORE WRITING ANYTHING. This check used to sit at the bottom,
    # after eight set_config calls had already committed - and the admin UI
    # sends the whole body at once, so one bad threshold wrote the providers,
    # the base URL, the keys and the model, and THEN answered 400. The operator
    # read "Save failed" over a half-applied change, which is worse than the
    # silent discard it replaced: that at least left the response and the
    # database agreeing. PATCH /api/admin/config already validates in a
    # pre-write block for exactly this reason, with a comment saying so.
    #
    # NaN is rejected explicitly because every comparison against it is False,
    # so it fails the range test for the wrong reason and would otherwise be
    # indistinguishable from an ordinary refusal.
    if body.rag_similarity_threshold is not None:
        _thr = body.rag_similarity_threshold
        if _thr != _thr or not (0.0 <= _thr <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="rag_similarity_threshold must be a number between 0 and 1")
    # The model endpoint is where every prompt goes; until 2026-10-02 it stored
    # any string, the cloud metadata address included. Checked here, in the
    # same pre-write block, so a refused address writes nothing else either.
    _ollama_url = (validate_outbound_url(body.ollama_base_url)
                   if body.ollama_base_url is not None else None)

    if body.ollama_enabled is not None:
        set_config("provider_ollama_enabled", "true" if body.ollama_enabled else "false")
    if body.anthropic_enabled is not None:
        set_config("provider_anthropic_enabled", "true" if body.anthropic_enabled else "false")
    if body.openai_enabled is not None:
        set_config("provider_openai_enabled", "true" if body.openai_enabled else "false")
    if body.ollama_base_url is not None:
        set_config("ollama_base_url", _ollama_url)
    if body.anthropic_api_key is not None and body.anthropic_api_key.strip() not in _MASKED:
        set_config("anthropic_api_key", encrypt_secret(body.anthropic_api_key.strip()))
    for name in OPENAI_COMPAT:
        val = getattr(body, f"{name}_api_key", None)
        if val is not None and val.strip() not in _MASKED:
            set_config(f"{name}_api_key", encrypt_secret(val.strip()))
    if body.default_model is not None:
        set_config("default_model", body.default_model.strip())
    if body.rag_similarity_threshold is not None:
        # Already range-checked in the pre-write block above; this only writes.
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

def _anthropic_badge(model_id: str) -> str:
    mid = model_id.lower()
    if "opus" in mid:   return "Best"
    if "sonnet" in mid: return "Smart"
    if "haiku" in mid:  return "Fast"
    return "Anthropic"


def _fetch_anthropic_models() -> list:
    """The picker's Anthropic group: one entry per Claude family - its alias,
    "newest" plus the version it resolves to now - from Anthropic's live list
    (cached 1h in model_catalog), never every version the key can see. With
    no list (no key, offline) the catalog's fallback families, so the picker
    is never empty."""
    return model_catalog.anthropic_picker_models(_anthropic_badge)

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
    "openai":   _OPENAI_MODELS,
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


def _fetch_compat_models(provider: str, all_versions: bool = False) -> list:
    """Live model list from an OpenAI-compatible provider's /models, cached
    1h, falling back to the static seed list above. Mirrors
    _fetch_anthropic_models.

    Three registry-specific rules:
    - Gemini returns ids prefixed "models/..." - stripped so they round-trip
      through _resolve_model's prefix routing.
    - A provider WITH routing prefixes gets its list filtered to ids matching
      them (drops embedding/image models from mixed lists); a provider
      WITHOUT prefixes (groq) keeps everything, with values namespaced
      "provider:id" so routing works.
    - The live list is trimmed to the newest model of each family, and the
      models a chat cannot use are dropped (model_catalog.trim_to_newest) -
      the picker shows what is current, not every version a provider still
      serves. `all_versions` returns the untrimmed list instead (the
      admin's "show every version" switch, to pin one).
    """
    import time as _time
    now = _time.time()
    key = "all" if all_versions else "models"
    cached = _compat_models_cache.get(provider)
    if cached is not None and now - cached["ts"] < 3600:
        return cached[key]
    entry = OPENAI_COMPAT[provider]
    everything = None
    try:
        resp = requests.get(f"{_compat_base(provider)}/models",
                            headers=_compat_headers(provider), timeout=5)
        resp.raise_for_status()
        models, created = [], {}
        for row in resp.json().get("data", []):
            mid = row.get("id", "")
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
            if isinstance(row.get("created"), (int, float)):
                created[value] = row["created"]
        everything = models or None
        models = (model_catalog.trim_to_newest(models, created)
                  or _COMPAT_FALLBACK_MODELS.get(provider, []))
    except Exception:
        models = _COMPAT_FALLBACK_MODELS.get(provider, [])
    _compat_models_cache[provider] = {"ts": now, "models": models,
                                      "all": everything or models}
    return _compat_models_cache[provider][key]

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
def get_available_models(all_versions: bool = False):
    """Returns grouped models for all enabled providers. Covered by
    AuthMiddleware when ENABLE_AUTH=true.

    What is current by default: one entry per Claude family and the newest
    model of each other provider's families. `all_versions=true` - the
    admin's "show every version" switch - adds every concrete Claude version
    in its own group and gives the other providers their untrimmed lists, so
    a setting can be PINNED to one; the chat picker never asks for it."""
    groups = []
    # ONE predicate with the chat route's dispatch gate (ruled 2026-09-21):
    # providers.offered_providers. What the picker shows is dispatchable and
    # nothing else is - and the Settings tab's Ollama toggle now hides the
    # local models it disables (this read the import-time env flag before and
    # ignored the toggle).
    offered = offered_providers()
    if "ollama" in offered:
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
    if "anthropic" in offered:
        groups.append({"provider": "anthropic", "label": "Anthropic", "models": _fetch_anthropic_models()})
        if all_versions:
            groups.append({"provider": "anthropic", "label": "Anthropic - every version (pins)",
                           "models": model_catalog.anthropic_all_versions(_anthropic_badge)})
    if "openai" in offered:
        # Live like every registry provider (its static list is the fallback):
        # a hand-kept list only ever shows the models current when it was written.
        groups.append({"provider": "openai", "label": "OpenAI",
                       "models": _fetch_compat_models("openai", all_versions)})
    # Registry providers appear the moment their key is configured - no
    # enable flag; dormant (unkeyed) providers stay out of the picker
    # entirely.
    for name, entry in OPENAI_COMPAT.items():
        if name == "openai":  # its own group above (offered_providers: its toggle or a key)
            continue
        if name in offered:
            groups.append({"provider": name, "label": entry["label"],
                           "models": _fetch_compat_models(name, all_versions)})
    return {"groups": groups}
