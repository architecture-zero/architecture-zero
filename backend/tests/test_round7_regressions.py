"""ROUND 7 - what the seventh adversarial review found, pinned.

The headline one is not subtle and had shipped for a long time: selecting the
`summarize` context strategy PERMANENTLY DELETED the stored conversation. The
admin control says "compress context silently" and the chat banner says "Older
messages were summarized"; the code called clear_session(), the same primitive
DELETE /api/history/{id} uses, and wrote back only a summary row plus the last
six turns. Everything older was gone, on every over-limit turn, with no
confirmation and no undo.

The deletion was never load-bearing: what reaches the model is a list rebuilt in
memory, and the stored rows are not consulted for context at all.

The rest are smaller and all share this codebase's recurring shape - a write path
that reports something other than what it did.
"""
from unittest.mock import patch

import pytest

from app import config


def _capturing_stream(captured):
    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        captured["messages"] = messages
        yield {"type": "text", "text": "ok"}
    return _stream


def _cfg(strategy):
    """Config with the context strategy under test and the guest gate open."""
    def _inner(key, default=None):
        if key == "context_strategy":
            return strategy
        if key == "guest_mode_enabled":
            return "true"
        if key == "default_rag_enabled":
            return "false"
        return default
    return _inner


# -- The HIGH: summarize must not delete stored history ----------------------

def _long_history(turns=40):
    """Enough transcript to cross MAX_CONTEXT_TOKENS (default 6000, estimated
    as total characters // 4)."""
    out = []
    for i in range(turns):
        out.append({"role": "user", "content": f"question {i} " + ("x" * 400)})
        out.append({"role": "assistant", "content": f"answer {i} " + ("y" * 400)})
    return out


def test_summarize_does_not_delete_stored_history(client, admin_headers):
    """THE BUG: this deleted every stored row for the session and wrote back
    only a summary plus the last six turns. A user's conversation vanished the
    next time they opened it, and the product called that 'summarized'."""
    session = "r7-summarize-keeps"

    # Two real stored turns to lose.
    for prompt in ("first question", "second question"):
        with patch("app.routers.chat.guest_chat_available", return_value=True), \
             patch("app.routers.chat.get_config", side_effect=_cfg("warn")), \
             patch("app.routers.chat.stream_chat_events",
                   side_effect=_capturing_stream({})):
            r = client.post("/api/chat", json={"prompt": prompt, "model": "test-model",
                                               "session_id": session, "use_rag": False},
                            headers=admin_headers)
            assert r.status_code == 200, r.text

    before = client.get(f"/api/history/{session}", headers=admin_headers).json()["messages"]
    assert len(before) == 4, before

    # Now a turn that trips the summarize branch.
    captured = {}
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_cfg("summarize")), \
         patch("app.routers.chat._summarize_history", return_value="a summary"), \
         patch("app.routers.chat.stream_chat_events",
               side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "the over-limit turn",
                                           "model": "test-model",
                                           "session_id": session,
                                           "history": _long_history(),
                                           "use_rag": False},
                        headers=admin_headers)
        assert r.status_code == 200, r.text

    after = client.get(f"/api/history/{session}", headers=admin_headers).json()["messages"]
    contents = [m["content"] for m in after]

    # The original turns are still there.
    assert any("first question" in c for c in contents), contents
    assert any("second question" in c for c in contents), contents
    # And nothing was replaced by a summary row.
    assert not any("[CONTEXT SUMMARY]" in c for c in contents), contents
    # The new turn was appended, not substituted.
    assert any("the over-limit turn" in c for c in contents), contents
    assert len(after) >= len(before), (len(before), len(after))


def test_summarize_still_compresses_what_the_model_sees(client, admin_headers):
    """The feature must keep working - the fix removes the DB write, not the
    context management. A test that only proved 'nothing was deleted' would
    also pass if summarize had been disabled outright."""
    captured = {}
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_cfg("summarize")), \
         patch("app.routers.chat._summarize_history", return_value="THE SUMMARY"), \
         patch("app.routers.chat.stream_chat_events",
               side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "over limit again",
                                           "model": "test-model",
                                           "session_id": "r7-summarize-context",
                                           "history": _long_history(),
                                           "use_rag": False},
                        headers=admin_headers)
        assert r.status_code == 200, r.text

    sent = captured["messages"]
    blob = " ".join(m.get("content", "") for m in sent)
    assert "THE SUMMARY" in blob, "the summary never reached the model"
    # The compressed context is far smaller than the raw transcript it replaced.
    assert len(sent) < 40, f"context was not compressed: {len(sent)} messages"


# -- PUT /api/settings must not half-apply then refuse -----------------------

def test_settings_refusal_does_not_partially_write(client, admin_headers):
    """THE BUG, and it was introduced by the fix for the silent discard: the
    range check sat at the BOTTOM of the handler, after eight set_config calls
    had already committed. The admin UI posts the whole body at once, so one bad
    threshold wrote the providers, base URL, keys and model and THEN answered
    400 - a partial write reported as a refusal, which is worse than the silent
    discard it replaced."""
    before = client.get("/api/settings", headers=admin_headers).json()
    original_model = before["default_model"]

    r = client.put("/api/settings",
                   json={"default_model": "should-not-land",
                         "rag_similarity_threshold": 5.0},
                   headers=admin_headers)
    assert r.status_code == 400, r.text

    after = client.get("/api/settings", headers=admin_headers).json()
    assert after["default_model"] == original_model, (
        "a refused body still wrote default_model")


# -- The tail delete must report what it deleted -----------------------------

def test_tail_delete_reports_rows_actually_removed(client, admin_headers):
    """It answered `deleted: count` straight from the request, so asking to trim
    two rows off a one-row session was told two were deleted."""
    session = "r7-tail-count"
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_cfg("warn")), \
         patch("app.routers.chat.stream_chat_events",
               side_effect=_capturing_stream({})):
        client.post("/api/chat", json={"prompt": "only turn", "model": "test-model",
                                       "session_id": session, "use_rag": False},
                    headers=admin_headers)

    stored = client.get(f"/api/history/{session}", headers=admin_headers).json()["messages"]
    assert len(stored) == 2, stored

    r = client.delete(f"/api/history/{session}/tail?count=10", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == 2, body
    assert body["requested"] == 10, body


def test_tail_delete_on_empty_session_reports_zero(client, admin_headers):
    r = client.delete("/api/history/r7-nothing-here/tail?count=3", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 0, r.json()


# -- suggestions: the error string promised strings, the check only tested list

def test_suggestions_rejects_a_non_string_element(client, admin_headers):
    """The message said "a list of strings" and the check only tested LIST, so a
    list containing a number passed validation and was then silently dropped
    element-by-element in the write loop - the element-level survivor of the
    silent-drop class this endpoint was cured of."""
    r = client.patch("/api/admin/config",
                     json={"suggestions": ["fine", 42, "also fine"]},
                     headers=admin_headers)
    assert r.status_code == 400, r.text
    assert "string" in r.json()["detail"].lower()


def test_suggestions_stores_strings_verbatim(client, admin_headers):
    """The write loop must not quietly rewrite what the operator typed."""
    original = config.get_config("suggestions", "")
    try:
        r = client.patch("/api/admin/config",
                         json={"suggestions": ["  padded  ", "plain"]},
                         headers=admin_headers)
        assert r.status_code == 200, r.text
        assert "  padded  " in config.get_config("suggestions", ""), \
            "stored content was rewritten"
    finally:
        config.set_config("suggestions", original)
