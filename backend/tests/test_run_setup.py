"""The run-setup stamp (2026-11-04, app/run_setup.py).

The trust panel bands only runs sharing one stamp, so the stamp has to move
when the system moves and hold still when only running state does:

- a settings row, a run option or a new build moves it;
- a state row (peer health, the backup receipt) and a key rotation do not -
  the first would make every run its own era, the second would feed a
  credential into exportable eval rows.

The last test is the backstop for the next setting: it pins every
settings-table write in app/, so a new row cannot ship until it is classified
as a setting (stamped, the default) or as state (`_STATE_KEYS` /
`_STATE_PREFIXES`).
"""
import pathlib
import re

from app.config import get_config, set_config
from app.db import get_session
from app.models import Config
from app.run_setup import is_state_key, run_setup_stamp

_OPTS = {"writer": "w1", "use_rag": True, "n_results": 8,
         "retrieval_only": False, "use_peers": False}


def _drop(*keys):
    with get_session() as db:
        db.query(Config).filter(Config.key.in_(keys)).delete(synchronize_session=False)


def test_one_setup_one_stamp(client):
    assert run_setup_stamp(dict(_OPTS)) == run_setup_stamp(dict(_OPTS))
    assert re.fullmatch(r"[0-9a-f]{16}", run_setup_stamp(dict(_OPTS)))


def test_a_settings_change_is_a_new_stamp(client):
    before = get_config("rag_similarity_threshold", "")
    try:
        set_config("rag_similarity_threshold", "0.40")
        a = run_setup_stamp(dict(_OPTS))
        set_config("rag_similarity_threshold", "0.55")
        assert run_setup_stamp(dict(_OPTS)) != a
    finally:
        if before:
            set_config("rag_similarity_threshold", before)
        else:
            _drop("rag_similarity_threshold")


def test_a_run_option_is_a_new_stamp(client):
    assert (run_setup_stamp(dict(_OPTS, use_peers=True))
            != run_setup_stamp(dict(_OPTS, use_peers=False)))


def test_a_new_build_is_a_new_stamp(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "aaaaaaa")
    a = run_setup_stamp(dict(_OPTS))
    monkeypatch.setenv("GIT_SHA", "bbbbbbb")
    assert run_setup_stamp(dict(_OPTS)) != a


def test_state_rows_never_move_the_stamp(client):
    from app.peers import _health_key
    keys = [_health_key("p-stamp"), "last_backup_timestamp", "last_backup_file",
            "rerank_hosted_vendors_used", "setup_wizard_state"]
    before = {k: get_config(k, None) for k in keys}
    try:
        a = run_setup_stamp(dict(_OPTS))
        for k in keys:
            set_config(k, "changed-by-test")
        assert run_setup_stamp(dict(_OPTS)) == a
    finally:
        for k, v in before.items():
            if v is None:
                _drop(k)
            else:
                set_config(k, v)


def test_a_key_rotation_never_moves_the_stamp(client):
    before = get_config("anthropic_api_key", None)
    try:
        set_config("anthropic_api_key", "rotation-one")
        a = run_setup_stamp(dict(_OPTS))
        set_config("anthropic_api_key", "rotation-two")
        assert run_setup_stamp(dict(_OPTS)) == a
    finally:
        if before is None:
            _drop("anthropic_api_key")
        else:
            set_config("anthropic_api_key", before)


# Every first argument `set_config(` is called with in app/, as source text.
# A SETTING is stamped (the default); a STATE row is left out. A new write
# fails this test until it is added to one list - and a state row also to
# run_setup's _STATE_KEYS / _STATE_PREFIXES, which the checks below confirm.
_SETTING_WRITES = {
    '"ai_peers"', '"anthropic_api_key"', '"chat_model"', '"context_strategy"',
    '"default_model"', '"eval_answer_model"', '"eval_judge_model"',
    '"ollama_base_url"', '"provider_anthropic_enabled"',
    '"provider_ollama_enabled"', '"provider_openai_enabled"',
    '"rag_similarity_threshold"',
    'f"{name}_api_key"',  # a provider key - masked to <provider>_key_set
    'key',                # the generic admin write and the secret setter
}
_STATE_WRITES = {
    '"last_backup_file"', '"last_backup_timestamp"',
    '_health_key(peer_id)',
}


def test_every_settings_write_is_classified():
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    call = re.compile(r"(?<![\w.])(def\s+)?set_config\(\s*([^,]+?)\s*,")
    found = set()
    for path in app_dir.rglob("*.py"):
        for m in call.finditer(path.read_text(encoding="utf-8")):
            if not m.group(1):
                found.add(m.group(2))
    assert found == _SETTING_WRITES | _STATE_WRITES, (
        "a settings-table write changed - classify it: "
        f"new {sorted(found - _SETTING_WRITES - _STATE_WRITES)}, "
        f"gone {sorted((_SETTING_WRITES | _STATE_WRITES) - found)}")
    for w in _STATE_WRITES:
        if w.startswith('"'):
            assert is_state_key(w.strip('"')), w
    for w in _SETTING_WRITES:
        if w.startswith('"'):
            assert not is_state_key(w.strip('"')), w
    from app.peers import _health_key
    assert is_state_key(_health_key("any"))
