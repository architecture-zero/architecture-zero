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
    "default_rag_enabled":   "false",
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
    with get_session() as db:
        rows = db.query(Config).all()
        return {r.key: r.value for r in rows}


def get_system_prompt() -> str:
    """The served persona: DB row first, environment only as its seed.

    CHANGED 2026-08-27. This used to return the env value outright whenever
    SYSTEM_PROMPT was set, which made the admin panel's persona editor a no-op
    on every deployment whose environment sets it - and .env.example ships that
    variable uncommented, so that was all of them. The editor saved the row,
    reported "Saved", and the server never read it. A control that reports
    success and changes nothing is worse than one that is absent: it stops the
    operator looking.

    Nothing changes for a never-edited deployment. init_config_db() seeds the
    row from _DEFAULTS["system_prompt"], which is itself
    os.getenv("SYSTEM_PROMPT", ...) read at import, so a fresh boot serves
    exactly what the environment set. What the environment no longer does is
    override a deliberate edit made afterwards.

    The one deployment class this MOVES - env edited after first boot - is
    reported rather than hidden: system_prompt_divergence() is the observable
    seam and a startup hook names it at every boot.
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
