"""The local lane caps the reply with num_predict (2026-10-29).

stream_chat_events handed max_tokens to the Anthropic streamer and the
OpenAI-shaped one and dropped it at the Ollama branch: the chat's
response_tokens (4096 signed in, GUEST_MAX_TOKENS for a guest) never reached
Ollama, so a local reply ran to the model's own stop - past the reply room
the history trim keeps for it inside num_ctx. The plumbing was there
(_ollama_stream_events takes options, the non-chat door passes num_predict,
note_if_cut reads it back); only the branch skipped it.

What these pin: the cap rides as options.num_predict, beside the num_ctx that
_ollama_options merges in, and the chat's own turn carries its max_tokens.
CONTROL: a cap of 0 or less sends no num_predict (no figure Ollama gives
its own meaning); a cloud lane's payload is unchanged.
"""
import json
from unittest.mock import MagicMock, patch

from app.providers import OLLAMA_NUM_CTX, stream_chat_events

LOCAL = "test-model"   # no vendor prefix and no namespace: the Ollama lane
_MSGS = [{"role": "user", "content": "what's next?"}]


def _ollama_resp():
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = [json.dumps(
        {"message": {"role": "assistant", "content": "hi"}, "done": True}).encode()]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _openai_resp():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = [
        b'data: {"choices": [{"delta": {"content": "hi"}}]}', b"data: [DONE]"]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# -- FIX ----------------------------------------------------------------------

def test_the_callers_cap_reaches_ollama_as_num_predict():
    with patch("app.providers.req.post", return_value=_ollama_resp()) as post:
        events = list(stream_chat_events(_MSGS, LOCAL, max_tokens=321))
    opts = post.call_args.kwargs["json"]["options"]
    assert opts["num_predict"] == 321, opts
    assert opts["num_ctx"] == OLLAMA_NUM_CTX
    assert events[0] == {"type": "text", "text": "hi"}


def test_the_default_cap_reaches_ollama_too():
    """The signature's default (1024) is a real cap on the cloud lanes
    already; the local lane now holds the same one instead of none."""
    with patch("app.providers.req.post", return_value=_ollama_resp()) as post:
        list(stream_chat_events(_MSGS, LOCAL))
    assert post.call_args.kwargs["json"]["options"]["num_predict"] == 1024


def test_a_chat_turn_on_the_local_lane_carries_its_max_tokens(
        client, admin_headers):
    """The chat's own turn, through the real route: a signed-in turn's
    max_tokens (4096) rides to Ollama as num_predict."""
    with patch("app.providers.req.post", return_value=_ollama_resp()) as post:
        r = client.post("/api/chat", headers=admin_headers, json={
            "prompt": "hello", "model": LOCAL, "use_rag": False,
            "session_id": "s-num-predict"})
    assert r.status_code == 200, r.text
    sent = [c.kwargs["json"] for c in post.call_args_list
            if str(c.args[0]).endswith("/api/chat")]
    assert sent, post.call_args_list
    for payload in sent:
        assert payload["options"]["num_predict"] == 4096, payload["options"]
        assert payload["options"]["num_ctx"] == OLLAMA_NUM_CTX


# -- CONTROL ------------------------------------------------------------------

def test_a_cap_of_zero_or_less_sends_no_num_predict():
    for cap in (0, -1):
        with patch("app.providers.req.post", return_value=_ollama_resp()) as post:
            list(stream_chat_events(_MSGS, LOCAL, max_tokens=cap))
        opts = post.call_args.kwargs["json"]["options"]
        assert "num_predict" not in opts, (cap, opts)
        assert opts["num_ctx"] == OLLAMA_NUM_CTX


def test_a_cloud_lane_payload_is_unchanged():
    with patch("app.providers.req.post", return_value=_openai_resp()) as post:
        events = list(stream_chat_events(_MSGS, "gpt-4o", max_tokens=321))
    # OpenAI's own API names the cap max_completion_tokens (2026-10-29,
    # tests/test_openai_cap_field.py); still no options and no num_predict.
    assert post.call_args.kwargs["json"] == {
        "model": "gpt-4o", "messages": _MSGS, "stream": True,
        "max_completion_tokens": 321}
    assert events == [{"type": "text", "text": "hi"}]
