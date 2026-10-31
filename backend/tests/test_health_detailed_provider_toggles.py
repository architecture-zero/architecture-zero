"""The detailed health view reads the provider toggles saved in Settings
(2026-10-31).

GET /api/health/detailed listed an OpenAI row when ENABLE_OPENAI was true in
the environment and an Anthropic row when ENABLE_ANTHROPIC was, and marked
each ok by the key in the environment. The chat, the model picker and the
Settings tab read the toggle and the key saved in Settings first
(providers.get_provider_config), and the view's Ollama row has done the same
since 2026-10-30. So an operator who turned a provider on in Settings saw no
row for it, one who turned it off still saw one, and a key saved in Settings
read as missing.

FIX: the toggle saved in Settings decides the row both ways, and a key saved
in Settings reads ok. CONTROL: with nothing saved, the environment still
decides, as get_provider_config falls back to it.
"""
import pytest

from app.config import get_config, set_config

_KEYS = ("provider_ollama_enabled", "provider_openai_enabled",
         "provider_anthropic_enabled", "openai_api_key", "anthropic_api_key")


@pytest.fixture
def saved(monkeypatch):
    """Set rows saved in Settings for one test; put them back after. Ollama is
    saved off so the view never probes a server, and no key is in the
    environment unless a test puts one there."""
    before = {k: get_config(k, "") for k in _KEYS}
    set_config("provider_ollama_enabled", "false")
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from app import providers
    monkeypatch.setattr(providers, "ANTHROPIC_KEY", "")   # the import-time fallback
    yield set_config
    for k, v in before.items():
        set_config(k, v)


def _rows(client, headers) -> dict:
    r = client.get("/api/health/detailed", headers=headers)
    assert r.status_code == 200, r.text
    return {p["name"]: p for p in r.json()["providers"]}


def test_a_toggle_saved_on_shows_the_row(client, admin_headers, saved, monkeypatch):
    monkeypatch.setenv("ENABLE_OPENAI", "false")
    monkeypatch.setenv("ENABLE_ANTHROPIC", "false")
    saved("provider_openai_enabled", "true")
    saved("provider_anthropic_enabled", "true")
    rows = _rows(client, admin_headers)
    assert {"openai", "anthropic"} <= set(rows), rows
    assert rows["openai"]["ok"] is False and rows["anthropic"]["ok"] is False


def test_a_toggle_saved_off_hides_the_row(client, admin_headers, saved, monkeypatch):
    monkeypatch.setenv("ENABLE_OPENAI", "true")
    monkeypatch.setenv("ENABLE_ANTHROPIC", "true")
    saved("provider_openai_enabled", "false")
    saved("provider_anthropic_enabled", "false")
    rows = _rows(client, admin_headers)
    assert "openai" not in rows and "anthropic" not in rows, rows


def test_a_key_saved_in_settings_reads_ok(client, admin_headers, saved):
    saved("provider_openai_enabled", "true")
    saved("provider_anthropic_enabled", "true")
    saved("openai_api_key", "test-openai-value")
    saved("anthropic_api_key", "test-anthropic-value")
    rows = _rows(client, admin_headers)
    assert rows["openai"]["ok"] is True and rows["anthropic"]["ok"] is True, rows


def test_with_nothing_saved_the_environment_decides(client, admin_headers, saved, monkeypatch):
    """CONTROL: get_provider_config falls back to the environment."""
    saved("provider_openai_enabled", "")
    saved("provider_anthropic_enabled", "")
    saved("openai_api_key", "")
    saved("anthropic_api_key", "")
    monkeypatch.setenv("ENABLE_OPENAI", "true")
    monkeypatch.setenv("ENABLE_ANTHROPIC", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-value")
    rows = _rows(client, admin_headers)
    assert rows["openai"]["ok"] is True, rows
    assert rows["anthropic"]["ok"] is False, rows
