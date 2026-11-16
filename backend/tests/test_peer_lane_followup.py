"""The peer lane asks the follow-up the local lane resolves, built from the
user's own words only (2026-11-16).

A bare follow-up ("tell me more", "current") carries no subject. The local
lane resolves it (routing.resolve_followup re-attaches the last substantive
user turn), but the peer lane asked every peer the words as typed and ranked
what came back against them: a follow-up asked each peer "tell me more", and
the pieces that came back were noise.

The peer lane now asks routing.peer_lane_query - the query each peer is asked
AND the one the peers' pieces are ranked against (peer_boundary.select). It is
the local lane's resolution when that is built from the user's own words: the
topic shape, the last substantive user turn re-attached, with the status frame
decided on that turn alone. It is never built from assistant text. The
'exchange' shape carries the start of the last answer, an answer is written
from any collection the caller may read, and a peer is not held to this
instance's clearance - so under that shape a peer gets the topic shape's
query. A turn the resolver leaves alone asks the peers the prompt itself, byte
for byte, as before. The model, the saved history and the logs keep the
user's real words.

FIX tests fail on the commit before, where the peers were asked the prompt as
typed. CONTROL tests hold on both. The canary tests are CONTROLS on the commit
before (nothing was resolved for a peer there, so nothing could leak); they
are the pin that a peer query built from the local lane's resolution - which
reads the answer under 'exchange' - would fail.
"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

USER = "How should a new operator prepare the first knowledge import?"
ANSWER = "Start with the onboarding guide, then stage the files in a folder."
PROJECT = "give me a general review of my project"
HISTORY = [{"role": "user", "content": USER},
           {"role": "assistant", "content": ANSWER}]
# Text only an assistant turn carries. It must never reach a peer.
CANARY = "zq-canary-7311-answer-only"

PEERS = [{"id": "p1", "name": "peer-one", "url": "http://peer-one.test", "enabled": True},
         {"id": "p2", "name": "peer-two", "url": "http://peer-two.test", "enabled": True}]
PIECE = {"source": "peer-doc.md", "text": "A peer's piece about staging files.", "score": 0.9}

_BEFORE_PATH = Path(__file__).resolve().parent / "fixtures" / "routing_topic_shape.py"


def _load_before():
    """The resolver as it was before the 'exchange' shape existed: the topic
    shape, and nothing else, by construction."""
    spec = importlib.util.spec_from_file_location("routing_topic_shape_peer", _BEFORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BEFORE = _load_before()


@pytest.fixture(autouse=True)
def _latch_shut(monkeypatch):
    """The 'exchange' shape is live only while the host's hosted-rerank latch
    is shut; every test starts with it shut whatever the machine's environment
    holds, so a test that sets the key really runs the shape."""
    import app.rag_config as latch_home
    monkeypatch.setattr(latch_home, "RERANK_HOSTED_ALLOWED", False)


def _shape(monkeypatch, value):
    """The config key `followup_resolver` as an operator sets it; None is no
    row at all."""
    import app.config
    real = app.config.get_config

    def _get(key, default=""):
        if key == "followup_resolver":
            return default if value is None else value
        return real(key, default)
    monkeypatch.setattr(app.config, "get_config", _get)


def _turn(client, admin_headers, prompt, history):
    """One chat turn with two enabled peers, local retrieval, the peer rank and
    the model stood in. Returns what each lane was handed: the local retrieval
    query, the query each peer was asked, the query the peers' pieces were
    ranked against, the messages the model was sent and the saved rows."""
    seen = {"local": [], "asked": [], "ranked": [], "messages": None, "saved": []}

    def _retrieve(query, department=None, top_k=None, user_level=None, stats=None, **kw):
        seen["local"].append(query)
        return []

    def _ask(peer, query, n_results=8, timeout=10):
        seen["asked"].append((peer["name"], query))
        return [dict(PIECE, peer=peer["name"])]

    def _rank(query, pool, top_k=None, stats=None):
        seen["ranked"].append(query)
        return list(pool)

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        seen["messages"] = messages
        yield {"type": "text", "text": "ok"}

    with patch("app.routers.chat.get_peers", return_value=[dict(p) for p in PEERS]), \
         patch("app.routers.chat.query_peer_kb", side_effect=_ask), \
         patch("app.rerank.retrieve", side_effect=_retrieve), \
         patch("app.rerank.rerank", side_effect=_rank), \
         patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.save_message",
               side_effect=lambda *a, **k: seen["saved"].append(a)):
        r = client.post("/api/chat", headers=admin_headers, json={
            "prompt": prompt, "use_peers": True, "use_rag": True,
            "session_id": "peer-lane-followup", "history": history or []})
    assert r.status_code == 200, r.text
    return seen


def _asked(seen) -> list:
    """The peers' questions, in a fixed order: the fan-out runs in threads."""
    return sorted(seen["asked"])


# -- FIX: a bare follow-up asks the peers its subject ---------------------------

FOLLOW_UPS = [
    ("tell me more", HISTORY, f"{USER} - tell me more"),
    ("  why?  ", HISTORY, f"{USER} - why?"),
    ("more", HISTORY + [{"role": "user", "content": "current"},
                        {"role": "assistant", "content": "Here is where it stands."}],
     f"{USER} - more"),
    ("current", [{"role": "user", "content": PROJECT},
                 {"role": "assistant", "content": "Here is the review of the build."}],
     f"{PROJECT} - current status"),
]


@pytest.mark.parametrize("prompt, history, want", FOLLOW_UPS,
                         ids=["tell me more", "padded why", "chained", "status frame"])
def test_fix_a_bare_follow_up_asks_every_peer_the_resolved_topic(client, admin_headers,
                                                                  prompt, history, want):
    """Each peer is asked the follow-up with its subject, the peers' pieces are
    ranked against the same words, and they are the words the local lane
    retrieved on (the topic shape is the default)."""
    seen = _turn(client, admin_headers, prompt, history)
    assert _asked(seen) == [("peer-one", want), ("peer-two", want)]
    assert seen["ranked"] == [want]
    assert seen["local"] == [want]


def test_control_the_model_and_the_saved_history_keep_the_users_words(client, admin_headers):
    seen = _turn(client, admin_headers, "tell me more", HISTORY)
    resolved = f"{USER} - tell me more"
    user_turns = [m for m in seen["messages"] if m.get("role") == "user"]
    assert user_turns[-1]["content"].endswith("QUESTION: tell me more")
    assert resolved not in json.dumps(seen["messages"])
    assert any(row[1:3] == ("user", "tell me more") for row in seen["saved"]), seen["saved"]
    assert not any(resolved in str(row) for row in seen["saved"])


# -- CONTROL: a turn the resolver leaves alone asks the prompt, byte for byte ----

UNRESOLVED = [
    ("What is the refund policy?", HISTORY),
    ("  Tell me more about the second point, please.  ", HISTORY),
    ("tell me more", None),
    ("  tell me more  ", []),
    ("why?", [{"role": "assistant", "content": ANSWER}]),
    ("more", [{"role": "user", "content": "current"},
              {"role": "assistant", "content": ANSWER}]),
    ("now " + "x" * 70, HISTORY),
]


@pytest.mark.parametrize("prompt, history", UNRESOLVED,
                         ids=["a real question", "a padded real question", "no history",
                              "empty history", "no user turn", "only bare user turns",
                              "too long to be a follow-up"])
def test_control_a_turn_the_resolver_leaves_alone_asks_the_peers_its_prompt(
        client, admin_headers, prompt, history):
    seen = _turn(client, admin_headers, prompt, history)
    assert _asked(seen) == [("peer-one", prompt), ("peer-two", prompt)]
    assert seen["ranked"] == [prompt]


# -- The canary: no assistant text reaches a peer, under any shape ---------------

CANARY_HISTORIES = {
    "an answer after the question": [
        {"role": "user", "content": USER},
        {"role": "assistant", "content": f"{CANARY} {ANSWER}"}],
    "a status topic": [
        {"role": "user", "content": PROJECT},
        {"role": "assistant", "content": f"{CANARY} Here is the review of the build."}],
    "chained follow-ups": [
        {"role": "user", "content": USER},
        {"role": "assistant", "content": f"{CANARY} first answer"},
        {"role": "user", "content": "why?"},
        {"role": "assistant", "content": f"{CANARY} second answer"}],
    "a long answer": [
        {"role": "user", "content": USER},
        {"role": "assistant", "content": CANARY + " staged" * 2000}],
}
CANARY_PROMPTS = ["tell me more", "current", "why?", "more"]
SHAPES = [None, "topic", "exchange", "EXCHANGE ", "bogus"]


@pytest.mark.parametrize("shape", SHAPES, ids=repr)
def test_no_assistant_text_reaches_a_peer_under_any_shape(client, admin_headers,
                                                          monkeypatch, shape):
    """Every turn below carries the canary in its assistant turns. No peer is
    ever asked it, and the peers' pieces are never ranked against it. Under
    'exchange' the LOCAL lane's query does carry it on every one of these
    turns - which is what makes the peer side's absence a proof and not an
    accident of the table."""
    _shape(monkeypatch, shape)
    exchange = (shape or "").strip().lower() == "exchange"
    turns = 0
    for name, history in CANARY_HISTORIES.items():
        for prompt in CANARY_PROMPTS:
            seen = _turn(client, admin_headers, prompt, history)
            assert len(seen["asked"]) == len(PEERS) and seen["ranked"], (name, prompt)
            for _, query in seen["asked"]:
                assert CANARY not in query, (shape, name, prompt, query)
            for query in seen["ranked"]:
                assert CANARY not in query, (shape, name, prompt, query)
            assert (CANARY in seen["local"][0]) is exchange, (shape, name, prompt)
            turns += 1
    assert turns == len(CANARY_HISTORIES) * len(CANARY_PROMPTS) == 16


# -- The helper itself (FIX: it did not exist) -----------------------------------

TABLE = {
    "none": None,
    "empty": [],
    "user turn only": [{"role": "user", "content": USER}],
    "user and answer": HISTORY,
    "an answer and no user turn": [{"role": "assistant", "content": ANSWER}],
    "only bare user turns": [{"role": "user", "content": "more"},
                             {"role": "assistant", "content": ANSWER}],
    **CANARY_HISTORIES,
}
QUERIES = ["explain", "more", "current", "why?", "  Tell me more  ", "what's next", "and",
           "latest.", "What is the refund policy?", "explain the reranker",
           "now " + "x" * 70, "", "   "]


@pytest.mark.parametrize("shape", [None, "", "topic", "exchange", "EXCHANGE ", "bogus"],
                         ids=repr)
def test_fix_the_peer_query_is_the_topic_shape_whatever_the_key_says(client, monkeypatch,
                                                                      shape):
    """Against the resolver as it was before the exchange shape existed, over
    every history and query in the table: the same answer, under every value
    of the key - and so never a word of an answer."""
    from app.routing import peer_lane_query, resolve_followup
    _shape(monkeypatch, shape)
    compared = 0
    for name, history in TABLE.items():
        for query in QUERIES:
            got = peer_lane_query(query, history)
            assert got == BEFORE.resolve_followup(query, history), (shape, name, query)
            assert CANARY not in got, (shape, name, query)
            compared += 1
    assert compared == len(TABLE) * len(QUERIES)
    # The key really was read by the local lane's resolver under 'exchange'.
    if (shape or "").strip().lower() == "exchange":
        assert CANARY in resolve_followup("tell me more", CANARY_HISTORIES["a long answer"])


def test_fix_the_peer_query_reads_no_shape(client, monkeypatch):
    """The shape is not consulted at all: a key that cannot be read, or one
    that is never asked for, cannot change what a peer is sent."""
    import app.config
    from app.routing import peer_lane_query
    reads: list = []
    real = app.config.get_config
    monkeypatch.setattr(app.config, "get_config",
                        lambda key, default="": reads.append(key) or real(key, default))
    for history in CANARY_HISTORIES.values():
        for query in CANARY_PROMPTS:
            peer_lane_query(query, history)
    assert "followup_resolver" not in reads


def test_fix_an_unresolved_turn_is_the_prompt_itself():
    """Not merely equal: the same object, so nothing about it was rebuilt."""
    from app.routing import peer_lane_query
    for prompt, history in UNRESOLVED:
        assert peer_lane_query(prompt, history) is prompt, prompt


# -- The evaluation replay asks its peers the same way ---------------------------

def _eval_lanes(monkeypatch, run_id, question, setup_turns):
    """One retrieval-only evaluation run of one question with use_peers on.
    Returns (the local retrieval queries, the queries the peer was asked, the
    queries the peer pool was ranked against)."""
    import app.eval_runner as runner
    from app.db import get_session
    from app.models import EvalResult
    local, asked, ranked = [], [], []
    monkeypatch.setattr("app.database.corpus_fingerprint", lambda: "src=0;chunks=0;sha=0")
    monkeypatch.setattr("app.rerank.retrieve",
                        lambda q, top_k=None, user_level=None: local.append(q) or [])
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("app.peers.get_peers", lambda: [dict(PEERS[0])])
    monkeypatch.setattr("app.peers.query_peer_kb",
                        lambda peer, query, n_results=8, timeout=10:
                        asked.append(query) or [dict(PIECE, peer=peer["name"])])
    monkeypatch.setattr("app.rerank.rerank",
                        lambda query, pool, top_k=None, stats=None:
                        ranked.append(query) or list(pool))
    questions = [{"id": 9950, "question": question, "category": "multi-turn",
                  "expected_source": None, "notes": "", "as_level": None,
                  "setup_turns": json.dumps(setup_turns) if setup_turns else None}]
    try:
        runner._run_eval_job(run_id, "2000-01-01T00:00:00", questions, model="m",
                             use_rag=True, n_results=5, retrieval_only=True, use_peers=True)
        state = dict(runner._eval_runs[run_id])
    finally:
        runner._eval_runs.pop(run_id, None)
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.run_id == run_id).delete()
    assert state["complete"] is True and state["failed"] is False, state
    return local, asked, ranked


def test_fix_the_eval_replay_asks_its_peers_the_resolved_follow_up(client, monkeypatch):
    local, asked, ranked = _eval_lanes(monkeypatch, "peer-followup-eval", "tell me more",
                                       HISTORY)
    want = f"{USER} - tell me more"
    assert asked == [want] and ranked == [want] and local == [want]


def test_control_the_eval_replay_asks_a_single_turn_question_in_its_own_words(client,
                                                                             monkeypatch):
    local, asked, ranked = _eval_lanes(monkeypatch, "peer-followup-eval-single",
                                       "tell me more", None)
    assert asked == ["tell me more"] and ranked == ["tell me more"]


def test_no_answer_in_a_scripted_turn_reaches_a_peer_of_the_eval_replay(client, monkeypatch):
    _shape(monkeypatch, "exchange")
    local, asked, ranked = _eval_lanes(monkeypatch, "peer-followup-eval-canary", "tell me more",
                                       CANARY_HISTORIES["an answer after the question"])
    assert CANARY in local[0]
    assert asked and ranked
    assert not any(CANARY in q for q in asked + ranked), (asked, ranked)
