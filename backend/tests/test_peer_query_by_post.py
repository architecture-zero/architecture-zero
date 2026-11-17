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
    # The GET retry holds it tighter (round three): see the next test.
    by_url = parse_qs(urlsplit(get.url).query)["q"][0]
    _clipped_at_a_word(by_url, LONG_WORDS, peers.PEER_GET_QUERY_MAX_BYTES)


def test_fix_the_get_retry_fits_an_older_nginx_request_line(monkeypatch, recorders):
    """FIX (round three, the security read's INFO-3): the GET an older peer is
    asked holds the question to PEER_GET_QUERY_MAX_BYTES, so even three-byte
    letters with no space percent-encode to a request line under nginx's 8k -
    an older peer never answers 414 and trips its breaker for every caller.
    The POST keeps PEER_QUERY_MAX_BYTES."""
    from urllib.parse import parse_qs, urlsplit
    question = chr(0x6587) * 5000                          # 15,000 bytes, no space
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch, route=_by_method(405))
    peers.query_peer_kb(PEER, question)
    post, get = sent[0][1], sent[1][1]
    posted = json.loads(post.body)["q"]
    assert len(posted.encode("utf-8")) > peers.PEER_QUERY_MAX_BYTES - 3
    assert peers.PEER_GET_QUERY_MAX_BYTES == 2500
    by_url = parse_qs(urlsplit(get.url).query)["q"][0]
    assert 2500 - 3 < len(by_url.encode("utf-8")) <= 2500 and posted.startswith(by_url)
    assert len("GET " + get.path_url + " HTTP/1.1") < 8192


def test_fix_a_lone_surrogate_in_the_question_is_posted_not_raised(monkeypatch, recorders):
    """FIX (round three, LOW-1): a lone surrogate has no UTF-8 form, and the
    clip raised on it before anything was sent - the asker raised out of the
    chat's peer lane. It goes as one "?"."""
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch)
    out = peers.query_peer_kb(PEER, "abc " + chr(0xD800) + " def", n_results=2)
    assert [c["peer"] for c in out] == ["Peer One"]
    assert len(sent) == 1 and sent[0][1].method == "POST"
    assert json.loads(sent[0][1].body)["q"] == "abc ? def"
    successes, failures = recorders
    assert len(successes) == 1 and failures == []


def test_fix_the_topic_clip_takes_a_lone_surrogate():
    """FIX (round three, LOW-1): the peer lane's topic clip is the same helper;
    a turn that carries a lone surrogate is clipped, over the bound and under
    it, without raising."""
    from app import routing
    topic = ("word " * 400) + chr(0xDC00) + (" word" * 400)
    got = routing._clip_for_a_peer(topic)
    assert len(got.encode("utf-8")) <= routing.PEER_TOPIC_CLIP_BYTES
    assert routing._clip_for_a_peer("a " + chr(0xD800)) == "a ?"


def test_guard_the_clip_counts_bytes_on_its_early_return():
    """GUARD (round three, LOW-3): 3,000 three-byte letters are 9,000 bytes but
    3,000 characters, so a clip that compared characters to the bound would
    hand them back whole. True on the code before too; it is here because no
    test failed when the early return's byte count was made a character count."""
    got = peers.clip_peer_query(chr(0x20AC) * 3000)
    assert len(got.encode("utf-8")) <= peers.PEER_QUERY_MAX_BYTES
    assert got == chr(0x20AC) * (peers.PEER_QUERY_MAX_BYTES // 3)


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


def test_fix_a_lone_surrogate_in_a_posted_question_is_answered(client, door):
    """FIX (round three, LOW-1): a JSON body can carry a lone surrogate, and
    the door's clip raised on it - a 500 with no data in it. Now the door
    answers, and the store is asked with one "?" in its place."""
    r = client.post("/api/query-kb", content=b'{"q": "abc \\ud800 def", "n": 2}',
                    headers={"X-Peer-Key": KEY, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    assert door == [("abc ? def", 2, None)]


def test_fix_a_posted_body_past_the_peer_door_ceiling_is_refused(client, door):
    """FIX (round three, INFO-6): the POST door parsed up to the default JSON
    ceiling from a keyed peer before it clipped; it has its own, 64 KiB -
    what uvicorn held the GET's whole request head to. Refused before the
    parse, the store never asked."""
    from app.main import PEER_DOOR_BODY_BYTES, app as live_app
    from app.body_limit import BodySizeLimit
    assert PEER_DOOR_BODY_BYTES == 64 * 1024
    m = next(m for m in live_app.user_middleware if m.cls is BodySizeLimit)
    assert m.kwargs["per_path"]["/api/query-kb"] == PEER_DOOR_BODY_BYTES
    big = json.dumps({"q": "x " * 40000}).encode()
    assert len(big) > 64 * 1024
    r = client.post("/api/query-kb", content=big,
                    headers={"X-Peer-Key": KEY, "Content-Type": "application/json"})
    assert r.status_code == 413 and door == []


def test_control_a_posted_question_under_the_ceiling_is_answered(client, door):
    """CONTROL: a body under 64 KiB is read and answered as before."""
    body = json.dumps({"q": "x " * 20000}).encode()
    assert 32 * 1024 < len(body) < 64 * 1024
    r = client.post("/api/query-kb", content=body,
                    headers={"X-Peer-Key": KEY, "Content-Type": "application/json"})
    assert r.status_code == 200 and len(door) == 1
