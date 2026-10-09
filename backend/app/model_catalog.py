"""The model catalog: what the model picker offers, and what a model setting
resolves to when it names a family instead of a version.

A model setting may hold a FAMILY ALIAS - `claude-opus-latest`,
`claude-sonnet-latest`, `claude-haiku-latest`, or any other Claude family the
key lists - instead of a version. `resolve()` turns it into the newest model of
that family the configured Anthropic key can use, read from Anthropic's
/v1/models and cached an hour, so a setting on "newest Opus" moves to each new
Opus on its own. Anything else passes through unchanged: a concrete id is a
PIN. An eval's answering model and its judge are pins on purpose - a
measurement must not change because a new model shipped - so the admin routes
store a family picked for them as the version it resolves to at that moment.

The picker offers one entry per Claude family - the alias, labelled with the
version it resolves to now - instead of every version the key can see, and
trims the other providers' lists to the newest model of each family. A saved
value the trimmed list no longer shows stays visible in the admin panel (its
"(saved)" entry), so trimming never hides a setting.

Shared verbatim by every backend built from this template; keep it free of
anything instance-specific (a caller passes its own badge rule).
"""

import logging
import re
import threading
import time

import requests

logger = logging.getLogger(__name__)

CACHE_SECONDS = 3600          # a good list is trusted for an hour
FAILURE_CACHE_SECONDS = 300   # an unreadable one is asked for again sooner

# What an alias resolves to when Anthropic's list cannot be read (no key, an
# outage) and nothing has resolved since this process started. Read ONLY then:
# while the live list answers, the newest model it names wins. Update a line
# when its family's newest model changes.
FAMILY_FALLBACK = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5-20251001",
}

# Picker order: the three tiers first, any other family the key lists after.
_FAMILY_ORDER = ("opus", "sonnet", "haiku")

_ALIAS_RE = re.compile(r"^claude-([a-z]+)-latest$")

_list_cache: dict = {"ts": 0.0, "ttl": 0.0, "models": None}
_refresh_lock = threading.Lock()
_last_resolved: dict = {}     # family -> the id this process last resolved it to


def alias_for(family: str) -> str:
    return f"claude-{family}-latest"


def family_of(model_id: str) -> str:
    """The Claude family of an id or an alias - the first word after
    "claude-" that is not a version number, so "claude-opus-5-5" and the
    older "claude-3-5-sonnet-20241022" shape both read right. "" for an id
    that is not Claude's."""
    parts = (model_id or "").strip().lower().split("-")
    if len(parts) < 2 or parts[0] != "claude":
        return ""
    for p in parts[1:]:
        if p.isalpha():
            return p
    return ""


def _fetch_list() -> list:
    """One read of Anthropic's model list for the configured key - id,
    display_name, created_at per model; [] when it cannot be read (no key, an
    outage)."""
    try:
        from app.providers import _anthropic_headers
        resp = requests.get("https://api.anthropic.com/v1/models?limit=100",
                            headers=_anthropic_headers(), timeout=5)
        resp.raise_for_status()
        return [m for m in resp.json().get("data", []) if m.get("id")]
    except Exception:
        return []


def _store(models: list) -> None:
    _list_cache.update(ts=time.time(), models=models,
                       ttl=CACHE_SECONDS if models else FAILURE_CACHE_SECONDS)


def _refresh_behind() -> None:
    """Refresh a STALE list on a thread of its own, one at a time, keeping the
    stale list if the read fails (and asking again sooner). A chat answers
    from the list it has meanwhile: the read is a blocking HTTP call, and the
    chat edge runs inside the server's event loop."""
    if not _refresh_lock.acquire(blocking=False):
        return

    def run():
        try:
            fresh = _fetch_list()
            if fresh:
                _store(fresh)
            else:
                _list_cache.update(ts=time.time(), ttl=FAILURE_CACHE_SECONDS)
        finally:
            _refresh_lock.release()

    threading.Thread(target=run, name="model-catalog-refresh", daemon=True).start()


def _anthropic_list() -> list:
    """Anthropic's model list - trusted for an hour; once stale, answered from
    while a fresh copy is read behind (_refresh_behind). Only a process with
    no list yet - its first use, or every read so far failed (then asked
    again after five minutes, so a key-less instance does not ask on every
    request) - reads it inline."""
    cached = _list_cache["models"]
    if cached is not None and time.time() - _list_cache["ts"] < _list_cache["ttl"]:
        return cached
    if cached:
        _refresh_behind()
        return cached
    models = _fetch_list()
    _store(models)
    return models


def newest_by_family() -> dict:
    """family -> the newest model of it the key lists: the latest created_at,
    and on a tie (or none given) the list's own newest-first order."""
    best: dict = {}
    for m in _anthropic_list():
        fam = family_of(m["id"])
        if not fam:
            continue
        cur = best.get(fam)
        if cur is None or (m.get("created_at") or "") > (cur.get("created_at") or ""):
            best[fam] = m
    return best


def resolve(model: str) -> str:
    """The id a call sends. A family alias becomes that family's newest model
    - from the live list, else the last answer this process had, else
    FAMILY_FALLBACK; an alias for a family none of those know is returned
    as written, so the provider refuses it loudly. Anything that is not an
    alias - a pinned version, another provider's id, a local tag - passes
    through unchanged. A family moving to a new model is logged once
    ("model_family_moved"), so the switch is visible when it happens."""
    m = (model or "").strip()
    match = _ALIAS_RE.match(m)
    if not match:
        return model
    fam = match.group(1)
    newest = newest_by_family().get(fam)
    if newest:
        rid = newest["id"]
        prev = _last_resolved.get(fam)
        if prev != rid:
            if prev:
                logger.warning("model_family_moved family=%s from=%s to=%s", fam, prev, rid)
            else:
                logger.info("model_family_resolved family=%s to=%s", fam, rid)
            _last_resolved[fam] = rid
        return rid
    return _last_resolved.get(fam) or FAMILY_FALLBACK.get(fam, m)


def anthropic_picker_models(badge) -> list:
    """The picker's Anthropic group: one entry per Claude family the key
    lists - the family's alias, labelled with the version it resolves to now,
    badged by the caller's own rule (`badge(model_id) -> str`). With no list,
    the fallback map's families, so the group is never empty."""
    newest = newest_by_family()
    if not newest:
        return [{"value": alias_for(f), "label": f"Claude {f.title()} - newest",
                 "badge": badge(mid)} for f, mid in FAMILY_FALLBACK.items()]
    order = sorted(newest, key=lambda f: (_FAMILY_ORDER.index(f) if f in _FAMILY_ORDER
                                          else len(_FAMILY_ORDER), f))
    out = []
    for f in order:
        m = newest[f]
        name = m.get("display_name") or m["id"]
        now = name[len("Claude "):] if name.startswith("Claude ") else name
        out.append({"value": alias_for(f), "label": f"Claude {f.title()} - newest (now {now})",
                    "badge": badge(m["id"])})
    return out


def anthropic_all_versions(badge) -> list:
    """Every Claude version the key lists, newest first, as concrete ids - for
    the admin's "show every version" switch, where picking one PINS a setting
    to it (a rollback lever when a new release misbehaves, or a deployment
    that must hold one version). [] when the list cannot be read."""
    models = sorted(_anthropic_list(), key=lambda m: m.get("created_at") or "", reverse=True)
    return [{"value": m["id"], "label": m.get("display_name") or m["id"], "badge": badge(m["id"])}
            for m in models if family_of(m["id"])]


# -- Other providers: the newest of each family -------------------------------

# Models a chat cannot use - embeddings, speech, images, moderation, search -
# dropped from a provider's list by any of these in the id.
_NOT_FOR_CHAT = ("embed", "tts", "whisper", "transcri", "audio", "image", "veo",
                 "dall-e", "moderation", "guard", "rerank", "aqa", "realtime",
                 "search", "live", "ocr")
_FLAG_WORDS = {"preview", "exp", "experimental", "latest"}
_SIZE_RE = re.compile(r"^\d+(\.\d+)?[bm]$")              # 70b, 1.5b - part of the family
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)[a-z]?$")   # 3.6, 4o, v4, 2411, 001


def _family_version(model_id: str) -> tuple:
    """(family key, version numbers, flags) of a model id: the id with its
    version numbers, dates and preview/experimental/latest words taken out is
    the family ("gemini-3.6-flash" and "gemini-2.5-flash" are both
    "gemini-flash"); a size like "70b" stays in it."""
    family, version, flags = [], [], set()
    for tok in model_id.lower().split("-"):
        if tok in _FLAG_WORDS:
            flags.add(tok)
        elif _SIZE_RE.match(tok):
            family.append(tok)
        elif _VERSION_RE.match(tok):
            version.extend(int(x) for x in _VERSION_RE.match(tok).group(1).split("."))
        else:
            family.append(tok)
    return "-".join(family), tuple(version), flags


def trim_to_newest(models: list, created: dict | None = None) -> list:
    """A provider's picker list without the models a chat cannot use, and
    with each family down to one entry: the provider's own "-latest" alias
    when it lists one (it follows the family by itself), else the newest
    stable model - by creation time when the provider gives one for every
    candidate (`created`: picker value -> timestamp), else by version
    number - with a preview or experiment only when the family has nothing
    stable. Families keep the order they first appear in."""
    created = created or {}
    families: dict = {}
    for m in models:
        value = m["value"]
        mid = value.split(":", 1)[1] if ":" in value else value
        if any(w in mid.lower() for w in _NOT_FOR_CHAT):
            continue
        fam, ver, flags = _family_version(mid)
        families.setdefault(fam, []).append((m, ver, flags, created.get(value)))
    out = []
    for members in families.values():
        native = [x for x in members if "latest" in x[2]]
        if native:
            out.append(native[0][0])
            continue
        stable = [x for x in members if not x[2] & {"preview", "exp", "experimental"}] or members
        if all(x[3] is not None for x in stable):
            pick = max(stable, key=lambda x: x[3])
        else:
            pick = max(stable, key=lambda x: x[1])
        out.append(pick[0])
    return out
