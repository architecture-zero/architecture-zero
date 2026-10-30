"""An empty tools list is left out of the request (2026-10-29).

The history summarizer calls the non-streaming door with tools=[], and the
OpenAI-style branch put that list in the body as given: "tools": []. OpenAI's
API refuses an empty tools array with a 400, so on OpenAI every summary
raised, and context_strategy=summarize always fell back to its placeholder.
The other doors already left an empty list out.

What these pin: on every door that builds a body - the OpenAI-style
non-streaming door and streamer, the Ollama streamer and its buffered
fallback, the Anthropic streamer and non-streaming door - a call with an
empty list sends no tools key, and the summarizer's request on OpenAI carries
none. CONTROL: a call with tools carries them as before.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import providers
from app.routers import chat as chat_mod

_MSGS = [{"role": "user", "content": "q"}]
_TOOLS = [{"type": "function",
           "function": {"name": "search_files", "description": "",
                        "parameters": {"type": "object", "properties": {}}}}]


def _json_resp(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _stream_resp(lines):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.ok = True
    resp.iter_lines.return_value = lines
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _compat_door(tools):
    resp = _json_resp({"choices": [{"message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop"}]})
    with patch("app.providers.req.post", return_value=resp) as post:
        providers.non_stream_tool_call(_MSGS, "gpt-4o", tools)
    return post.call_args.kwargs["json"]


def _compat_stream(tools):
    resp = _stream_resp([b'data: {"choices": [{"delta": {"content": "ok"}}]}',
                         b"data: [DONE]"])
    with patch("app.providers.req.post", return_value=resp) as post:
        list(providers._openai_stream_events(_MSGS, "gpt-4o", tools, 64, provider="openai"))
    return post.call_args.kwargs["json"]


def _ollama_stream(tools):
    resp = _stream_resp([json.dumps({"message": {"role": "assistant", "content": "ok"},
                                     "done": True}).encode()])
    with patch("app.providers.req.post", return_value=resp) as post:
        list(providers._ollama_stream_events(_MSGS, "test-model", tools))
    return post.call_args.kwargs["json"]


def _ollama_buffered(tools):
    resp = _json_resp({"message": {"role": "assistant", "content": "ok"}, "done": True})
    with patch("app.providers.req.post", return_value=resp) as post:
        providers._ollama_chat_nonstream(_MSGS, "test-model", tools)
    return post.call_args.kwargs["json"]


def _anthropic_stream(tools):
    resp = _stream_resp([
        b'data: {"type": "content_block_delta", "index": 0, '
        b'"delta": {"type": "text_delta", "text": "ok"}}'])
    with patch("app.providers.req.post", return_value=resp) as post:
        list(providers._anthropic_stream_events(_MSGS, "claude-sonnet-4-5-20250929",
                                                "", 64, tools))
    return post.call_args.kwargs["json"]


def _anthropic_door(tools):
    resp = _json_resp({"content": [{"type": "text", "text": "ok"}],
                       "stop_reason": "end_turn", "usage": {}})
    with patch("app.providers.req.post", return_value=resp) as post:
        providers.non_stream_tool_call(_MSGS, "claude-sonnet-4-5-20250929", tools)
    return post.call_args.kwargs["json"]


_DOORS = {
    "openai-style door": _compat_door,
    "openai-style stream": _compat_stream,
    "ollama stream": _ollama_stream,
    "ollama buffered": _ollama_buffered,
    "anthropic stream": _anthropic_stream,
    "anthropic door": _anthropic_door,
}


# -- FIX ----------------------------------------------------------------------

@pytest.mark.parametrize("door", list(_DOORS))
def test_an_empty_tools_list_sends_no_tools_key(door):
    body = _DOORS[door]([])
    assert "tools" not in body, (door, body)
    assert "tool_choice" not in body


def test_the_summarizers_request_to_openai_carries_no_tools_key():
    turns = [SimpleNamespace(role="user", content="What did we decide?"),
             SimpleNamespace(role="assistant", content="One surface at a time.")]
    resp = _json_resp({"choices": [{"message": {"role": "assistant",
                                                "content": "They chose one at a time."},
                                    "finish_reason": "stop"}]})
    with patch("app.providers.req.post", return_value=resp) as post:
        out = chat_mod._summarize_history(turns, "gpt-4o")
    assert out == "They chose one at a time."
    body = post.call_args.kwargs["json"]
    assert "tools" not in body, body


# -- CONTROL ------------------------------------------------------------------

@pytest.mark.parametrize("door", list(_DOORS))
def test_a_call_with_tools_carries_them(door):
    body = _DOORS[door](_TOOLS)
    if door.startswith("anthropic"):
        assert body["tools"] == providers._to_anthropic_tools(_TOOLS)
    else:
        assert body["tools"] == _TOOLS
