"""Runtime constants and helpers shared by main.py and the routers.

Named runtime_config, not app_config, because app/config.py already exists and
holds the DB-backed key/value store that nearly every route reads. Two files one
character apart in every traceback is a maintenance trap; these are the
env-derived values and the helpers that sit on top of them.

DEPENDENCY DIRECTION, and it is load-bearing: main -> routers -> here. This
module must never import app.main. A router that reaches back into main mints an
import cycle and makes every patch("app.main.X") target ambiguous.
"""
import os

import requests

from app.config import get_config
# _get_runtime and _ollama_headers are PRIVATE names in app.providers. This
# module is a second consumer of both, so a rename over there orphans this file
# rather than failing at its definition site.
from app.providers import _get_runtime, _ollama_headers, OLLAMA_BASE

# ── Env constants, moved verbatim from main.py ───────────────────────────────
#
# OLLAMA_BASE is deliberately ABSENT from this block and imported from
# app.providers above. main.py defined its own with a "http://localhost:11434"
# default while providers.py uses "http://host.docker.internal:11434", and
# main's mid-file `from app.providers import (... OLLAMA_BASE ...)` rebinds it -
# so the value every caller actually reads is providers'. Re-declaring it here
# from main's line would flip /api/health to localhost inside the container,
# where nothing is listening, and no test would catch it: /api/health is public
# by design and nothing asserts on its body.
DEFAULT_MODEL               = os.getenv("DEFAULT_MODEL", "qwen3:8b")
RAG_ONLY_MODE               = os.getenv("RAG_ONLY_MODE", "false").lower() == "true"
RAG_SIMILARITY_THRESHOLD    = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.40"))
PII_SCAN_MODE               = os.getenv("PII_SCAN_MODE", "off").lower()
# Private by default. Guest (unauthenticated) access is OFF unless explicitly
# opted in here AND enabled in admin config. Without this env var set, the
# instance is login-required for everyone.
ALLOW_GUEST_MODE            = os.getenv("ALLOW_GUEST_MODE", "false").lower() == "true"
ENCRYPTION_AT_REST_VERIFIED = os.getenv("ENCRYPTION_AT_REST_VERIFIED", "false").lower() == "true"
_DATA_DIR                   = os.getenv("DATA_DIR", "/app/data")


def _config_or_default(key: str, default: str) -> str:
    """get_config, but a row that EXISTS and is BLANK does not beat the
    default.

    `get_config` returns `row.value if row else default`, so an empty stored
    value wins over a perfectly good fallback - and clearing a field in the
    admin UI writes exactly that. A blank `default_model` row resolves the
    eval writer to "", every answer errors, and the run reports 0% as though
    that were a measurement.

    Used only where a blank is genuinely broken - model ids and the numeric
    threshold, where `float("")` raises and 500s the chat path. Deliberately
    NOT applied inside get_config itself: some callers compare against
    "true"/"false", and promoting a blank to a non-empty default there would
    flip a stored false into a true.
    """
    val = (get_config(key, "") or "").strip()
    return val or default


def _ollama_get(path: str, timeout: int = 5):
    """GET from the configured Ollama base URL with CF-Access headers when
    set."""
    base = _get_runtime("ollama_base_url", "OLLAMA_BASE", OLLAMA_BASE)
    return requests.get(f"{base}{path}", headers=_ollama_headers(), timeout=timeout)
