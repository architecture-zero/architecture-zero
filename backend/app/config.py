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
    env_val = os.getenv("SYSTEM_PROMPT", "")
    if env_val:
        return env_val
    return get_config("system_prompt", _DEFAULTS["system_prompt"])
