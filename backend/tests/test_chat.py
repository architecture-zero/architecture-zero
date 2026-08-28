from unittest.mock import patch


def _mock_stream_events(messages, model, tools=None, system_prompt="", max_tokens=1024):
    # The chat endpoint streams via stream_chat_events (agentic loop), which yields
    # normalized event dicts, not bare strings.
    yield {"type": "text", "text": "Hello"}
    yield {"type": "text", "text": " world"}


def _guest_enabled_cfg(key, default=None):
    """Selective config: turn on the admin half of the guest gate, leave the rest at defaults."""
    if key == "guest_mode_enabled":
        return "true"
    return default


def test_chat_streams_sse(client):
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_mock_stream_events):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "test-model"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert "Hello" in r.text
    assert "[DONE]" in r.text


def test_chat_guest_blocked_when_disabled(client):
    # Guest access is DOUBLE-latched: the env half (ALLOW_GUEST_MODE) is on in
    # the test env, but with the admin-config half off the instance must still
    # refuse unauthenticated chat - a stray config row alone can't open the
    # site, and neither can the env var alone.
    with patch("app.routers.chat.guest_chat_available", return_value=False), \
         patch("app.routers.chat.get_config", return_value="false"):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "test-model"})
    assert r.status_code == 403


def test_chat_expired_token_gets_401_not_guest_403(client):
    """A presented-but-invalid/expired token must answer 401 - the client's
    silent-refresh signal - not the guest 403, which the 401-keyed refresh
    never catches and which leaves an idle session dead on its first message.
    A token-less guest still gets the 403 (previous test)."""
    with patch("app.routers.chat.guest_chat_available", return_value=False), \
         patch("app.routers.chat.get_config", return_value="false"):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "test-model"},
                        headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_chat_guest_turn_limit(client):
    history = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_mock_stream_events):
        r = client.post("/api/chat", json={
            "prompt": "one more",
            "model": "test-model",
            "history": history,
        })
    assert r.status_code == 429


def _capturing_stream(captured):
    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        captured["messages"] = messages
        captured["system_prompt"] = system_prompt
        yield {"type": "text", "text": "ok"}
    return _stream


def test_chat_rag_off_discloses_retrieval_status(client):
    # With use_rag=False the system prompt must tell the model RAG is off, so a
    # knowledge question gets "RAG is switched off" instead of a misleading
    # "not on record" - the miss is otherwise indistinguishable from a
    # retrieval failure.
    captured = {}
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)):
        r = client.post("/api/chat", json={"prompt": "what did the last eval round measure?",
                                           "model": "test-model"})
    assert r.status_code == 200
    system = captured["messages"][0]
    assert system["role"] == "system"
    assert "RETRIEVAL STATUS" in system["content"]
    assert "TURNED OFF" in system["content"]
    # Prompt-cache contract: the system_prompt kwarg is the STABLE core -
    # conditional suffixes like the RAG-off notice must ride only the system
    # message, or toggling RAG would bust the cached prefix.
    assert captured["system_prompt"]
    assert "RETRIEVAL STATUS" not in captured["system_prompt"]
    assert system["content"].startswith(captured["system_prompt"])


def test_chat_followup_rewrite_touches_only_the_retrieval_query(client):
    """The follow-up rewrite (routing.resolve_followup) must reach retrieve() ONLY.
    The saved history and the model's own user turn keep the user's REAL words - if a
    future edit routes the rewritten query into save_message or the prompt, saved
    history and model input are corrupted, so lock the invariant down here."""
    captured = {}
    saved = []

    def _fake_retrieve(query, department=None, top_k=None, user_level=None, stats=None):
        captured["retrieval_query"] = query
        return []

    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)), \
         patch("app.rerank.retrieve", side_effect=_fake_retrieve), \
         patch("app.routers.chat.save_message", side_effect=lambda *a, **k: saved.append(a)):
        r = client.post("/api/chat", json={
            "prompt": "current",
            "model": "test-model",
            "use_rag": True,
            "history": [{"role": "user", "content": "give me a review of my project"},
                        {"role": "assistant", "content": "..."}],
        })
    assert r.status_code == 200
    # retrieval saw the EXPANDED query (the bare token alone retrieves noise)...
    assert captured["retrieval_query"] != "current"
    assert "review of my project" in captured["retrieval_query"]
    # ...while the saved user message kept the real words...
    assert any("current" in a and "user" in a for a in saved), saved
    # ...and so did the user turn handed to the model.
    user_turns = [m for m in captured["messages"] if m.get("role") == "user"]
    assert user_turns and user_turns[-1]["content"] == "current", user_turns


def test_chat_retrieval_runs_off_the_event_loop(client):
    """retrieve() can be CPU-bound and slow when the local rerank leg runs -
    tens of seconds on a loaded box. Called directly from the async handler it
    blocks the whole uvicorn loop for that time, so one chat turn freezes
    every other request to the instance (health checks and status polls
    included) while the cross-encoder grinds through the pool.

    The assertion is not about thread names: it asks whether an event loop is
    RUNNING in the calling thread. On the loop, get_running_loop() succeeds; in
    an executor thread it raises RuntimeError. So this fails the moment someone
    unwraps the call, which is the regression worth catching."""
    import asyncio
    seen = {}

    def _fake_retrieve(query, department=None, top_k=None, user_level=None, stats=None):
        try:
            asyncio.get_running_loop()
            seen["on_event_loop"] = True
        except RuntimeError:
            seen["on_event_loop"] = False
        return []

    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_mock_stream_events), \
         patch("app.rerank.retrieve", side_effect=_fake_retrieve):
        r = client.post("/api/chat", json={"prompt": "what did the last eval round measure?",
                                           "model": "test-model", "use_rag": True})
    assert r.status_code == 200
    assert seen.get("on_event_loop") is False, (
        "retrieve() ran on the asyncio event loop - a long CPU call there stalls "
        "every other request to this backend")


def test_chat_rag_on_omits_retrieval_status(client):
    captured = {}
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)), \
         patch("app.rerank.retrieve", return_value=[]):
        r = client.post("/api/chat", json={"prompt": "what did the last eval round measure?",
                                           "model": "test-model", "use_rag": True})
    assert r.status_code == 200
    system = captured["messages"][0]
    assert system["role"] == "system"
    assert "RETRIEVAL STATUS" not in system["content"]


# -- Empty-final-answer guard -------------------------------------------------
# A model can genuinely stop without text - most often right after a
# full-length tool round. Provider stream errors raise loudly upstream, so
# anything still producing an empty final answer gets ONE nudged retry, then
# an honest fallback line - never a blank bubble.

def test_chat_empty_answer_retries_once(client):
    calls = {"n": 0}
    captured = {}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        calls["n"] += 1
        if calls["n"] == 1:
            return  # empty generator - the model "answered" with nothing
            yield  # pragma: no cover
        captured["retry_messages"] = list(messages)
        yield {"type": "text", "text": "recovered"}

    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_stream):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "test-model",
                                           "session_id": "s-empty-retry"})
    assert r.status_code == 200
    assert "recovered" in r.text
    assert calls["n"] == 2
    # The nudge rides as a user message and is recognizably synthetic.
    nudge = captured["retry_messages"][-1]
    assert nudge["role"] == "user"
    assert nudge["content"].startswith("(system note:")


def test_chat_empty_answer_fallback_after_tool_round(client):
    calls = {"n": 0}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"type": "tool_call", "id": "t1", "name": "fake_tool", "args": {}}
        # rounds 2 (post-tool) and 3 (the retry) both yield nothing

    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.execute_tool", return_value="{}"):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "test-model",
                                           "session_id": "s-empty-fallback"})
    assert r.status_code == 200
    assert "could not produce an answer" in r.text
    assert calls["n"] == 3


# -- The guard must key on the FINAL round ------------------------------------

def test_preamble_then_empty_final_round_is_retried(client):
    """A round-1 preamble ('Checking the knowledge base now...') followed by
    an empty final round is the same dangling non-answer the guard exists
    for - it just has chars>0, so keying on the cumulative response missed
    it."""
    calls = {"n": 0}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"type": "text", "text": "Checking the knowledge base now..."}
            yield {"type": "tool_call", "id": "t1", "name": "fake_tool", "args": {}}
        elif calls["n"] == 2:
            return          # empty final round
        else:
            yield {"type": "text", "text": "Here is the real answer."}

    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.execute_tool", return_value="{}"):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "test-model",
                                           "session_id": "s-preamble-empty"})
    assert r.status_code == 200
    assert "Here is the real answer." in r.text
    assert calls["n"] == 3, "the retry must fire on an empty FINAL round"


# -- The guest posture is ONE expression, read three ways ---------------------

def test_the_login_screen_the_config_and_the_gate_agree_on_guest_access(
        client, admin_headers):
    """Three surfaces read "may an anonymous caller chat": the public
    /api/auth/config (so the login screen knows whether to offer a guest door),
    the authenticated /api/config, and the chat gate itself.

    They were two separately-written copies of the same boolean before the
    login screen needed a third. A UI that computes this independently
    eventually offers a door the server refuses - which is a support ticket
    that looks like a bug in authentication.

    Pinned two-sided at the one seam they now share.
    """
    from app.routers import chat as chat_mod, system as system_mod, auth as auth_mod

    for available in (True, False):
        with patch.object(chat_mod, "guest_chat_available", return_value=available), \
             patch.object(system_mod, "guest_chat_available", return_value=available), \
             patch("app.runtime_config.guest_chat_available", return_value=available):
            public = client.get("/api/auth/config").json()
            authed = client.get("/api/config", headers=admin_headers).json()
            assert public["guest_mode_enabled"] is available, public
            assert authed["guest_mode_enabled"] is available, authed

            # And the gate itself lands the same way for a token-less caller.
            r = client.post("/api/chat", json={"prompt": "Hi", "model": "test-model"})
            if available:
                assert r.status_code != 403, r.text
            else:
                assert r.status_code == 403, r.text


def test_auth_config_is_reachable_without_a_token():
    """The boot handler reads this BEFORE any session exists - if it ever
    acquired an auth dependency, a fresh deployment would boot to a blank
    decision and land on the wrong view with no error to show."""
    from app.auth import EXCLUDED_PATHS
    assert "/api/auth/config" in EXCLUDED_PATHS
    assert "/api/auth/needs-setup" in EXCLUDED_PATHS
