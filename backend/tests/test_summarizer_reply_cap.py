"""The non-streaming door takes a reply cap, and the history summarizer passes
its own (2026-10-29).

The non-streaming door (providers.non_stream_tool_call) serves the history
summarizer (context_strategy=summarize) and the eval runner's tool rounds.
Its OpenAI-style branch sent no cap at all, so a summary on that path ran to
the model's own limit; the Anthropic and Ollama branches each sent a fixed
figure of their own (8192 with thinking room, and 16384).

What these pin: the door takes a cap, and the summarizer passes
chat._SUMMARY_MAX_TOKENS on every lane - as max_completion_tokens to OpenAI's
own API and max_tokens to another compatible host (providers._cap_field), as
num_predict to Ollama, and as the Anthropic door's max_tokens with its
thinking room. CONTROL: a call that passes no cap - the eval runner's - sends
exactly what it sent before: no cap field on the OpenAI-style door, 16384 on
Ollama, 8192 with thinking room on Anthropic; and the eval runner, driven
through the real door, calls it with no cap.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import model_catalog, providers
from app.routers import chat as chat_mod

_TURNS = [SimpleNamespace(role="user", content="What did we decide about the rollout?"),
          SimpleNamespace(role="assistant", content="One surface at a time, each with its gate.")]
_MSGS = [{"role": "user", "content": "q"}]
_SUMMARY = "They agreed to roll out one surface at a time."


def _json_resp(payload):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _compat_resp(text=_SUMMARY):
    return _json_resp({"choices": [{"message": {"role": "assistant", "content": text},
                                    "finish_reason": "stop"}]})


def _ollama_resp(text=_SUMMARY):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = [json.dumps(
        {"message": {"role": "assistant", "content": text}, "done": True}).encode()]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _anthropic_resp(text=_SUMMARY):
    return _json_resp({"content": [{"type": "text", "text": text}],
                       "stop_reason": "end_turn", "usage": {}})


@pytest.fixture(autouse=True)
def _default_bases(monkeypatch):
    """Each registry entry on its own default base."""
    real = providers._get_runtime
    monkeypatch.setattr(providers, "_get_runtime",
                        lambda db_key, env_key, default="": (
                            default if db_key.endswith("_base_url")
                            else real(db_key, env_key, default)))


def _summarize(model, resp):
    with patch("app.providers.req.post", return_value=resp) as post:
        out = chat_mod._summarize_history(_TURNS, model)
    assert out == _SUMMARY
    assert post.call_count == 1
    return post.call_args.args[0], post.call_args.kwargs["json"]


# -- FIX ----------------------------------------------------------------------

def test_the_summary_bound_is_a_real_bound():
    assert isinstance(chat_mod._SUMMARY_MAX_TOKENS, int)
    assert 0 < chat_mod._SUMMARY_MAX_TOKENS <= 4096


def test_the_summarizer_caps_its_reply_on_openais_api():
    url, payload = _summarize("gpt-4o", _compat_resp())
    assert url == "https://api.openai.com/v1/chat/completions"
    assert payload["max_completion_tokens"] == chat_mod._SUMMARY_MAX_TOKENS
    assert "max_tokens" not in payload


def test_the_summarizer_caps_its_reply_on_another_compatible_host():
    url, payload = _summarize("groq:llama-3.3-70b-versatile", _compat_resp())
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert payload["max_tokens"] == chat_mod._SUMMARY_MAX_TOKENS
    assert "max_completion_tokens" not in payload


def test_the_summarizer_caps_its_reply_on_the_local_lane():
    _, payload = _summarize("test-model", _ollama_resp())
    assert payload["options"]["num_predict"] == chat_mod._SUMMARY_MAX_TOKENS
    assert payload["options"]["num_ctx"] == providers.OLLAMA_NUM_CTX


def test_the_summarizer_caps_its_reply_on_anthropic():
    model = "claude-sonnet-4-5-20250929"
    _, payload = _summarize(model, _anthropic_resp())
    assert payload["max_tokens"] == model_catalog.output_room(
        model, chat_mod._SUMMARY_MAX_TOKENS)


# -- CONTROL ------------------------------------------------------------------

def test_no_cap_sends_no_cap_field_on_the_openai_style_door():
    with patch("app.providers.req.post", return_value=_compat_resp()) as post:
        providers.non_stream_tool_call(_MSGS, "gpt-4o", [])
    assert post.call_args.kwargs["json"] == {"model": "gpt-4o", "messages": _MSGS,
                                             "tools": []}


def test_no_cap_keeps_the_ollama_doors_16384():
    with patch("app.providers.req.post", return_value=_ollama_resp()) as post:
        providers.non_stream_tool_call(_MSGS, "test-model", [])
    assert post.call_args.kwargs["json"]["options"]["num_predict"] == 16384


def test_no_cap_keeps_the_anthropic_doors_8192():
    model = "claude-sonnet-4-5-20250929"
    with patch("app.providers.req.post", return_value=_anthropic_resp()) as post:
        providers.non_stream_tool_call(_MSGS, model, [])
    assert post.call_args.kwargs["json"]["max_tokens"] == model_catalog.output_room(model, 8192)


def test_the_eval_runners_tool_door_request_is_unchanged(monkeypatch, tmp_path):
    """The eval runner, through the real door: it passes no cap, and its
    request carries the model, the messages and the tools - nothing else."""
    import app.eval_runner as runner
    from app.db import get_session
    from app.models import EvalResult
    from tests.test_agent_tool_results_budget import _ws

    _ws(monkeypatch, tmp_path)
    seen: dict = {"door": [], "posts": []}
    real = providers.non_stream_tool_call

    def _spy(*args, **kwargs):
        seen["door"].append((len(args), sorted(kwargs)))
        return real(*args, **kwargs)

    def _post(url, **kw):
        seen["posts"].append((url, kw["json"]))
        if kw.get("stream"):
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.iter_lines.return_value = [
                b'data: {"choices": [{"delta": {"content": "an answer"}}]}', b"data: [DONE]"]
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp
        return _compat_resp("no tools needed")

    monkeypatch.setattr("app.database.corpus_fingerprint", lambda: "src=0;chunks=0;sha=0")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(runner, "supports_tools", lambda model="": True)
    monkeypatch.setattr(runner, "non_stream_tool_call", _spy)
    for judge in ("judge_answer", "judge_faithfulness", "judge_freshness"):
        monkeypatch.setattr(f"app.eval_judge.{judge}", lambda *a, **k: (1, "ok"))
    questions = [{"id": 9811, "question": "What do the logs say?", "category": "general",
                  "expected_source": None, "notes": "Reads the logs.", "as_level": None}]
    run_id = "eval-summary-cap-control"
    try:
        with patch("app.providers.req.post", side_effect=_post):
            runner._run_eval_job(run_id, "2026-01-01T00:00:00", questions, model="gpt-4o",
                                 use_rag=False, n_results=5, retrieval_only=False)
    finally:
        runner._eval_runs.pop(run_id, None)
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.run_id == run_id).delete()
    assert seen["door"] == [(3, [])]
    door_posts = [p for u, p in seen["posts"] if "stream" not in p]
    assert len(door_posts) == 1
    assert sorted(door_posts[0]) == ["messages", "model", "tools"]
    assert door_posts[0]["model"] == "gpt-4o" and door_posts[0]["tools"]
