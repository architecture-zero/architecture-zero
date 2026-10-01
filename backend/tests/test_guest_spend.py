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
    Nothing pinned the model a guest answers on - and in a template a
    missing guard reaches everyone who deploys it.

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
    not pass a bound stated in characters. Since 2026-09-30 such a body is
    refused before the bound is read: a history role is user or assistant, and
    anything else is a 422 (it was this bound's 413 until then). Either way
    nothing reaches the model."""
    history = [{"role": "r" * 700, "content": ""}, {"role": "s" * 700, "content": ""}]
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 1000), \
         patch("app.routers.chat.get_config", side_effect=_cfg()), \
         patch("app.routers.chat.stream_chat_events") as stream:
        r = client.post("/api/chat", json={"prompt": "hi", "history": history})
    assert r.status_code == 422
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


def test_the_call_sites_exist_in_the_handler(chat_route_src):
    """The def is not the guard; the call is."""
    src = chat_route_src
    assert "_check_request_size(request, guest=current_user is None" in src
    assert "request.model = GUEST_MODEL or _pinned" in src
    # size before scan: the injection regexes must never see an unbounded body
    assert src.index("_check_request_size(") < src.index("check_injection(request.prompt)")


# -- what the reviews asked for (2026-09-28): the short fields, the guest figure, the record --

import importlib

_chat = importlib.import_module("app.routers.chat")


def _answers(*args, **kwargs):
    yield {"type": "text", "text": "ok"}


def _string_fields():
    """Every top-level field of the chat request that can carry a string,
    other than the prompt - read from the model, so a field added later is in
    this list the day it is added."""
    import typing
    out = []
    for name, field in _chat.ChatRequest.model_fields.items():
        kinds = typing.get_args(field.annotation) or (field.annotation,)
        if name != "prompt" and str in kinds:
            out.append(name)
    return out


def _field_limit(name):
    return _chat.CHAT_MAX_MODEL_CHARS if name == "model" else _chat.CHAT_MAX_FIELD_CHARS


def test_the_request_has_short_fields_to_bound():
    assert {"session_id", "model"} <= set(_string_fields())


@pytest.mark.parametrize("name", _string_fields())
def test_a_short_field_over_its_bound_is_refused_before_anything_runs(client, admin_headers, name):
    """None of these is part of the conversation, so the character bound does
    not count them - and the session id is written into a row on every
    answered turn. Unbounded, a megabyte rode in a field the bound said
    nothing about."""
    limit = _field_limit(name)
    with patch("app.routers.chat.check_injection") as scan, \
         patch("app.routers.chat.stream_chat_events") as stream:
        r = client.post("/api/chat", json={"prompt": "hi", name: "s" * (limit + 1)},
                        headers=admin_headers)
    assert r.status_code == 413
    detail = r.json()["detail"]
    assert isinstance(detail, str) and name in detail and str(limit) in detail
    assert "sss" not in detail                 # the figure, never the text
    scan.assert_not_called()
    stream.assert_not_called()


def test_short_fields_at_their_bounds_are_answered(client, admin_headers):
    with patch("app.routers.chat.stream_chat_events", side_effect=_answers):
        r = client.post("/api/chat",
                        json={"prompt": "hi", "model": "m" * _chat.CHAT_MAX_MODEL_CHARS,
                              "session_id": "s" * _chat.CHAT_MAX_FIELD_CHARS},
                        headers=admin_headers)
    assert r.status_code == 200


def test_each_short_field_bound_is_its_columns_width():
    """Not a preference: a value longer than its column is a row the database
    may refuse to write, after the answer has been paid for."""
    from app.models import Message
    columns = Message.__table__.c
    assert _chat.CHAT_MAX_FIELD_CHARS == columns.session.type.length == 255
    assert _chat.CHAT_MAX_MODEL_CHARS == columns.model.type.length == 100


@pytest.mark.parametrize("body,reason,figures", [
    ({"prompt": "PRIVATE" * 200}, "characters", {"chars": 1400, "limit": 1000}),
    ({"prompt": "hi", "history": [{"role": "user", "content": "PRIVATE"}] * 7},
     "history_messages", {"messages": 7, "limit": 5}),
    ({"prompt": "hi", "session_id": "PRIVATE" * 60}, "field",
     {"field": "session_id", "chars": 420, "limit": 255}),
])
def test_a_size_refusal_is_counted_and_logged_with_figures_only(client, admin_headers,
                                                               body, reason, figures):
    """The refusals were silent until 2026-09-28: no counter, no log line, so
    an operator could not see the bound firing. Each of the three leaves by
    the same exit, and the record carries the reason and the figures and
    never the caller's text."""
    with patch("app.routers.chat.CHAT_MAX_INPUT_CHARS", 1000), \
         patch("app.routers.chat.CHAT_MAX_HISTORY_MESSAGES", 5), \
         patch("app.routers.chat.increment") as count, \
         patch("app.routers.chat.log") as record:
        r = client.post("/api/chat", json=body, headers=admin_headers)
    assert r.status_code == 413
    assert "PRIVATE" not in r.text
    assert ("chat_request_refused_total",) in [c.args for c in count.call_args_list]
    refusals = [c for c in record.call_args_list if c.args[0] == "chat_request_refused"]
    assert len(refusals) == 1
    assert refusals[0].kwargs == {"reason": reason, "guest": False, **figures}
    assert "PRIVATE" not in repr(record.call_args_list)


def test_every_size_refusal_leaves_by_the_one_exit():
    """A refusal that raises by hand is a refusal nobody counts."""
    import inspect
    src = inspect.getsource(_chat._check_request_size)
    assert "raise HTTPException" not in src
    assert src.count("_refuse_oversize(") == 3


def test_a_guest_is_never_given_more_than_a_signed_in_caller():
    """With the guest figure switched off (0) a guest used to be bound by
    nothing at all, while a signed-in caller was still held to theirs."""
    from fastapi import HTTPException
    big = _chat.ChatRequest(prompt="x" * 1001)
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 0), \
         patch("app.routers.chat.CHAT_MAX_INPUT_CHARS", 1000):
        with pytest.raises(HTTPException) as refused:
            _chat._check_request_size(big, guest=True)
    assert refused.value.status_code == 413
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 5000), \
         patch("app.routers.chat.CHAT_MAX_INPUT_CHARS", 1000):
        with pytest.raises(HTTPException):
            _chat._check_request_size(big, guest=True)    # a guest figure ABOVE the other is not honoured
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 1000), \
         patch("app.routers.chat.CHAT_MAX_INPUT_CHARS", 0):
        with pytest.raises(HTTPException):
            _chat._check_request_size(big, guest=True)    # the guest figure holds with the other off
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 0), \
         patch("app.routers.chat.CHAT_MAX_INPUT_CHARS", 0):
        _chat._check_request_size(big, guest=True)        # both off: no bound, as documented


def test_the_bounds_are_said_at_startup_and_loudly_when_one_is_off():
    """A bound at 0 is silent everywhere else."""
    with patch("app.routers.chat.log") as said, patch("app.routers.chat.log_error") as loud:
        _chat._say_the_bounds()
    loud.assert_not_called()
    assert said.call_args.args == ("chat_request_bounds",)
    assert said.call_args.kwargs == {"off": [], "chat_chars": _chat.CHAT_MAX_INPUT_CHARS,
                                     "guest_chars": _chat.GUEST_MAX_INPUT_CHARS,
                                     "history_messages": _chat.CHAT_MAX_HISTORY_MESSAGES}
    with patch("app.routers.chat.GUEST_MAX_INPUT_CHARS", 0), \
         patch("app.routers.chat.log") as said, patch("app.routers.chat.log_error") as loud:
        _chat._say_the_bounds()
    said.assert_not_called()
    assert loud.call_args.args == ("chat_request_bounds",)
    assert loud.call_args.kwargs["off"] == ["guest_chars"]
    import inspect
    src = inspect.getsource(_chat)
    assert "\n_say_the_bounds()\n" in src                # and it is CALLED, at import
