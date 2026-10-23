"""The other two provider doors, brought to the Anthropic door's standard (2026-10-23).

1. The empty turn on the non-streaming exit: the streaming exit and the Anthropic
   door answer a completion with no text and no tool call with EMPTY_TURN_TEXT;
   the Ollama and OpenAI-dialect doors handed it to the caller as a blank answer.
2. The token cap's cut on record: only the Anthropic streamer logged
   "output_cut_at_token_cap"; the OpenAI dialect says finish_reason "length" and
   Ollama says done_reason "length" for the same cut, and nothing read them.
"""
import json
from unittest.mock import MagicMock, patch

from app import model_catalog
from app.providers import (EMPTY_TURN_TEXT, _ollama_stream_events,
                           _openai_stream_events, non_stream_tool_call)

_MSGS = [{"role": "user", "content": "q"}]
_TOOLS = [{"type": "function",
           "function": {"name": "search_files", "description": "", "parameters": {}}}]


def _json_resp(payload):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _stream_resp(lines):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = lines
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _openai_door(message, finish_reason="stop"):
    resp = _json_resp({"choices": [{"message": message, "finish_reason": finish_reason}]})
    with patch("app.providers.req.post", return_value=resp), \
         patch("app.providers._compat_headers", return_value={}), \
         patch("app.providers._compat_base", return_value="http://unit.test"):
        return non_stream_tool_call(_MSGS, "gpt-test", _TOOLS)


# -- 1. The empty turn on the non-streaming exit ---------------------------------

def test_openai_dialect_empty_completion_gets_the_empty_turn_line():
    """FIX: a completion with no content and no tool call is answered, not blank."""
    out = _openai_door({"role": "assistant", "content": None})
    assert out["message"]["content"] == EMPTY_TURN_TEXT
    assert out["message"]["tool_calls"] == []


def test_ollama_empty_completion_gets_the_empty_turn_line():
    """FIX: the Ollama door's empty completion is answered the same way."""
    resp = _stream_resp([json.dumps(
        {"message": {"role": "assistant", "content": ""}, "done": True}).encode()])
    with patch("app.providers.req.post", return_value=resp):
        out = non_stream_tool_call(_MSGS, "qwen3:8b", _TOOLS)
    assert out["message"]["content"] == EMPTY_TURN_TEXT
    assert out["message"]["tool_calls"] == []


def test_openai_dialect_text_completion_passes_through():
    """CONTROL: a completion that said something is untouched."""
    out = _openai_door({"role": "assistant", "content": "Hello"})
    assert out["message"]["content"] == "Hello"


def test_openai_dialect_tool_call_only_round_passes_through():
    """CONTROL: an agentic round that only called a tool is not an empty turn."""
    calls = [{"id": "c1", "type": "function",
              "function": {"name": "search_files", "arguments": "{}"}}]
    out = _openai_door({"role": "assistant", "content": None, "tool_calls": calls},
                       finish_reason="tool_calls")
    assert out["message"]["content"] is None
    assert out["message"]["tool_calls"] == calls


# -- 2. The token cap's cut on record -------------------------------------------

def _seen(monkeypatch):
    seen = []
    monkeypatch.setattr("app.logger.log", lambda event, **f: seen.append((event, f)))
    return seen


def _openai_stream(chunks, max_tokens=1024):
    lines = [b"data: " + json.dumps(c).encode() for c in chunks] + [b"data: [DONE]"]
    with patch("app.providers.req.post", return_value=_stream_resp(lines)), \
         patch("app.providers._compat_headers", return_value={}), \
         patch("app.providers._compat_base", return_value="http://unit.test"):
        return list(_openai_stream_events(_MSGS, "gpt-test", None, max_tokens, provider="openai"))


def test_openai_stream_cut_at_the_cap_is_logged(monkeypatch):
    """FIX: finish_reason "length" puts the cut on record."""
    seen = _seen(monkeypatch)
    events = _openai_stream([
        {"choices": [{"delta": {"content": "Where does"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ])
    assert events == [{"type": "text", "text": "Where does"}]
    assert seen == [("output_cut_at_token_cap", {"model": "gpt-test", "max_tokens": 1024})]


def test_openai_stream_whole_answer_is_not_logged(monkeypatch):
    """CONTROL: an answer that ended on its own is not a cut."""
    seen = _seen(monkeypatch)
    _openai_stream([
        {"choices": [{"delta": {"content": "Done."}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ])
    assert seen == []


def test_ollama_stream_cut_at_the_cap_is_logged(monkeypatch):
    """FIX: done_reason "length" (num_predict reached) puts the cut on record."""
    seen = _seen(monkeypatch)
    resp = _stream_resp([
        json.dumps({"message": {"role": "assistant", "content": "Where"}, "done": False}).encode(),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "done_reason": "length"}).encode(),
    ])
    with patch("app.providers.req.post", return_value=resp):
        events = list(_ollama_stream_events(_MSGS, "qwen3:8b", None,
                                            options={"num_predict": 16384}))
    assert events == [{"type": "text", "text": "Where"}]
    assert seen == [("output_cut_at_token_cap", {"model": "qwen3:8b", "max_tokens": 16384})]


def test_ollama_stream_whole_answer_is_not_logged(monkeypatch):
    """CONTROL: done_reason "stop" is not a cut."""
    seen = _seen(monkeypatch)
    resp = _stream_resp([json.dumps({"message": {"role": "assistant", "content": "ok"},
                                     "done": True, "done_reason": "stop"}).encode()])
    with patch("app.providers.req.post", return_value=resp):
        list(_ollama_stream_events(_MSGS, "qwen3:8b", None))
    assert seen == []


def test_note_if_cut_reads_both_dialects_words(monkeypatch):
    """FIX: "length" is the cut's word outside Anthropic; "max_tokens" stays."""
    seen = _seen(monkeypatch)
    model_catalog.note_if_cut("length", "m", 10)
    model_catalog.note_if_cut("max_tokens", "m", 10)
    model_catalog.note_if_cut("stop", "m", 10)
    assert [e for e, _ in seen] == ["output_cut_at_token_cap", "output_cut_at_token_cap"]
