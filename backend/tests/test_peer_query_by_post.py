"""A peer is asked by POST, the question in the body (2026-11-16).

The peer question rode `GET /api/query-kb?q=...`, and every hop that logs a
request line kept it: the peer's own access log, every proxy in front of it,
and a proxy's error log, which quotes the request line on every upstream
failure. It rides a JSON body now. An older peer serves GET only and answers
a keyed POST with 405; that peer, and only on a 405, is asked once more by
GET, and the breaker records the retry's outcome, never the 405. The question
is held to app.peers.PEER_QUERY_MAX_BYTES of UTF-8 on both sides of the door,
cut at a word, head kept.

The asker's tests replace the HTTP adapter's send one level up, as
test_peers_pinned.py does, so the pinned request is built for real and can be
read; the door's tests go through the app with the peer-key middleware on.

FIX (fails on the code before):
  - the asker POSTs, `q` and `n` in the body and nothing in the URL
  - a 405 is asked once more by GET, and the breaker sees no failure from it
  - a 405 and then a failing GET is ONE failure, the GET's
  - a 401, 403, 404, 500 or 503, or a timeout, does not fall back
  - an over-long question is clipped at a word before it is sent, by either method
  - the door answers a POST with what it answers a GET, for the same question
  - the door clips an over-long question, by either method
  - the door ignores a body field it does not know, and bounds n as the GET does
  - with exposure off, a POST meets the route's fail-closed 403 as a GET does
CONTROL (passes before and after):
  - the GET is still served
  - an unkeyed or wrongly keyed POST is refused by the middleware, as a GET is
"""
import json

import pytest
import requests

import app.auth as auth
from app import peers
from app.routers import chat as chat_router
from tests.test_peers_pinned import PEER, PUBLIC, _capture_sends, _resolver, _response

LONG_WORDS = " ".join(f"word{i:05d}" for i in range(2500))     # 24,999 bytes, all ASCII


@pytest.fixture
def recorders(monkeypatch):
    """The breaker's two recorders, observed rather than run; the circuit is
    always closed."""
    monkeypatch.setattr(peers, "_circuit_open", lambda peer_id: False)
    successes: list = []
    failures: list = []
    monkeypatch.setattr(peers, "_record_success", lambda *a: successes.append(a))
    monkeypatch.setattr(peers, "_record_failure", lambda *a: failures.append(a))
    return successes, failures


def _by_method(post_status, get_status=200):
    """A peer that answers POST with `post_status` and GET with `get_status`."""
    def route(req):
        status = post_status if req.method == "POST" else get_status
        return _response(status=status, payload=None if status == 200 else {"detail": "no"})
    return route


def _clipped_at_a_word(sent: str, whole: str, bound: int):
    assert len(sent.encode("utf-8")) <= bound
    assert whole.startswith(sent) and whole[len(sent)] == " "
    assert len(sent.encode("utf-8")) > bound - len("word00000 ")


# --- the asker -----------------------------------------------------------------

def test_fix_the_asker_posts_the_question_in_the_body(monkeypatch, recorders):
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch)
    out = peers.query_peer_kb(PEER, "what is up", n_results=3)
    assert [c["peer"] for c in out] == ["Peer One"]
    assert len(sent) == 1
    req = sent[0][1]
    assert req.method == "POST"
    assert req.url == f"https://{PUBLIC}/api/query-kb"        # no query string at all
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.body) == {"q": "what is up", "n": 3}
    assert req.headers["Host"] == "peer.example.com"          # still pinned
    successes, failures = recorders
    assert len(successes) == 1 and failures == []


def test_fix_a_405_is_asked_once_more_by_get_and_is_no_failure(monkeypatch, recorders):
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch, route=_by_method(405))
    out = peers.query_peer_kb(PEER, "what is up", n_results=3)
    assert [c["peer"] for c in out] == ["Peer One"]
    assert [r.method for _, r in sent] == ["POST", "GET"]
    get = sent[1][1]
    assert get.url.startswith(f"https://{PUBLIC}/api/query-kb?")
    assert "q=what+is+up" in get.url and "n=3" in get.url and get.body is None
    assert get.headers["Host"] == "peer.example.com"
    successes, failures = recorders
    assert len(successes) == 1 and failures == []


def test_fix_a_405_then_a_failing_get_is_one_failure_the_gets(monkeypatch, recorders):
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch, route=_by_method(405, get_status=500))
    assert peers.query_peer_kb(PEER, "q") == []
    assert [r.method for _, r in sent] == ["POST", "GET"]
    successes, failures = recorders
    assert successes == [] and failures == [("p1", "HTTP 500")]


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_fix_no_other_refusal_falls_back(monkeypatch, recorders, status):
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch, route=_by_method(status))
    assert peers.query_peer_kb(PEER, "q") == []
    assert [r.method for _, r in sent] == ["POST"]
    successes, failures = recorders
    assert successes == [] and failures == [("p1", f"HTTP {status}")]


def test_fix_a_timeout_does_not_fall_back(monkeypatch, recorders):
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    methods = []

    def slow(self, request, **kwargs):
        methods.append(request.method)
        raise requests.exceptions.ReadTimeout("slow peer")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", slow)
    assert peers.query_peer_kb(PEER, "q", timeout=3) == []
    assert methods == ["POST"]
    successes, failures = recorders
    assert successes == [] and failures == [("p1", "timeout after 3s")]


def test_fix_an_over_long_question_is_clipped_at_a_word_by_either_method(monkeypatch,
                                                                         recorders):
    bound = peers.PEER_QUERY_MAX_BYTES
    assert bound == 8000 and len(LONG_WORDS.encode("utf-8")) > 3 * bound
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch, route=_by_method(405))
    peers.query_peer_kb(PEER, LONG_WORDS)
    post, get = sent[0][1], sent[1][1]
    posted = json.loads(post.body)["q"]
    _clipped_at_a_word(posted, LONG_WORDS, bound)
    from urllib.parse import parse_qs, urlsplit
    assert parse_qs(urlsplit(get.url).query)["q"] == [posted]


# --- the door ------------------------------------------------------------------

KEY = "peer-key-for-the-door"


@pytest.fixture
def door(monkeypatch):
    """The peer-key middleware on, one key at the 'public' rung, and the store
    replaced by a recorder that answers two pieces."""
    monkeypatch.setattr(auth, "ECO_EXPOSE_KB", True)
    monkeypatch.setattr(auth, "PEER_KEY_SCOPES", {KEY: "public"})
    asked = []

    def similar(q, n_results, department=None):
        asked.append((q, n_results, department))
        return [{"text": "piece one", "source": "a.md", "score": 0.9, "collection": "kb"},
                {"text": "piece two", "source": "b.md", "score": 0.8, "collection": "kb"}]

    monkeypatch.setattr(chat_router, "query_similar", similar)
    monkeypatch.setattr(chat_router, "list_departments", lambda: [])
    return asked


def test_fix_the_door_answers_a_post_as_it_answers_a_get(client, door):
    keyed = {"X-Peer-Key": KEY}
    by_get = client.get("/api/query-kb", params={"q": "the question", "n": 2}, headers=keyed)
    by_post = client.post("/api/query-kb", json={"q": "the question", "n": 2}, headers=keyed)
    assert by_get.status_code == 200 and by_post.status_code == 200, by_post.text
    assert by_post.json() == by_get.json()
    assert [r["text"] for r in by_post.json()["results"]] == ["piece one", "piece two"]
    assert "collection" not in by_post.json()["results"][0]
    assert door == [("the question", 2, None), ("the question", 2, None)]


def test_control_the_get_is_still_served(client, door):
    r = client.get("/api/query-kb", params={"q": "the question"}, headers={"X-Peer-Key": KEY})
    assert r.status_code == 200 and len(r.json()["results"]) == 2
    assert door == [("the question", 8, None)]


def test_fix_the_door_clips_an_over_long_question_by_either_method(client, door):
    keyed = {"X-Peer-Key": KEY}
    assert client.post("/api/query-kb", json={"q": LONG_WORDS}, headers=keyed).status_code == 200
    assert client.get("/api/query-kb", params={"q": LONG_WORDS}, headers=keyed).status_code == 200
    (by_post, _, _), (by_get, _, _) = door
    assert by_post == by_get
    _clipped_at_a_word(by_post, LONG_WORDS, 8000)


def test_fix_the_door_ignores_unknown_fields_and_bounds_n(client, door):
    keyed = {"X-Peer-Key": KEY}
    for n, served in ((2, 2), (2 ** 70, chat_router._query_kb_max_results()), (0, 1), (-5, 1)):
        r = client.post("/api/query-kb", json={"q": "q", "n": n, "lane": "newer"}, headers=keyed)
        assert r.status_code == 200, (n, r.status_code, r.text)
        assert door[-1][1] == served, n


def test_control_an_unkeyed_post_is_refused_as_an_unkeyed_get_is(client, door):
    for headers in ({}, {"X-Peer-Key": "not-a-key"}):
        by_get = client.get("/api/query-kb", params={"q": "x"}, headers=headers)
        by_post = client.post("/api/query-kb", json={"q": "x"}, headers=headers)
        assert by_get.status_code == by_post.status_code == 401
        assert by_post.json() == by_get.json()
    assert door == []


def test_fix_exposure_off_a_post_meets_the_routes_403(client, door, monkeypatch):
    """The middleware is not the only seal: with exposure off nothing stamps
    a scope, and the handler refuses either method."""
    monkeypatch.setattr(auth, "ECO_EXPOSE_KB", False)
    keyed = {"X-Peer-Key": KEY}
    assert client.get("/api/query-kb", params={"q": "x"}, headers=keyed).status_code == 403
    assert client.post("/api/query-kb", json={"q": "x"}, headers=keyed).status_code == 403
    assert door == []
