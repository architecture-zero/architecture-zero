"""Ollama transport tests - streaming with tools is load-bearing when the
Ollama base sits behind a tunnel/proxy (Cloudflare 524s any request with no
response bytes after ~100s). These pin the fix: tools ride the STREAMING
request, tool calls are parsed from chunks, and the buffered path survives
only as the old-server (HTTP 400) fallback."""
import json
from unittest.mock import MagicMock, patch

import requests

from app.providers import _ollama_stream_events, _ollama_tool_call


def _ndjson(*objs):
    return [json.dumps(o).encode() for o in objs]


def _stream_resp(lines):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = lines
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


_TOOLS = [{"type": "function",
           "function": {"name": "search_files", "description": "", "parameters": {}}}]

_MSGS = [{"role": "user", "content": "what's next?"}]


def test_ollama_streams_with_tools_in_payload():
    # The regression that caused the 524: tools used to force one buffered
    # stream=False call. Tools must now ride the streaming request itself.
    resp = _stream_resp(_ndjson(
        {"message": {"role": "assistant", "content": "Check"}, "done": False},
        {"message": {"role": "assistant", "content": "ing"}, "done": False},
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "search_files",
                                                  "arguments": {"query": "readme"}}}]},
         "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True},
    ))
    with patch("app.providers.req.post", return_value=resp) as post:
        events = list(_ollama_stream_events(_MSGS, "qwen3:8b", _TOOLS))

    payload = post.call_args.kwargs["json"]
    assert payload["stream"] is True
    assert payload["tools"] == _TOOLS
    assert post.call_args.kwargs["stream"] is True

    assert events == [
        {"type": "text", "text": "Check"},
        {"type": "text", "text": "ing"},
        {"type": "tool_call", "id": "", "name": "search_files", "args": {"query": "readme"}},
    ]


def test_ollama_requests_carry_num_ctx_on_every_path():
    """Ollama ignores the model's advertised context and applies its own ~4k
    default, truncating the FRONT - the system prompt, where the safety and
    grounding rules live. It fails SILENTLY (no error, a plausible rule-free
    answer), so the guard is this test. Proven live by a needle probe: no
    num_ctx -> the front is gone; num_ctx=32768 -> recalled exactly.
    Re-open only if: Ollama starts honoring the model's own context length."""
    from app.providers import OLLAMA_NUM_CTX
    assert OLLAMA_NUM_CTX >= 32768, "context window must fit the prompt core plus retrieved context"

    # Streaming path (the one every chat turn uses).
    resp = _stream_resp(_ndjson({"message": {"role": "assistant", "content": "hi"}, "done": True}))
    with patch("app.providers.req.post", return_value=resp) as post:
        list(_ollama_stream_events(_MSGS, "qwen3:8b", _TOOLS))
    assert post.call_args.kwargs["json"]["options"]["num_ctx"] == OLLAMA_NUM_CTX

    # Caller-supplied options must SURVIVE the merge, not be replaced.
    resp2 = _stream_resp(_ndjson({"message": {"role": "assistant", "content": "hi"}, "done": True}))
    with patch("app.providers.req.post", return_value=resp2) as post2:
        list(_ollama_stream_events(_MSGS, "qwen3:8b", None,
                                   options={"num_predict": 16384}))
    opts = post2.call_args.kwargs["json"]["options"]
    assert opts["num_predict"] == 16384 and opts["num_ctx"] == OLLAMA_NUM_CTX

    # Buffered fallback path carries it too.
    from app.providers import _ollama_chat_nonstream
    buffered = MagicMock()
    buffered.status_code = 200
    buffered.raise_for_status.return_value = None
    buffered.json.return_value = {"message": {"role": "assistant", "content": "x"}}
    with patch("app.providers.req.post", return_value=buffered) as post3:
        _ollama_chat_nonstream(_MSGS, "qwen3:8b")
    assert post3.call_args.kwargs["json"]["options"]["num_ctx"] == OLLAMA_NUM_CTX


def test_ollama_stream_parses_string_arguments():
    # Some models emit arguments as a JSON string instead of an object.
    resp = _stream_resp(_ndjson(
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "search_files",
                                                  "arguments": "{\"query\": \"readme\"}"}}]},
         "done": True},
    ))
    with patch("app.providers.req.post", return_value=resp):
        events = list(_ollama_stream_events(_MSGS, "qwen3:8b", _TOOLS))
    assert events == [{"type": "tool_call", "id": "", "name": "search_files",
                       "args": {"query": "readme"}}]


def test_ollama_400_with_tools_falls_back_to_buffered():
    # Pre-0.8 servers reject stream+tools with HTTP 400 - one buffered call,
    # same events out.
    rejected = MagicMock()
    rejected.status_code = 400
    rejected.raise_for_status.side_effect = requests.exceptions.HTTPError(response=rejected)

    buffered = MagicMock()
    buffered.status_code = 200
    buffered.raise_for_status.return_value = None
    buffered.json.return_value = {"message": {
        "role": "assistant", "content": "done",
        "tool_calls": [{"function": {"name": "search_files", "arguments": {}}}],
    }}

    with patch("app.providers.req.post", side_effect=[rejected, buffered]) as post:
        events = list(_ollama_stream_events(_MSGS, "old-model", _TOOLS))

    assert post.call_count == 2
    assert post.call_args_list[1].kwargs["json"]["stream"] is False
    assert events == [
        {"type": "text", "text": "done"},
        {"type": "tool_call", "id": "", "name": "search_files", "args": {}},
    ]


def test_ollama_400_without_tools_raises():
    # The fallback exists only for the stream+tools rejection; a plain 400
    # (bad model name etc.) must still surface.
    rejected = MagicMock()
    rejected.status_code = 400
    rejected.raise_for_status.side_effect = requests.exceptions.HTTPError(response=rejected)
    with patch("app.providers.req.post", return_value=rejected):
        try:
            list(_ollama_stream_events(_MSGS, "bad-model", None))
            assert False, "expected HTTPError"
        except requests.exceptions.HTTPError:
            pass


def test_ollama_tool_call_streams_under_the_hood():
    # non_stream_tool_call callers (history summary, eval runner) keep their
    # buffered interface, but the transport must stream so a tunneled base
    # stays alive.
    resp = _stream_resp(_ndjson(
        {"message": {"role": "assistant", "content": "part1 "}, "done": False},
        {"message": {"role": "assistant", "content": "part2",
                     "tool_calls": [{"function": {"name": "search_files",
                                                  "arguments": {"query": "readme"}}}]},
         "done": True},
    ))
    with patch("app.providers.req.post", return_value=resp) as post:
        data = _ollama_tool_call(_MSGS, "qwen3:8b", _TOOLS)

    payload = post.call_args.kwargs["json"]
    assert payload["stream"] is True
    # num_predict still rides (the original assertion); num_ctx joined it
    # later - see test_ollama_requests_carry_num_ctx_on_every_path.
    from app.providers import OLLAMA_NUM_CTX
    assert payload["options"]["num_predict"] == 16384
    assert payload["options"]["num_ctx"] == OLLAMA_NUM_CTX

    msg = data["message"]
    assert msg["content"] == "part1 part2"
    assert msg["tool_calls"] == [{
        "id": "", "type": "function",
        "function": {"name": "search_files", "arguments": {"query": "readme"}},
    }]


# -- Stream-error fail-loud ---------------------------------------------------
# All three streamers used to SKIP in-stream error payloads (Anthropic SSE
# error events, Ollama {"error": ...} lines, OpenAI-compat error chunks): the
# round yielded zero events, the agentic loop read that as "final answer", and
# the user got a silent empty turn. An in-stream error must raise - the chat
# handler surfaces it as a visible error event.

def test_ollama_stream_error_line_raises():
    import pytest
    resp = _stream_resp(_ndjson(
        {"message": {"role": "assistant", "content": "par"}, "done": False},
        {"error": "model runner has unexpectedly stopped"},
    ))
    with patch("app.providers.req.post", return_value=resp):
        with pytest.raises(ValueError, match="Ollama stream error"):
            list(_ollama_stream_events(_MSGS, "qwen3:8b", _TOOLS))


def test_anthropic_stream_error_event_raises():
    import pytest
    from app.providers import _anthropic_stream_events
    lines = [
        b"data: " + json.dumps(
            {"type": "message_start", "message": {"usage": {}}}).encode(),
        b"data: " + json.dumps(
            {"type": "error", "error": {"type": "overloaded_error",
                                        "message": "Overloaded"}}).encode(),
    ]
    resp = _stream_resp(lines)
    resp.ok = True
    with patch("app.providers.req.post", return_value=resp), \
         patch("app.providers._anthropic_headers", return_value={}):
        with pytest.raises(ValueError, match="overloaded_error"):
            list(_anthropic_stream_events(_MSGS, "claude-sonnet-4-6"))


def test_openai_stream_error_chunk_raises():
    import pytest
    from app.providers import _openai_stream_events
    resp = _stream_resp(
        [b"data: " + json.dumps({"error": {"message": "rate limited"}}).encode()])
    with patch("app.providers.req.post", return_value=resp), \
         patch("app.providers._compat_headers", return_value={}), \
         patch("app.providers._compat_base", return_value="http://unit.test"):
        with pytest.raises(ValueError, match="stream error"):
            list(_openai_stream_events(_MSGS, "gpt-test", None, 1024,
                                       provider="openai"))


# -- Anthropic transcript: a user message after tool results must SURVIVE -----
# _to_anthropic_messages merged a user message into a prior user entry only
# when that entry's content was a str. After a tool round the prior user entry
# is the tool_result LIST, so the message was silently DROPPED - which made
# the empty-answer retry nudge a byte-identical replay of the turn that had
# just produced nothing, in the exact scenario the guard was built for.

def test_anthropic_user_message_after_tool_results_is_not_dropped():
    from app.providers import _to_anthropic_messages
    msgs = [
        {"role": "user", "content": "list the project files"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "list_directory", "arguments": {}}}]},
        {"role": "tool", "content": "[file] readme.md"},
        {"role": "user", "content": "(system note: your previous turn "
                                    "produced no text...)"},
    ]
    out = _to_anthropic_messages(msgs)
    flat = json.dumps(out)
    assert "system note" in flat, out
    # It must ride the tool_result user turn as a text block (Anthropic
    # rejects two consecutive user messages).
    last = out[-1]
    assert last["role"] == "user" and isinstance(last["content"], list)
    kinds = [b.get("type") for b in last["content"]]
    assert kinds == ["tool_result", "text"], kinds


def test_anthropic_plain_user_merge_still_works():
    from app.providers import _to_anthropic_messages
    out = _to_anthropic_messages([
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ])
    assert len(out) == 1 and out[0]["content"] == "first\nsecond"
