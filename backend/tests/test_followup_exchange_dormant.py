"""The follow-up resolver's 'exchange' shape ships DORMANT.

A bare follow-up ("explain", "more", "current") carries no subject, so the
retrieval query is rebuilt from the conversation. The 'topic' shape - the
default, and the only shape before this file existed - re-attaches the last
substantive user turn. The 'exchange' shape also carries the start of the last
assistant turn, which names the subject in the corpus's own vocabulary where
the user's question often does not. It is chosen by the config key
`followup_resolver`, read per call.

What "dormant" has to mean, and what this file pins:

CONTROL (passes before the shape existed and after): with the key unset,
blank, `topic`, or any word that is not `exchange`, every resolved query is
BYTE FOR BYTE what the resolver returned before. "Before" is not a formula
written here: tests/fixtures/routing_topic_shape.py is that resolver's module,
copied whole from the commit before the shape was added, and it is run beside
the live one over the same table - bare tokens, real sentences, no history, a
history with no assistant turn, a 5,000-character assistant turn, an assistant
turn of only whitespace - through the function, through the chat's call of it
and through the evaluation replay's call of it. The same control holds the key
itself still: no default row, no environment read, and no way to write it
through the admin config route (it is a config row an operator sets on the
host, like the other per-call keys the runbook names).

FIX (fails before, passes after): with the key at `exchange`, a bare
follow-up's query carries the first 400 characters of the last assistant turn,
whitespace-collapsed, between the topic and the follow-up; the status frame is
still decided on the user's topic alone; every other turn is unchanged; and the
model and the saved history still receive the user's real words.

THE HOSTED RERANKER (the last section). The retrieval query is what the
reranker is sent, so the start of an answer never rides a query that can
reach a hosted vendor: while the hosted rerank provider is the one in effect,
`exchange` is refused and every query resolves as the topic shape - through
the function, the chat and the evaluation replay - and the vendor's stand-in
is handed nothing of the answer. The refusal is said once in the log, and so
is a value of the key that is not recognised. With the local model, or a
scoring endpoint on the operator's own hardware, the shape stays available.
"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

KEY = "followup_resolver"
CLIP = 400   # app.routing.EXCHANGE_CLIP_CHARS

_BEFORE_PATH = Path(__file__).resolve().parent / "fixtures" / "routing_topic_shape.py"


def _load_before():
    spec = importlib.util.spec_from_file_location("routing_topic_shape", _BEFORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BEFORE = _load_before()


class _Turn:
    """A Message-like: the chat hands the resolver objects, not dicts."""
    def __init__(self, role, content):
        self.role, self.content = role, content


USER = "How should a new operator prepare the first knowledge import?"
ANSWER = "Start with the onboarding guide, then stage the files in a folder."
PROJECT = "give me a general review of my project"
LONG_ANSWER = ("The import   runs in three stages.\n\n\tFirst the files are staged; "
               "then they are chunked,\nand last they are embedded.  ") * 45
assert len(LONG_ANSWER) >= 5000

HISTORIES = {
    "none": None,
    "empty": [],
    "user turn only": [{"role": "user", "content": USER}],
    "user and answer": [{"role": "user", "content": USER},
                        {"role": "assistant", "content": ANSWER}],
    "a 5,000-character answer": [{"role": "user", "content": USER},
                                 {"role": "assistant", "content": LONG_ANSWER}],
    "an answer of only whitespace": [{"role": "user", "content": USER},
                                     {"role": "assistant", "content": " \n\t  \r\n "}],
    "an answer and no user turn": [{"role": "assistant", "content": ANSWER}],
    "only bare user turns": [{"role": "user", "content": "more"},
                             {"role": "assistant", "content": ANSWER}],
    "chained follow-ups": [{"role": "user", "content": USER},
                           {"role": "assistant", "content": ANSWER},
                           {"role": "user", "content": "current"},
                           {"role": "assistant", "content": "Here is where it stands today."}],
    "a status topic": [{"role": "user", "content": PROJECT},
                       {"role": "assistant", "content": "Here is the review of the build."}],
    "a blank user turn last": [{"role": "user", "content": USER},
                               {"role": "assistant", "content": ANSWER},
                               {"role": "user", "content": "   "}],
    "message objects": [_Turn("user", USER), _Turn("assistant", ANSWER)],
}

BARE = ["explain", "more", "current", "why?", "  Tell me more  ", "what's next", "and",
        "latest."]
SENTENCES = ["What is the refund policy?", "explain the reranker",
             "Tell me more about the second point, please.",
             "now " + "x" * 70]
EMPTY = ["", "   "]
QUERIES = BARE + SENTENCES + EMPTY

# Every value that must read as the 'topic' shape. None = no row at all.
DORMANT_VALUES = [None, "", "   ", "topic", "TOPIC ", "bogus", "exchanged", "on", "true",
                  "topic exchange"]


def _delete_key():
    from app.db import get_session
    from app.models import Config
    with get_session() as db:
        db.query(Config).filter(Config.key == KEY).delete()


@pytest.fixture
def key():
    """Sets the live config row the way an operator does (set_config), and
    leaves no row behind. Called with None, it makes sure there is no row."""
    from app.config import set_config

    def _set(value):
        _delete_key()
        if value is not None:
            set_config(KEY, value)
    _delete_key()
    yield _set
    _delete_key()


def _live(query, history):
    from app.routing import resolve_followup
    return resolve_followup(query, history)


def _carried(answer):
    return " ".join((answer or "").split())[:CLIP]


def _last_answer(history):
    for m in reversed(history or []):
        role = m.get("role") if isinstance(m, dict) else m.role
        if role == "assistant":
            return m.get("content") if isinstance(m, dict) else m.content
    return ""


# -- CONTROL: the key as shipped ----------------------------------------------

def test_the_key_ships_with_no_row_no_default_and_no_environment_read(client):
    """Nothing turns the shape on: a fresh instance has no `followup_resolver`
    row, config's defaults seed none, and the resolver's module reads no
    environment variable at all."""
    import app.config
    from app.config import get_config
    _delete_key()   # a row an earlier test left would be that test's, not the app's
    app.config.init_config_db()
    assert get_config(KEY, "<no row>") == "<no row>"
    assert KEY not in app.config._DEFAULTS
    source = (Path(__file__).resolve().parents[1] / "app" / "routing.py").read_text(
        encoding="utf-8")
    assert not any(read in source for read in ("import os", "os.environ", "getenv"))


def test_the_admin_config_route_cannot_write_the_key(client, admin_headers):
    """The key is a config row set on the host, not an admin-tab setting: the
    admin write route refuses it by name and stores nothing, so no session -
    whatever it holds - turns the shape on through the API."""
    from app.config import get_config
    _delete_key()
    for value in ("exchange", "topic", ""):
        r = client.patch("/api/admin/config", headers=admin_headers, json={KEY: value})
        assert r.status_code == 400, r.text
        assert KEY in r.json()["detail"]
        assert get_config(KEY, "<no row>") == "<no row>"
    # Beside a key the route does accept: refused whole, nothing written.
    before = get_config("instance_name", "")
    r = client.patch("/api/admin/config", headers=admin_headers,
                     json={"instance_name": "Changed", KEY: "exchange"})
    assert r.status_code == 400, r.text
    assert get_config("instance_name", "") == before
    assert get_config(KEY, "<no row>") == "<no row>"


# -- CONTROL: byte for byte, through the function -----------------------------

@pytest.mark.parametrize("value", DORMANT_VALUES, ids=repr)
def test_with_the_key_not_exchange_every_query_resolves_as_before(client, key, value):
    key(value)
    compared = 0
    for name, history in HISTORIES.items():
        for query in QUERIES:
            assert _live(query, history) == BEFORE.resolve_followup(query, history), (
                value, name, query)
            compared += 1
    assert compared == len(HISTORIES) * len(QUERIES) == 168


def test_the_table_reaches_every_branch_of_the_resolver_as_it_was(client):
    """The table is not a row of fail-open cases: under the resolver as it
    was, it holds queries returned unchanged, the neutral '<topic> - <query>'
    form and the 'current status' frame."""
    outs = {(name, q): BEFORE.resolve_followup(q, h)
            for name, h in HISTORIES.items() for q in QUERIES}
    assert outs[("user and answer", "explain")] == f"{USER} - explain"
    assert outs[("a status topic", "current")] == f"{PROJECT} - current status"
    assert outs[("a status topic", "more")] == f"{PROJECT} - more"
    assert outs[("chained follow-ups", "more")] == f"{USER} - more"
    assert outs[("none", "explain")] == "explain"
    assert outs[("an answer and no user turn", "explain")] == "explain"
    assert outs[("user and answer", "What is the refund policy?")] == "What is the refund policy?"
    rewritten = sum(1 for (name, q), out in outs.items() if out != q)
    assert rewritten == 8 * len(BARE), rewritten


def test_a_config_store_that_cannot_be_read_resolves_as_before(client, monkeypatch):
    """The key's read never becomes a new way for a follow-up to fail: a store
    that raises reads as the 'topic' shape."""
    import app.config

    def _down(*args, **kwargs):
        raise RuntimeError("the config store is down")
    monkeypatch.setattr(app.config, "get_config", _down)
    for name, history in HISTORIES.items():
        for query in QUERIES:
            assert _live(query, history) == BEFORE.resolve_followup(query, history), (
                name, query)


def test_a_turn_that_is_not_a_bare_follow_up_reads_no_config(client, monkeypatch):
    """Dormant also means free: a real question, a first question and a
    follow-up with nothing to attach to never reach the config store."""
    import app.config
    reads = []
    real = app.config.get_config
    monkeypatch.setattr(app.config, "get_config",
                        lambda k, default="": reads.append(k) or real(k, default))
    for query in SENTENCES + EMPTY:
        _live(query, HISTORIES["user and answer"])
    for query in BARE:
        for name in ("none", "empty", "an answer and no user turn", "only bare user turns"):
            _live(query, HISTORIES[name])
    assert reads == []


# -- CONTROL: byte for byte, through the chat and the evaluation replay ---------

CHAT_ROWS = [
    ("explain", "user and answer"),
    ("current", "a status topic"),
    ("Tell me more", "a 5,000-character answer"),
    ("why?", "an answer of only whitespace"),
    ("more", "user turn only"),
    ("more", "chained follow-ups"),
    ("more", "only bare user turns"),
    ("What is the refund policy?", "user and answer"),
    ("explain", "empty"),
]


def _chat_retrieval_query(client, admin_headers, prompt, history, sink=None):
    """One chat turn, retrieval and the model stubbed. Returns the query the
    chat handed to retrieval; `sink` collects the saved rows and the messages
    the model was sent."""
    seen = {}
    sink = {} if sink is None else sink
    sink["saved"] = []

    def _retrieve(query, department=None, top_k=None, user_level=None, stats=None, **kw):
        seen["query"] = query
        return []

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        sink["messages"] = messages
        yield {"type": "text", "text": "ok"}

    with patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.rerank.retrieve", side_effect=_retrieve), \
         patch("app.routers.chat.save_message",
               side_effect=lambda *a, **k: sink["saved"].append(a)):
        r = client.post("/api/chat", headers=admin_headers, json={
            "prompt": prompt, "model": "test-model", "use_rag": True,
            "session_id": "followup-dormant", "history": history or []})
    assert r.status_code == 200, r.text
    return seen["query"]


@pytest.mark.parametrize("value", DORMANT_VALUES, ids=repr)
def test_with_the_key_not_exchange_the_chat_retrieves_as_before(client, admin_headers,
                                                                key, value):
    key(value)
    for prompt, name in CHAT_ROWS:
        history = HISTORIES[name]
        got = _chat_retrieval_query(client, admin_headers, prompt, history)
        assert got == BEFORE.resolve_followup(prompt, history), (value, prompt, name)


def _replay_retrieval_queries(monkeypatch, run_id, rows):
    """A retrieval-only evaluation run over multi-turn questions. Returns the
    query each question handed to retrieval, in order."""
    import app.eval_runner as runner
    from app.db import get_session
    from app.models import EvalResult
    queries = []
    monkeypatch.setattr("app.database.corpus_fingerprint", lambda: "src=0;chunks=0;sha=0")
    monkeypatch.setattr("app.rerank.retrieve",
                        lambda q, top_k=None, user_level=None: queries.append(q) or [])
    monkeypatch.setattr("time.sleep", lambda s: None)
    questions = [{"id": 9900 + i, "question": question, "category": "multi-turn",
                  "expected_source": None, "notes": "", "as_level": None,
                  "setup_turns": json.dumps(setup) if setup else None}
                 for i, (question, setup) in enumerate(rows)]
    try:
        runner._run_eval_job(run_id, "2000-01-01T00:00:00", questions, model="m",
                             use_rag=True, n_results=5, retrieval_only=True)
        state = dict(runner._eval_runs[run_id])
    finally:
        runner._eval_runs.pop(run_id, None)
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.run_id == run_id).delete()
    assert state["complete"] is True and state["failed"] is False, state
    return queries


REPLAY_ROWS = [(prompt, HISTORIES[name]) for prompt, name in CHAT_ROWS]


@pytest.mark.parametrize("value", DORMANT_VALUES, ids=repr)
def test_with_the_key_not_exchange_the_evaluation_replay_retrieves_as_before(
        client, monkeypatch, key, value):
    key(value)
    got = _replay_retrieval_queries(monkeypatch, f"followup-dormant-{DORMANT_VALUES.index(value)}",
                                    REPLAY_ROWS)
    # A question with no scripted turns is asked with its own words, as before.
    want = [BEFORE.resolve_followup(q, setup) if setup else q for q, setup in REPLAY_ROWS]
    assert got == want


# -- CONTROL: what the exchange shape leaves alone -----------------------------

def test_exchange_leaves_every_other_turn_as_it_was(client, key):
    """On, the shape acts on one kind of turn only - a bare follow-up, after a
    substantive user turn, with an assistant turn that says something. A real
    question, an empty message, a follow-up with no topic, with no assistant
    turn, or with an assistant turn of only whitespace all resolve as before."""
    key("exchange")
    unchanged = 0
    for name, history in HISTORIES.items():
        answer = _carried(_last_answer(history))
        for query in QUERIES:
            before = BEFORE.resolve_followup(query, history)
            if before != query and answer:
                continue   # the turns the shape acts on - the FIX cases below
            assert _live(query, history) == before, (name, query)
            unchanged += 1
    # 12 histories x 14 queries, less the 6 histories x 8 bare tokens it acts on.
    assert unchanged == 168 - 48


# -- FIX: the exchange shape ---------------------------------------------------

def test_the_shape_reader_knows_two_shapes(client, key):
    from app.routing import EXCHANGE_CLIP_CHARS, followup_shape
    assert EXCHANGE_CLIP_CHARS == CLIP
    for value in DORMANT_VALUES:
        key(value)
        assert followup_shape() == "topic", value
    for value in ("exchange", "EXCHANGE", "  Exchange \n"):
        key(value)
        assert followup_shape() == "exchange", value


def test_exchange_carries_the_start_of_the_last_answer_after_the_topic(client, key):
    key("exchange")
    assert _live("explain", HISTORIES["user and answer"]) == f"{USER} {ANSWER} - explain"
    assert _live("  Tell me more  ", HISTORIES["message objects"]) == \
        f"{USER} {ANSWER} - Tell me more"
    key("  Exchange \n")
    assert _live("explain", HISTORIES["user and answer"]) == f"{USER} {ANSWER} - explain"


def test_exchange_carries_400_characters_whitespace_collapsed(client, key):
    key("exchange")
    history = HISTORIES["a 5,000-character answer"]
    resolved = _live("explain", history)
    carried = _carried(LONG_ANSWER)
    assert len(carried) == CLIP
    assert "\n" not in carried and "\t" not in carried and "  " not in carried
    assert resolved == f"{USER} {carried} - explain"
    # The shape adds the clip and one space to the query, and never more.
    assert len(resolved) == len(BEFORE.resolve_followup("explain", history)) + CLIP + 1


def test_exchange_acts_on_every_bare_follow_up_in_the_table(client, key):
    """Each of the turns the shape acts on, against the resolver as it was:
    the topic grows by the start of the most recent answer and nothing else
    about the query moves - the frame and the follow-up's own words included."""
    key("exchange")
    acted = 0
    for name, history in HISTORIES.items():
        answer = _carried(_last_answer(history))
        for query in QUERIES:
            before = BEFORE.resolve_followup(query, history)
            if before == query or not answer:
                continue
            topic = BEFORE._last_topic_text(history)
            assert before.startswith(topic + " - ")
            assert _live(query, history) == f"{topic} {answer}{before[len(topic):]}", (name, query)
            acted += 1
    assert acted == 48


def test_exchange_uses_the_most_recent_answer(client, key):
    key("exchange")
    assert _live("more", HISTORIES["chained follow-ups"]) == \
        f"{USER} Here is where it stands today. - more"


def test_the_status_frame_is_decided_on_the_users_topic_not_the_answer(client, key):
    """A factual topic keeps the neutral form even when the ANSWER is full of
    status words - otherwise an answer could lift generated status chunks over
    the narrative the user asked about."""
    from app.routing import is_status_query
    key("exchange")
    history = [{"role": "user", "content": "How does the cross-encoder reranker work?"},
               {"role": "assistant",
                "content": "Current status of the plan: the roadmap's next step is ..."}]
    resolved = _live("current", history)
    assert resolved.endswith(" - current") and not resolved.endswith("current status")
    # And a status topic keeps its frame with the answer aboard.
    resolved = _live("current", HISTORIES["a status topic"])
    assert resolved == f"{PROJECT} Here is the review of the build. - current status"
    assert is_status_query(resolved)


def test_exchange_reaches_retrieval_only_through_the_chat(client, admin_headers, key):
    """The chat's retrieval gets the exchange query; the saved turn and the
    model's own user turn keep the user's real words."""
    key("exchange")
    sink = {}
    history = HISTORIES["a 5,000-character answer"]
    got = _chat_retrieval_query(client, admin_headers, "explain", history, sink)
    assert got == f"{USER} {_carried(LONG_ANSWER)} - explain"
    assert any("explain" in row and "user" in row for row in sink["saved"]), sink["saved"]
    assert not any(got in row for row in sink["saved"])
    user_turns = [m for m in sink["messages"] if m.get("role") == "user"]
    assert user_turns[-1]["content"] == "explain"
    assert all(got not in str(m.get("content", "")) for m in sink["messages"])
    # A real question in the same conversation is retrieved with its own words.
    question = "What is the refund policy?"
    assert _chat_retrieval_query(client, admin_headers, question, history) == question


def test_exchange_reaches_the_evaluation_replay(client, monkeypatch, key):
    key("exchange")
    got = _replay_retrieval_queries(monkeypatch, "followup-exchange-replay", REPLAY_ROWS)
    assert got[0] == f"{USER} {ANSWER} - explain"
    assert got[1] == f"{PROJECT} Here is the review of the build. - current status"
    assert got[2] == f"{USER} {_carried(LONG_ANSWER)} - Tell me more"
    # The rows the shape does not act on are asked as before.
    for i in range(3, len(REPLAY_ROWS)):
        question, setup = REPLAY_ROWS[i]
        want = BEFORE.resolve_followup(question, setup) if setup else question
        if i == 5:   # chained follow-ups: a bare token with an answer to carry
            want = f"{USER} Here is where it stands today. - more"
        assert got[i] == want, i


# -- The exchange shape and a hosted reranker ---------------------------------
# FIX cases fail on the commit that added the shape, where `exchange` was
# applied whatever the reranker was; CONTROL cases pass on both.

SCORER_URL = "http://scorer.invalid/rerank"


class _Scorer:
    """Stands in for requests.post inside the reranker and records every call:
    the hosted vendor's contract ({query, documents} -> results) and the
    self-hosted endpoint's ({query, texts} -> scores)."""
    def __init__(self):
        self.calls = []

    def __call__(self, url, **kw):
        body = kw.get("json") or {}
        self.calls.append({"url": url, "json": body})
        texts = body.get("documents", body.get("texts", []))

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"results": [{"index": i, "relevance_score": 1.0 - i / 10}
                                    for i in range(len(texts))],
                        "scores": [1.0 - i / 10 for i in range(len(texts))]}
        return _Resp()


class _LocalModel:
    """The local cross-encoder, stood in for; records the queries it scores."""
    def __init__(self):
        self.queries = []

    def rerank(self, query, docs):
        self.queries.append(query)
        return [float(len(d)) for d in docs]


def _reranker(monkeypatch, provider, *, latch=True, vendor_key="test-key", vendor=None):
    """The reranker set the way a deployer sets it - the provider key, the host
    latch and the vendor's key - with the vendor's endpoint, the self-hosted
    endpoint and the local model stood in for. The reranker's own decision
    functions run for real. Returns (the endpoint stand-in, the local one)."""
    import app.rag_config as rc
    import app.rerank as rr
    cfg = {"rerank_enabled": "true", "rerank_provider": provider,
           "rerank_remote_url": SCORER_URL}
    if vendor:
        cfg["rerank_hosted_vendor"] = vendor
    monkeypatch.setattr(rr, "_cfg", lambda k, d: cfg.get(k, d))
    monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", latch)
    if vendor_key:
        monkeypatch.setenv("COHERE_API_KEY", vendor_key)
    else:
        monkeypatch.delenv("COHERE_API_KEY", raising=False)
    scorer, local = _Scorer(), _LocalModel()
    monkeypatch.setattr(rr, "requests", type("Requests", (), {"post": scorer}))
    monkeypatch.setattr(rr, "_get_encoder", lambda model=None: local)
    return scorer, local


@pytest.fixture(autouse=True)
def _nothing_said_yet():
    """The shape reader says each thing once per process; every test here
    starts and ends with nothing said."""
    import app.routing as routing
    notices = getattr(routing, "_SHAPE_NOTICES", None)
    if notices is not None:
        notices.clear()
    yield
    if notices is not None:
        notices.clear()


@pytest.fixture
def routing_lines():
    """Every line the routing module logs during the test, in order. Its
    logger does not propagate, so the test hangs a handler of its own on it
    for the length of the test."""
    import logging
    import app.routing  # noqa: F401 - its logger is set up before ours is added
    lines = []

    class _Lines(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())
    logger, handler = logging.getLogger("routing"), _Lines(level=logging.DEBUG)
    logger.addHandler(handler)
    yield lines
    logger.removeHandler(handler)


def _pool():
    from app.database import GLOBAL_COLLECTION
    return [{"text": "Stage the files in a folder before the import.", "source": "docs/a.md",
             "score": 0.9, "collection": GLOBAL_COLLECTION},
            {"text": "The import embeds each chunk.", "source": "docs/b.md",
             "score": 0.8, "collection": GLOBAL_COLLECTION}]


def _chat_turn_through_the_reranker(client, admin_headers, monkeypatch, prompt, history):
    """One chat turn with retrieval's own pipeline running: the store hands
    back a small public pool and the reranker - whichever one the settings
    name - is asked to rank it. Only the model is stubbed."""
    monkeypatch.setattr("app.database.query_similar", lambda query, **kw: _pool())

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        yield {"type": "text", "text": "ok"}

    with patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.save_message", side_effect=lambda *a, **k: None):
        r = client.post("/api/chat", headers=admin_headers, json={
            "prompt": prompt, "model": "test-model", "use_rag": True,
            "session_id": "followup-hosted", "history": history})
    assert r.status_code == 200, r.text


# FIX
def test_with_a_hosted_reranker_exchange_resolves_as_the_topic_shape(client, monkeypatch, key):
    key("exchange")
    _reranker(monkeypatch, "hosted-api")
    compared = 0
    for name, history in HISTORIES.items():
        for query in QUERIES:
            assert _live(query, history) == BEFORE.resolve_followup(query, history), (name, query)
            compared += 1
    assert compared == 168


# FIX
def test_with_a_hosted_reranker_the_chat_and_the_replay_retrieve_as_the_topic_shape(
        client, admin_headers, monkeypatch, key):
    key("exchange")
    _reranker(monkeypatch, "hosted-api")
    for prompt, name in CHAT_ROWS:
        history = HISTORIES[name]
        got = _chat_retrieval_query(client, admin_headers, prompt, history)
        assert got == BEFORE.resolve_followup(prompt, history), (prompt, name)
    got = _replay_retrieval_queries(monkeypatch, "followup-hosted-replay", REPLAY_ROWS)
    assert got == [BEFORE.resolve_followup(q, setup) if setup else q for q, setup in REPLAY_ROWS]


# FIX
def test_nothing_of_the_answer_reaches_the_hosted_reranker(client, admin_headers,
                                                           monkeypatch, key):
    """One chat turn with the hosted path stubbed at its request: the vendor is
    really asked, and what it is handed is the topic-shape query."""
    key("exchange")
    scorer, local = _reranker(monkeypatch, "hosted-api")
    history = HISTORIES["a 5,000-character answer"]
    _chat_turn_through_the_reranker(client, admin_headers, monkeypatch, "explain", history)
    assert len(scorer.calls) == 1 and local.queries == []
    call = scorer.calls[0]
    assert call["url"] == "https://api.cohere.com/v2/rerank"
    assert call["json"]["documents"] == [c["text"] for c in _pool()]
    assert call["json"]["query"] == f"{USER} - explain"
    for piece in ("three stages", "chunked", "embedded", "staged"):
        assert piece not in call["json"]["query"]


# FIX
def test_the_refusal_is_said_once_in_the_log_and_names_no_text(client, monkeypatch,
                                                              routing_lines, key):
    key("exchange")
    _reranker(monkeypatch, "hosted-api")
    for _ in range(3):
        assert _live("explain", HISTORIES["user and answer"]) == f"{USER} - explain"
    lines = list(routing_lines)
    assert len(lines) == 1, lines
    assert KEY in lines[0] and "hosted" in lines[0] and "topic" in lines[0]
    assert USER not in lines[0] and ANSWER not in lines[0] and "explain" not in lines[0]


# FIX
def test_a_value_that_is_not_recognised_is_said_once_in_the_log(client, monkeypatch,
                                                               routing_lines, key):
    """A mistyped value still reads as the topic shape, and now says so - once
    for each value - so an arm set to it is not measured under its label."""
    for value in ("", "topic", "TOPIC "):
        key(value)
        _live("explain", HISTORIES["user and answer"])
    assert routing_lines == []
    key("exhcange")
    for _ in range(3):
        assert _live("explain", HISTORIES["user and answer"]) == f"{USER} - explain"
    lines = list(routing_lines)
    assert len(lines) == 1 and KEY in lines[0] and "exhcange" in lines[0], lines
    key("bogus")
    _live("explain", HISTORIES["user and answer"])
    _live("explain", HISTORIES["user and answer"])
    lines = list(routing_lines)
    assert len(lines) == 2 and "bogus" in lines[1], lines
    assert all(USER not in ln and ANSWER not in ln for ln in lines)


# FIX
def test_a_reranker_decision_that_cannot_be_read_gives_the_topic_shape(client, monkeypatch,
                                                                      routing_lines, key):
    """Fail closed, and say which thing happened: the line names the unreadable
    decision, not a hosted provider nobody configured."""
    import app.rerank as rr
    key("exchange")

    def _unreadable():
        raise RuntimeError("no such table")
    monkeypatch.setattr(rr, "rerank_provider", _unreadable)
    for _ in range(2):
        assert _live("explain", HISTORIES["user and answer"]) == f"{USER} - explain"
    lines = list(routing_lines)
    assert len(lines) == 1, lines
    assert KEY in lines[0] and "could not be read (RuntimeError)" in lines[0]
    assert "no such table" not in lines[0] and "is in effect" not in lines[0]


# CONTROL
@pytest.mark.parametrize("setting", [
    {"provider": "local"},
    {"provider": "remote-http"},
    {"provider": "hosted-api", "latch": False},
    {"provider": "hosted-api", "vendor_key": None},
    {"provider": "hosted-api", "vendor": "nobody"},
], ids=["local", "self-hosted endpoint", "hosted but latched off", "hosted with no key",
        "hosted with an unknown vendor"])
def test_exchange_stays_available_when_no_rerank_query_can_reach_a_vendor(
        client, monkeypatch, key, setting):
    key("exchange")
    _reranker(monkeypatch, **setting)
    assert _live("explain", HISTORIES["user and answer"]) == f"{USER} {ANSWER} - explain"


# CONTROL
def test_with_the_local_model_or_a_self_hosted_endpoint_the_exchange_query_stays_off_the_vendor(
        client, admin_headers, monkeypatch, key):
    key("exchange")
    history = HISTORIES["user and answer"]
    scorer, local = _reranker(monkeypatch, "local")
    _chat_turn_through_the_reranker(client, admin_headers, monkeypatch, "explain", history)
    assert scorer.calls == [] and local.queries == [f"{USER} {ANSWER} - explain"]
    scorer, local = _reranker(monkeypatch, "remote-http")
    _chat_turn_through_the_reranker(client, admin_headers, monkeypatch, "explain", history)
    assert [c["url"] for c in scorer.calls] == [SCORER_URL] and local.queries == []
    assert scorer.calls[0]["json"]["query"] == f"{USER} {ANSWER} - explain"
