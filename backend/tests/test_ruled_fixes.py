"""The fixes ruled on 2026-09-21 (the pilot month's item 6), template first.

Four of them land on this surface:
  - the password short-circuit: a sign-in-only account's refusal now pays the
    bcrypt round, so timing no longer says which usernames have no password;
  - the admin MFA-reset door costs the caller's own password (self-target is
    allowed WITH it, and logged - there is no self-service disable route to
    send a sole Owner to);
  - the settings route costs the caller's own password (it holds the egress
    address and every provider key);
  - the dispatch gate: a caller-chosen model whose provider this instance
    does not offer is refused at the chat route, and the picker and the gate
    share one predicate.
"""
from unittest.mock import patch

import pytest

from app import jwt_auth
from app.jwt_auth import unusable_password_hash, verify_password

OWNER_PW = "AdminPass1"


def _mock_stream_events(messages, model, tools=None, system_prompt="", max_tokens=1024):
    yield {"type": "text", "text": "ok"}


# -- the password short-circuit ------------------------------------------------

def test_a_sign_in_only_account_pays_the_bcrypt_round_before_refusing():
    with patch.object(jwt_auth.bcrypt, "checkpw", wraps=jwt_auth.bcrypt.checkpw) as check:
        assert verify_password("anything", unusable_password_hash("google")) is False
    assert check.call_count == 1          # the dummy round, not a bare False
    hashed = jwt_auth.hash_password("RealPass1!x")
    with patch.object(jwt_auth.bcrypt, "checkpw", wraps=jwt_auth.bcrypt.checkpw) as check:
        assert verify_password("RealPass1!x", hashed) is True
    assert check.call_count == 1          # a real account still pays exactly one


# -- the admin MFA-reset door ----------------------------------------------------

def test_mfa_reset_costs_the_callers_password(client, admin_headers):
    me = client.get("/api/auth/me", headers=admin_headers).json()["id"]
    with patch("app.routers.users.disable_mfa") as disable:
        r = client.post(f"/api/admin/users/{me}/mfa-reset", headers=admin_headers)
        assert r.status_code == 400 and "current password" in r.json()["detail"].lower()
        r = client.post(f"/api/admin/users/{me}/mfa-reset", headers=admin_headers,
                        json={"current_password": "not-the-password"})
        assert r.status_code == 400 and "incorrect" in r.json()["detail"].lower()
        disable.assert_not_called()
        # self-target, WITH the step-up: allowed - the same door a self-service
        # disable would be - and the body still needs the string 400 shape,
        # never a 422 (the admin panel renders `detail` as text)
        r = client.post(f"/api/admin/users/{me}/mfa-reset", headers=admin_headers,
                        json={"current_password": OWNER_PW})
        assert r.status_code == 200, r.text
        disable.assert_called_once_with(me)
    from app.users import reset_failed_attempts
    reset_failed_attempts(me)            # the wrong password above counted


# -- the settings route ------------------------------------------------------------

def test_settings_write_costs_the_callers_password_and_never_stores_it(client, admin_headers):
    from app.config import get_config
    r = client.put("/api/settings", headers=admin_headers, json={"default_model": "x"})
    assert r.status_code == 400 and "current password" in r.json()["detail"].lower()
    before = client.get("/api/settings", headers=admin_headers).json()["default_model"]
    r = client.put("/api/settings", headers=admin_headers,
                   json={"default_model": before, "current_password": OWNER_PW})
    assert r.status_code == 200, r.text
    assert get_config("current_password", "") == ""     # written nowhere
    assert "current_password" not in r.text


# -- the dispatch gate ---------------------------------------------------------------

def test_a_caller_chosen_model_on_an_unoffered_provider_is_refused(client, admin_headers):
    with patch("app.routers.chat.offered_providers", return_value={"ollama"}), \
         patch("app.routers.chat.stream_chat_events", side_effect=_mock_stream_events) as stream:
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "claude-opus-5"},
                        headers=admin_headers)
        assert r.status_code == 400
        assert "anthropic" in r.json()["detail"] and "not enabled" in r.json()["detail"]
        stream.assert_not_called()
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "qwen3:8b"},
                        headers=admin_headers)
        assert r.status_code == 200


def test_the_operators_own_pin_is_never_gated(client, admin_headers):
    """The pin is trusted config: an operator whose pin names a provider the
    predicate does not list gets the model they configured, not a refusal
    (the eval judge and every other pinned dispatch depend on the same
    trust)."""
    def _cfg(key, default=None):
        if key == "chat_model":
            return "claude-opus-5"
        if key == "guest_mode_enabled":
            return "true"
        return default
    seen = {}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        seen["model"] = model
        yield {"type": "text", "text": "ok"}

    with patch("app.routers.chat.offered_providers", return_value={"ollama"}) as offered, \
         patch("app.routers.chat.get_config", side_effect=_cfg), \
         patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.stream_chat_events", side_effect=_stream):
        r = client.post("/api/chat", json={"prompt": "Hi"}, headers=admin_headers)   # no model sent
        assert r.status_code == 200 and seen["model"] == "claude-opus-5"
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "claude-opus-5"})   # a guest
        assert r.status_code == 200 and seen["model"] == "claude-opus-5"
        offered.assert_not_called()


def test_the_picker_and_the_gate_share_one_predicate(client, admin_headers):
    from app.routers import settings as settings_mod
    from app.routers import chat as chat_mod
    from app import providers
    assert settings_mod.offered_providers is providers.offered_providers
    assert chat_mod.offered_providers is providers.offered_providers
    with patch("app.routers.settings.offered_providers", return_value={"anthropic"}), \
         patch("app.routers.settings._fetch_anthropic_models", return_value=[]):
        groups = client.get("/api/models", headers=admin_headers).json()["groups"]
    assert [g["provider"] for g in groups] == ["anthropic"]   # ollama hidden when not offered


def test_offered_providers_follows_the_toggles_and_the_keys(monkeypatch):
    from app import providers
    monkeypatch.setattr(providers, "get_provider_config", lambda: {
        "enabled_providers": ["ollama"], "anthropic_configured": True})
    monkeypatch.setattr(providers, "compat_key_configured", lambda name: name == "groq")
    assert providers.offered_providers() == {"ollama", "anthropic", "groq"}
