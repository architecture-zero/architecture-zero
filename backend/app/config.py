import os
from app.db import get_session
from app.models import Config

_SUGGESTIONS_DEFAULT = (
    '["What can you help me with?",'
    '"How do I get started with this assistant?",'
    '"What kinds of questions can I ask you?"]'
)

_DEFAULTS = {
    "system_prompt":         os.getenv("SYSTEM_PROMPT", "You are a helpful AI assistant."),
    "instance_name":         os.getenv("VITE_INSTANCE_NAME", "Architecture Zero"),
    "primary_color":         os.getenv("VITE_PRIMARY_COLOR", "#2563eb"),
    "context_strategy":      os.getenv("CONTEXT_STRATEGY", "warn"),
    "suggestions":           _SUGGESTIONS_DEFAULT,
    "allow_model_selection": "true",
    "allow_rag_toggle":      "true",
    "default_model":         os.getenv("DEFAULT_MODEL", ""),
    # ON by default. This is a retrieval platform, and the first question asked
    # of a fresh deployment decides what an evaluator concludes it is: with
    # retrieval off the model answers from training memory, cites nothing, and
    # reads as an instance whose corpus was never ingested. The operator can
    # still turn it off here, and the per-message toggle still exists - this
    # only chooses which way the default points, and "do not use the knowledge
    # base" was the wrong way for the only product this repo builds.
    "default_rag_enabled":   "true",
}


def init_config_db():
    with get_session() as db:
        for key, value in _DEFAULTS.items():
            if not db.query(Config).filter(Config.key == key).first():
                db.add(Config(key=key, value=value))


def get_config(key: str, default: str = "") -> str:
    with get_session() as db:
        row = db.query(Config).filter(Config.key == key).first()
        return row.value if row else default


def set_config(key: str, value: str):
    with get_session() as db:
        row = db.query(Config).filter(Config.key == key).first()
        if row:
            row.value = value
        else:
            db.add(Config(key=key, value=value))


def get_all_config() -> dict:
    """EVERY config row, secrets included. Callers that answer a request must
    use get_all_config_masked() instead - see the note there."""
    with get_session() as db:
        rows = db.query(Config).all()
        return {r.key: r.value for r in rows}


def is_secret_config_key(key: str) -> bool:
    """Config rows that hold a credential rather than a setting.

    One definition, because every surface that reports config has to agree on
    it. Provider keys are written here as `<provider>_api_key` by the settings
    endpoint, which itself only ever reports them as `<provider>_key_set`.
    """
    return key.endswith("_api_key")


def get_all_config_masked() -> dict:
    """Config as a response may carry it: every secret replaced by a boolean.

    /api/admin/config returned the raw table, and its guard is manage_system -
    a permission an Owner can grant, and one whose documented job is "edit
    system prompt, instance config, model settings". So a non-Owner could read
    every provider credential in cleartext from a surface a TIER BELOW the
    Owner-only settings page that deliberately masks the same values. Every
    neighbouring surface already strips its secrets: /api/settings reports
    `<provider>_key_set` booleans, /api/users strips password_hash and
    mfa_secret, and system_records refuses to touch get_all_config() at all
    with a comment naming provider keys as the reason.
    """
    out = {}
    for key, value in get_all_config().items():
        if is_secret_config_key(key):
            out[f"{key[:-len('_api_key')]}_key_set"] = bool(value and value.strip())
        else:
            out[key] = value
    return out


def get_system_prompt() -> str:
    """The served persona: DB row first, environment only as its seed.

    CHANGED 2026-08-27. This used to return the env value outright whenever
    SYSTEM_PROMPT was set, which made the stored persona row - the one
    PATCH /api/admin/config writes and reports "Saved" - dead on any deployment
    whose environment sets that variable. A write path that reports success and
    changes nothing is worse than one that is absent: it stops the operator
    looking. (This repo's .env.example does not set SYSTEM_PROMPT, so a default
    deployment was never affected; the fix matters the moment an operator
    exports the variable.)

    Nothing changes for a never-edited deployment. init_config_db() seeds the
    row from _DEFAULTS["system_prompt"], which is itself
    os.getenv("SYSTEM_PROMPT", ...) read at import, so a fresh boot serves
    exactly what the environment set. What the environment no longer does is
    override a deliberate edit made afterwards.

    The deployment class this MOVES - env set AND disagreeing with the stored
    row (env edited after first boot, or a row saved while the env override
    kept it unread) - is reported rather than hidden: system_prompt_divergence()
    is the observable seam and a startup hook names it at every boot.
    """
    return get_config("system_prompt", _DEFAULTS["system_prompt"])


def system_prompt_divergence() -> tuple[str, str] | None:
    """(env_value, served_value) when SYSTEM_PROMPT disagrees with the served row.

    Returns None when the environment sets no persona, or when the two agree.

    Report, do not reconcile: which value is the intended one is an operator
    decision, and silently preferring either is how the original defect got its
    foothold.
    """
    env_val = os.getenv("SYSTEM_PROMPT", "")
    if not env_val:
        return None
    served = get_config("system_prompt", _DEFAULTS["system_prompt"])
    return None if served == env_val else (env_val, served)
