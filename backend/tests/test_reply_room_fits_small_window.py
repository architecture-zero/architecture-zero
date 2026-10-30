"""A small window's reply room is a quarter of it (2026-10-29).

A signed-in turn kept 4096 tokens for the reply on every window. On an
8192-token window - a local lane at that num_ctx, or a vendor model whose list
declares it - that is half the turn: the history trim reserved it, the tool
room was sized after it, a message that fit with a smaller reserve was
refused, and with tools on the turn kept little or no history.

What these pin: on a bounded window the turn's reply room is
min(configured cap, max(512, window // 4)) - 2048 on 8192 - and the request's
cap (num_predict on the local lane, the cap field on a compatible host) is
that same figure, so the reserve and the cap cannot disagree; the window
trim, the over-window refusal and the tool-room sizing all read it. FIX: on
8192 the cap is 2048, the trim reserves 2048, more history is kept than the
old reserve allowed, the tool room is sized from 2048, and a message that
fits beside 2048 but not beside 4096 is answered. CONTROL: a 32768 or larger
window and an unknown window keep 4096; a guest's 1024 is already under a
quarter of 8192; the eval runner keeps its own 1024.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app import providers
from app.routers import chat as chat_mod
from tests.test_agent_tool_results_budget import _big_files, _tokens, _ws

LOCAL = "test-model"   # no vendor prefix and no namespace: the Ollama lane
PROMPT = "Read the logs."
OLD_REPLY = 4096       # the signed-in reply room on every window, until now


def _history(n, size):
    out = []
    for i in range(n):
        head = f"[message {i:03d}] "
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": head + "x" * (size - len(head))})
    return out


def _kept(sent):
    return [m for m in sent if m.get("role") in ("user", "assistant")
            and str(m.get("content", "")).startswith("[message")]


def _send(client, headers, session, history=None, prompt=PROMPT, model=LOCAL,
          rounds=0):
    """One chat turn through the real route, the provider stream stubbed;
    `rounds` tool rounds each read big<n>.log before the answer."""
    sink: dict = {"caps": [], "events": [], "calls": 0}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        sink["calls"] += 1
        sink["caps"].append(max_tokens)
        if sink["calls"] == 1:
            sink["first"] = [dict(m) for m in messages]
            sink["tools"] = tools
        if sink["calls"] <= rounds:
            yield {"type": "tool_call", "id": f"t{sink['calls']}", "name": "read_file",
                   "args": {"path": f"big{sink['calls'] - 1}.log"}}
            return
        sink["last"] = [dict(m) for m in messages]
        yield {"type": "text", "text": "done"}

    real_log = chat_mod.log

    def _log(event, **fields):
        sink["events"].append((event, fields))
        return real_log(event, **fields)

    with patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.log", side_effect=_log):
        r = client.post("/api/chat", headers=headers, json={
            "prompt": prompt, "model": model, "use_rag": False,
            "session_id": session, "history": history or []})
    sink["response"] = r
    sink["trims"] = [f for e, f in sink["events"] if e == "chat_history_trimmed"]
    return sink


def _ollama_resp():
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = [json.dumps(
        {"message": {"role": "assistant", "content": "hi"}, "done": True}).encode()]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _words(tokens):
    return ("word " * (tokens * 4 // 5 + 1))[:tokens * 4]


# -- FIX ----------------------------------------------------------------------

def test_an_8k_window_reserves_a_quarter_and_caps_the_reply_there(
        client, admin_headers, monkeypatch):
    """No tools: the trim reserves 2048, the request's cap is 2048, and the
    history kept is more than the 4096 reserve let through."""
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", 8192)
    history = _history(80, 400)          # about 8000 tokens: past the window
    sink = _send(client, admin_headers, "s-room-quarter", history=history)
    assert sink["response"].status_code == 200, sink["response"].text
    assert sink["caps"] == [2048]
    assert len(sink["trims"]) == 1
    assert sink["trims"][0]["reserved"] == 2048
    sent = sink["last"]
    kept = _kept(sent)
    assert 0 < len(kept) < len(history)
    assert kept == history[-len(kept):]
    assert _tokens(sent) + 2048 <= 8192
    # The room the old reserve held back now carries history.
    assert _tokens(sent) + OLD_REPLY > 8192, _tokens(sent)


def test_the_local_lanes_num_predict_is_the_reply_room(client, admin_headers, monkeypatch):
    """Through the real provider: num_predict equals the room the trim kept."""
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", 8192)
    with patch("app.providers.req.post", return_value=_ollama_resp()) as post:
        r = client.post("/api/chat", headers=admin_headers, json={
            "prompt": "hello", "model": LOCAL, "use_rag": False,
            "session_id": "s-room-num-predict"})
    assert r.status_code == 200, r.text
    sent = [c.kwargs["json"] for c in post.call_args_list
            if str(c.args[0]).endswith("/api/chat")]
    assert sent
    for payload in sent:
        assert payload["options"]["num_predict"] == 2048, payload["options"]
        assert payload["options"]["num_ctx"] == 8192


def test_the_tool_room_is_sized_from_the_reply_room(
        client, admin_headers, monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", 8192)
    ws = _ws(monkeypatch, tmp_path)
    _big_files(ws, 2)
    history = _history(40, 400)
    sink = _send(client, admin_headers, "s-room-tools", history=history, rounds=2)
    assert sink["response"].status_code == 200, sink["response"].text
    assert sink["tools"]
    assert sink["caps"] == [2048, 2048, 2048]
    system = sink["first"][0]["content"]
    budget = chat_mod._turn_tool_chars(system, PROMPT, sink["tools"], 2048, LOCAL)
    assert budget > chat_mod._turn_tool_chars(system, PROMPT, sink["tools"],
                                              OLD_REPLY, LOCAL)
    assert sink["trims"][0]["reserved"] == (
        2048 + (budget + len(json.dumps(sink["tools"]))) // 4
        + chat_mod._TOOL_ROUNDS_ROOM_TOKENS)
    assert _kept(sink["last"])


def test_a_message_that_fits_beside_the_quarter_is_answered(
        client, admin_headers, monkeypatch):
    """Past the window beside 4096, inside it beside 2048: answered, not
    refused - the over-window refusal reads the same reply room."""
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", 8192)
    probe = _send(client, admin_headers, "s-room-probe")
    system_tokens = len(probe["first"][0]["content"]) // 4
    prompt = _words(8192 - system_tokens - 3000)
    needed = (len(probe["first"][0]["content"]) + len(prompt)) // 4
    assert needed + 2048 <= 8192 < needed + OLD_REPLY
    sink = _send(client, admin_headers, "s-room-fits", prompt=prompt)
    assert sink["response"].status_code == 200, sink["response"].text
    assert sink["caps"] == [2048]
    assert sink["last"][-1] == {"role": "user", "content": prompt}


@pytest.mark.parametrize("window, room", [
    (8192, 2048), (4096, 1024), (2048, 512), (1024, 512), (16384, 4096),
    (32768, 4096), (131072, 4096), (None, 4096)])
def test_the_reply_room_rule(monkeypatch, window, room):
    monkeypatch.setattr(chat_mod, "bounded_window_tokens", lambda model: window)
    assert chat_mod._reply_room(4096, LOCAL) == room


# -- CONTROL ------------------------------------------------------------------

@pytest.mark.parametrize("window", [32768, 131072])
def test_a_large_window_keeps_4096(client, admin_headers, monkeypatch, window):
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", window)
    sink = _send(client, admin_headers, f"s-room-{window}", history=_history(4, 300))
    assert sink["caps"] == [4096]
    assert sink["trims"] == []


def test_an_unknown_window_keeps_4096(client, admin_headers):
    with patch("app.routers.chat.offered_providers",
               return_value={"ollama", "anthropic"}):
        sink = _send(client, admin_headers, "s-room-cloud", history=_history(40, 400),
                     model="claude-sonnet-4-5-20250929")
    assert sink["caps"] == [4096]
    assert sink["trims"] == []


def test_a_guests_cap_is_already_under_the_quarter(client, monkeypatch):
    from tests.test_guest_spend import _cfg
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", 8192)
    assert chat_mod.GUEST_MAX_TOKENS <= 2048
    caps = []

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        caps.append((model, max_tokens))
        yield {"type": "text", "text": "ok"}

    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.GUEST_MODEL", LOCAL), \
         patch("app.routers.chat.get_config", side_effect=_cfg(chat_model=LOCAL)), \
         patch("app.routers.chat.stream_chat_events", side_effect=_stream):
        r = client.post("/api/chat", json={"prompt": "Hi"})
    assert r.status_code == 200, r.text
    assert caps == [(LOCAL, chat_mod.GUEST_MAX_TOKENS)]


@pytest.mark.parametrize("window", [8192, 2048])
def test_the_eval_runner_keeps_its_own_1024(monkeypatch, tmp_path, window):
    from tests.test_eval_turn_fits_window import _eval, _setup_turns
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", window)
    sink = _eval(monkeypatch, tmp_path, f"eval-room-{window}", "m", _setup_turns(4, 200))
    assert sink["max_tokens"] == 1024
