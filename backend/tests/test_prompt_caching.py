"""Anthropic prompt-caching payload tests.

The direct-API cost fix: `system` is sent as content blocks with a
cache_control ephemeral breakpoint on the FIRST block (the stable core), so
Anthropic caches tools + core server-side and re-reads bill ~10%. These pin:

- the stable/suffix SPLIT (a system message that startswith the system_prompt
  param keeps only its remainder as an uncached tail block),
- the DEDUPE of the pre-existing double-send (callers passing the same string
  as both the param and a system message - the eval judges did - used to get
  it joined twice into `system`),
- the wire shape on both Anthropic call sites (streaming + tool-call).

Caching is fail-silent (off looks like on except in billing), so the payload
shape must be guarded here; the live proof is usage.cache_read_input_tokens.
"""
import json
from unittest.mock import MagicMock, patch

from app.providers import (_anthropic_stream_events, _anthropic_system_blocks,
                           _anthropic_tool_call)

_CORE = "You are a helpful assistant." + ("x" * 50)
_SUFFIX = "\n\n--- ACCESS TIER: NON-OWNER ---\nno history.\n--- END ---"


def test_split_core_and_suffix():
    # Chat/eval shape: param = stable core, message = core + conditional suffix.
    blocks = _anthropic_system_blocks(
        _CORE, [{"role": "system", "content": _CORE + _SUFFIX}])
    assert len(blocks) == 2
    assert blocks[0] == {"type": "text", "text": _CORE,
                         "cache_control": {"type": "ephemeral"}}
    assert blocks[1] == {"type": "text", "text": _SUFFIX}  # NO cache_control
    # Byte preservation: the blocks re-concatenate to exactly what the model
    # saw before the split existed.
    assert blocks[0]["text"] + blocks[1]["text"] == _CORE + _SUFFIX


def test_exact_duplicate_deduped():
    # Judge shape: the SAME string arrives as the param AND a system message.
    # Old behavior joined them twice; now it must appear exactly once, cached.
    blocks = _anthropic_system_blocks(
        _CORE, [{"role": "system", "content": _CORE}])
    assert blocks == [{"type": "text", "text": _CORE,
                       "cache_control": {"type": "ephemeral"}}]


def test_message_only_whole_block_cached():
    # Tool-call path shape: no param, system rides only as a message.
    blocks = _anthropic_system_blocks(
        "", [{"role": "system", "content": _CORE + _SUFFIX}])
    assert blocks == [{"type": "text", "text": _CORE + _SUFFIX,
                       "cache_control": {"type": "ephemeral"}}]


def test_no_system_returns_none():
    assert _anthropic_system_blocks("", [{"role": "user", "content": "hi"}]) is None


def test_extra_system_message_lands_in_uncached_tail():
    # Context-summarize strategy injects a mid-history system message ("Earlier
    # conversation summary: ..."). It varies, so it must never join the cached
    # core block.
    summary = "Earlier conversation summary: we discussed the eval harness."
    blocks = _anthropic_system_blocks(
        _CORE,
        [{"role": "system", "content": _CORE + _SUFFIX},
         {"role": "system", "content": summary}])
    assert len(blocks) == 2
    assert blocks[0]["text"] == _CORE
    assert "cache_control" not in blocks[1]
    assert blocks[1]["text"] == _SUFFIX + "\n\n" + summary


def _sse_resp(lines):
    resp = MagicMock()
    resp.ok = True
    resp.iter_lines.return_value = lines
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_stream_payload_carries_cached_system_blocks():
    # Wire shape on the streaming site: system must be a block LIST with the
    # breakpoint on block 1 - a refactor back to a plain string would silently
    # turn caching off (no error, just full-price re-sends).
    msgs = [{"role": "system", "content": _CORE + _SUFFIX},
            {"role": "user", "content": "what's next?"}]
    with patch("app.providers.req.post", return_value=_sse_resp([])) as post:
        list(_anthropic_stream_events(msgs, "claude-sonnet-4-6",
                                      system_prompt=_CORE))
    payload = post.call_args.kwargs["json"]
    assert payload["system"] == [
        {"type": "text", "text": _CORE, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _SUFFIX},
    ]


def test_tool_call_payload_carries_cached_system_blocks():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"content": [{"type": "text", "text": "ok"}]}
    msgs = [{"role": "system", "content": _CORE},
            {"role": "user", "content": "status?"}]
    with patch("app.providers.req.post", return_value=resp) as post:
        _anthropic_tool_call(msgs, "claude-sonnet-4-6", [])
    payload = post.call_args.kwargs["json"]
    assert payload["system"] == [
        {"type": "text", "text": _CORE, "cache_control": {"type": "ephemeral"}}]
