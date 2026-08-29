"""ROUND 6 - the defects the sixth adversarial review found, pinned.

Every test here exists because a control reported success while doing nothing,
or because a fix established an invariant and then broke it somewhere else.
That is the shape this repo keeps producing, and the reason these are tests
rather than notes: the round-6 review found that NOTHING in the round-5 fix
commit was exercised by any assertion in the suite, so "554 passed" was true and
said nothing about the code that had just changed.

1. THE THRESHOLD VALIDATOR was hardened on PATCH /api/admin/config and the
   commit that did it verified the work by hand, live, once. These are the four
   inputs that verification named, so the claim survives the next change.

2. PUT /api/settings - the endpoint the admin UI actually calls - had the
   ORIGINAL defect the whole time: `if 0 <= value <= 1:` with no `else`, while
   the log line and the success response ran unconditionally. Out of range was
   discarded and answered "Saved".

3. rerank_remote_url is an arbitrary destination for candidate chunk text and
   sat behind manage_system, three lines below a comment asserting that no
   config write could start egress. ollama_base_url - the identical capability
   - requires owner.

4. allow_rag_toggle was enforced NOWHERE. It lived in the config defaults, the
   admin write allowlist and the /api/config read-back, and never in a request
   path, so an operator who turned the control off had turned off a checkbox in
   somebody else's browser.
"""
from unittest.mock import patch

import pytest

from app import config


# -- 1. The threshold validator on PATCH /api/admin/config -------------------
# The four inputs the round-5 commit body claimed it had verified live.

def test_threshold_rejects_a_json_boolean(client, admin_headers):
    """bool is a subclass of int, so float(True) is 1.0 and sails through a
    range check - then str(True) stores "True", which float() cannot parse on
    the chat path. The validator would have reintroduced the exact outage it
    was written to prevent."""
    r = client.patch("/api/admin/config",
                     json={"rag_similarity_threshold": True},
                     headers=admin_headers)
    assert r.status_code == 400, r.text


def test_threshold_rejects_an_oversized_integer(client, admin_headers):
    """A JSON integer too large for a float raises OverflowError, not
    ValueError. Uncaught, that is a 500 where the caller deserves a 400."""
    r = client.patch("/api/admin/config",
                     json={"rag_similarity_threshold": 10 ** 400},
                     headers=admin_headers)
    assert r.status_code == 400, r.text


def test_threshold_rejects_nan(client, admin_headers):
    """float("NaN") succeeds and every comparison against it is False, so NaN
    passes a range check by failing it in the wrong direction."""
    r = client.patch("/api/admin/config",
                     json={"rag_similarity_threshold": "NaN"},
                     headers=admin_headers)
    assert r.status_code == 400, r.text


def test_threshold_still_accepts_a_real_value(client, admin_headers):
    """The contract cuts both ways: a validator that refuses everything is not
    a fix. Restored afterwards - the client fixture is session-scoped."""
    original = client.get("/api/admin/config", headers=admin_headers).json() \
        .get("rag_similarity_threshold", "")
    try:
        r = client.patch("/api/admin/config",
                         json={"rag_similarity_threshold": 0.5},
                         headers=admin_headers)
        assert r.status_code == 200, r.text
    finally:
        # RESTORE THROUGH THE CONFIG LAYER, not the API. The stored value is
        # legitimately blank on a fresh instance (the key exists with an empty
        # default and the runtime falls back), and the endpoint's own validator
        # correctly refuses "" - so an API restore silently skipped, and this
        # test left 0.5 behind in a session-scoped database for every test that
        # runs after it. The first version of this teardown raised ValueError
        # instead; the second swallowed it and did nothing while the docstring
        # still said "restored afterwards". Both are the same class this file
        # exists to pin, committed in the file pinning it.
        config.set_config("rag_similarity_threshold", original)


# -- 2. PUT /api/settings stopped discarding in silence ----------------------

def test_settings_put_refuses_an_out_of_range_threshold(client, admin_headers):
    """THE BUG: this answered 200 with "Saved - changes take effect
    immediately" and wrote nothing. The Pydantic field is a bare float with no
    ge/le, so there was no 422 either, and the input went on displaying the
    rejected value under a green banner."""
    r = client.put("/api/settings",
                   json={"rag_similarity_threshold": 5.0},
                   headers=admin_headers)
    assert r.status_code == 400, r.text
    assert "between 0 and 1" in r.json()["detail"]


def test_settings_put_accepts_zero(client, admin_headers):
    """Zero is a legitimate threshold and the UI advertises min="0". The client
    sent `parseFloat(x) || 0.4`, so typing 0 silently sent 0.4 - the server must
    at least accept the value the control claims to offer."""
    original = client.get("/api/settings", headers=admin_headers).json()
    try:
        r = client.put("/api/settings",
                       json={"rag_similarity_threshold": 0.0},
                       headers=admin_headers)
        assert r.status_code == 200, r.text
        assert float(r.json()["rag_similarity_threshold"]) == 0.0
    finally:
        try:
            restore = float(original["rag_similarity_threshold"])
        except (TypeError, ValueError, KeyError):
            restore = None
        if restore is not None:
            client.put("/api/settings",
                       json={"rag_similarity_threshold": restore},
                       headers=admin_headers)


# -- 3. rerank_remote_url is owner-gated -------------------------------------

_MS_USER = {"username": "round6_ms", "password": "Round6Ms1", "role": "admin"}


@pytest.fixture
def manage_system_headers(client, admin_headers):
    """A NON-owner account holding manage_system.

    This is the principal the finding is about: manage_system is a permission an
    Owner can delegate, and it was enough to repoint corpus-text egress.
    """
    r = client.post("/api/users", json=_MS_USER, headers=admin_headers)
    assert r.status_code in (200, 201, 409), r.text
    users = client.get("/api/users", headers=admin_headers).json()
    rows = users if isinstance(users, list) else users.get("users", [])
    uid = next(u["id"] for u in rows if u["username"] == _MS_USER["username"])
    g = client.patch(f"/api/users/{uid}/permissions",
                     json={"permissions": ["chat", "manage_system"]},
                     headers=admin_headers)
    assert g.status_code == 200, g.text
    login = client.post("/api/auth/login",
                        json={"username": _MS_USER["username"],
                              "password": _MS_USER["password"]})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_manage_system_cannot_repoint_rerank_egress(client, manage_system_headers):
    """The whole finding in one assertion: a delegated permission must not be
    able to choose where candidate chunk text is POSTed."""
    r = client.patch("/api/admin/config",
                     json={"rerank_remote_url": "http://attacker.example/rerank"},
                     headers=manage_system_headers)
    assert r.status_code == 403, r.text
    assert "rerank_remote_url" in r.json()["detail"]


def test_owner_can_still_set_rerank_remote_url(client, admin_headers):
    """The seam's whole point is that an operator flip is a config change, not
    a restart. Gating it must not close it."""
    original = client.get("/api/admin/config", headers=admin_headers).json() \
        .get("rerank_remote_url", "")
    try:
        r = client.patch("/api/admin/config",
                         json={"rerank_remote_url": "http://localhost:9999/rerank"},
                         headers=admin_headers)
        assert r.status_code == 200, r.text
    finally:
        client.patch("/api/admin/config",
                     json={"rerank_remote_url": original},
                     headers=admin_headers)


def test_owner_only_key_does_not_partially_write(client, manage_system_headers):
    """A refused body must not have half-landed. The owner check runs before
    the write loop for the same reason the unknown-key check does."""
    r = client.patch("/api/admin/config",
                     json={"instance_name": "Should Not Land",
                           "rerank_remote_url": "http://attacker.example/rerank"},
                     headers=manage_system_headers)
    assert r.status_code == 403, r.text
    assert config.get_config("instance_name", "") != "Should Not Land"


# -- 4. allow_rag_toggle is enforced on the request path ---------------------

def _cfg(allow_toggle, default_rag):
    """Guest gate open, with the two retrieval settings under test."""
    def _inner(key, default=None):
        if key == "guest_mode_enabled":
            return "true"
        if key == "allow_rag_toggle":
            return allow_toggle
        if key == "default_rag_enabled":
            return default_rag
        return default
    return _inner


def _capturing_stream(captured):
    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        captured["messages"] = messages
        captured["system_prompt"] = system_prompt
        yield {"type": "text", "text": "ok"}
    return _stream


def test_disabled_toggle_ignores_an_explicit_use_rag(client):
    """THE BYPASS. The operator turned the control off and set retrieval on.
    A caller sending use_rag=false was honoured anyway, because the setting was
    read by the admin panel and by nothing else. It became reachable the moment
    the client started sending an explicit value on a plain toggle tap - which a
    guest can do, since a guest never learns the setting exists.
    """
    captured = {}
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config",
               side_effect=_cfg(allow_toggle="false", default_rag="true")), \
         patch("app.routers.chat.stream_chat_events",
               side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "what is this instance?",
                                           "model": "test-model",
                                           "use_rag": False})
    assert r.status_code == 200, r.text
    system = captured["messages"][0]["content"]
    assert "TURNED OFF" not in system, \
        "an explicit use_rag=false overrode the operator's disabled toggle"


def test_enabled_toggle_still_honours_an_explicit_use_rag(client):
    """The other direction, and the reason this is not just a hardcode: where
    the operator LEFT the toggle on, the caller's choice still decides."""
    captured = {}
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config",
               side_effect=_cfg(allow_toggle="true", default_rag="true")), \
         patch("app.routers.chat.stream_chat_events",
               side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "what is this instance?",
                                           "model": "test-model",
                                           "use_rag": False})
    assert r.status_code == 200, r.text
    system = captured["messages"][0]["content"]
    assert "TURNED OFF" in system, \
        "a permitted toggle stopped reaching the answer path"


def test_public_auth_config_delivers_the_toggle_setting(client):
    """The client half. /api/config is authenticated, so a guest could never
    learn this and rendered the toggle unconditionally. The server enforces it
    either way; this only stops the UI offering a control that does nothing."""
    body = client.get("/api/auth/config").json()
    assert "allow_rag_toggle" in body, body
    assert isinstance(body["allow_rag_toggle"], bool), body
