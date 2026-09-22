"""Guest spend: the two gaps filed against this template on 2026-08-28 and
closed 2026-09-21.

(a) Per-request INPUT was unbounded. Every other guest control counted
    something else - turns per conversation, tokens per answer, requests per
    day - while the body itself had no cap on the prompt, on a history
    message, or on the history's length, and context_strategy=warn truncates
    nothing. One guest turn could carry a megabyte to a metered provider.
(b) An unauthenticated caller PICKED THE MODEL. request.model was filled only
    when blank, and the provider is chosen by the model name's prefix with no
    enable check at the dispatch site, so a guest named the model that bills.
    The private forks close this with a guest model pin; the template had no
    equivalent - the one surface where a missing guard reaches everyone who
    deploys it.

These pin both at the route, for a guest and for a signed-in user, plus the
allow_model_selection enforcement that rode in with (b), plus the CALL sites -
a guard defined and never called is the shape check_daily_guest_budget
shipped in for a month.
"""
import inspect
from unittest.mock import patch

import pytest

from app.routers import chat as chat_mod


def _capturing_stream(captured):
    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        captured["model"] = model
        yield {"type": "text", "text": "ok"}
    return _stream


def _cfg(**overrides):
    """Open the admin half of the guest gate; answer the named keys; default
    the rest, which is what a fresh config table does."""
    def _get(key, default=None):
        if key == "guest_mode_enabled":
            return "true"
        return overrides.get(key, default)
    return _get


@pytest.fixture
def guest_open():
    with patch("app.routers.chat.guest_chat_available", return_value=True):
        yield


# -- (a) request size ----------------------------------------------------------

def test_a_guest_prompt_over_the_bound_is_refused_before_anything_runs(client, guest_open):
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 1000), \
         patch("app.routers.chat.get_config", side_effect=_cfg()), \
         patch("app.routers.chat.check_injection") as scan, \
         patch("app.routers.chat.stream_chat_events") as stream:
        r = client.post("/api/chat", json={"prompt": "x" * 1001})
    assert r.status_code == 413
    detail = r.json()["detail"]
    assert isinstance(detail, str) and "Message too long" in detail and "1,000" in detail
    scan.assert_not_called()          # the regex scan never sees an unbounded body
    stream.assert_not_called()


def test_the_bound_counts_the_history_the_client_sends_back(client, guest_open):
    history = [{"role": "user", "content": "y" * 600},
               {"role": "assistant", "content": "z" * 600}]
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 1000), \
         patch("app.routers.chat.get_config", side_effect=_cfg()), \
         patch("app.routers.chat.stream_chat_events") as stream:
        r = client.post("/api/chat", json={"prompt": "short", "history": history})
    assert r.status_code == 413
    stream.assert_not_called()


def test_a_signed_in_user_gets_the_wider_bound(client, admin_headers):
    captured = {}
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 1000), \
         patch("app.routers.chat.CHAT_MAX_INPUT_CHARS", 5000), \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "x" * 2000, "model": "test-model"},
                        headers=admin_headers)
        assert r.status_code == 200
        r = client.post("/api/chat", json={"prompt": "x" * 5001, "model": "test-model"},
                        headers=admin_headers)
        assert r.status_code == 413


def test_the_history_length_is_bounded_in_messages(client, admin_headers):
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}] * 3
    with patch("app.routers.chat.CHAT_MAX_HISTORY_MESSAGES", 5), \
         patch("app.routers.chat.stream_chat_events") as stream:
        r = client.post("/api/chat", json={"prompt": "hi", "history": history},
                        headers=admin_headers)
    assert r.status_code == 413 and "too long to send" in r.json()["detail"]
    stream.assert_not_called()


def test_zero_disables_a_bound(client, admin_headers):
    captured = {}
    with patch("app.routers.chat.CHAT_MAX_INPUT_CHARS", 0), \
         patch("app.routers.chat.CHAT_MAX_HISTORY_MESSAGES", 0), \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "x" * 300000, "model": "test-model"},
                        headers=admin_headers)
    assert r.status_code == 200


# -- (b) who picks the model ---------------------------------------------------

def test_a_guest_never_picks_the_model(client, guest_open):
    captured = {}
    with patch("app.routers.chat.GUEST_MODEL", ""), \
         patch("app.routers.chat.get_config", side_effect=_cfg(chat_model="operator-pin")), \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "claude-opus-5"})
    assert r.status_code == 200
    assert captured["model"] == "operator-pin"


def test_guest_model_wins_for_guests_only(client, guest_open, admin_headers):
    captured = {}
    with patch("app.routers.chat.GUEST_MODEL", "cheap-model"), \
         patch("app.routers.chat.get_config", side_effect=_cfg(chat_model="operator-pin")), \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "claude-opus-5"})
        assert r.status_code == 200 and captured["model"] == "cheap-model"
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "my-pick"},
                        headers=admin_headers)
        assert r.status_code == 200 and captured["model"] == "my-pick"


def test_allow_model_selection_off_pins_every_caller(client, admin_headers):
    """The setting used to hide the picker and nothing more - the
    allow_rag_toggle lesson, on the field that chooses the provider."""
    captured = {}
    with patch("app.routers.chat.get_config",
               side_effect=_cfg(chat_model="operator-pin", allow_model_selection="false")), \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "my-pick"},
                        headers=admin_headers)
    assert r.status_code == 200
    assert captured["model"] == "operator-pin"


def test_a_signed_in_caller_still_chooses_when_selection_is_allowed(client, admin_headers):
    captured = {}
    with patch("app.routers.chat.get_config", side_effect=_cfg(chat_model="operator-pin")), \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "my-pick"},
                        headers=admin_headers)
    assert r.status_code == 200 and captured["model"] == "my-pick"


def test_role_strings_count_toward_the_bound(client, guest_open):
    """Every string the body carries to the provider counts, the role field
    included - a body whose payload rides in `role` with empty content must
    not pass a bound stated in characters."""
    history = [{"role": "r" * 700, "content": ""}, {"role": "s" * 700, "content": ""}]
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 1000), \
         patch("app.routers.chat.get_config", side_effect=_cfg()), \
         patch("app.routers.chat.stream_chat_events") as stream:
        r = client.post("/api/chat", json={"prompt": "hi", "history": history})
    assert r.status_code == 413
    stream.assert_not_called()


def test_an_expired_session_gets_its_401_not_the_guest_413(client):
    """A presented-but-invalid token is a signed-in user about to be told 401 -
    the client refreshes on that and replays. Bounding them at the guest
    figure would answer 413 first, and nothing refreshes on a 413."""
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 1000), \
         patch("app.routers.chat.CHAT_MAX_INPUT_CHARS", 5000), \
         patch("app.routers.chat.stream_chat_events") as stream:
        r = client.post("/api/chat", json={"prompt": "x" * 2000},
                        headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
    stream.assert_not_called()


def test_the_call_sites_exist_in_the_handler():
    """The def is not the guard; the call is."""
    src = inspect.getsource(chat_mod.chat)
    assert "_check_request_size(request, guest=current_user is None" in src
    assert "request.model = GUEST_MODEL or _pinned" in src
    # size before scan: the injection regexes must never see an unbounded body
    assert src.index("_check_request_size(") < src.index("check_injection(request.prompt)")
