"""The hosted-rerank tier rule (2026-11-10): corpus text from any collection
but the global one never goes to a third-party vendor.

THE GAP. The hosted reranker (rerank_provider=hosted-api) sits behind the
RERANK_HOSTED_ALLOWED host latch, and once a deployer opened the latch,
rerank() sent the vendor whatever pool it was handed. retrieve() hands it the
caller's whole pool: the global collection plus the caller's department, any
routed department and the floor departments the caller is cleared for
(restricted, on an Owner's turn). The peer lane hands it the pieces the peers
sent. So the latch opened the vendor to every collection at once.

THE RULE. _score_hosted sends a pool only when every candidate was read from
the global collection, the shared floor every rung reads. query_similar stamps
each candidate with the collection it was read from, on both of its legs (the
vector search and the full-corpus BM25 rescue). A peer's piece is never
public, whatever it claims, and a candidate with no stamp is never public. A
refused pool ranks on the local model (else the retriever's order), and the
log and the stats say why. The Eco Mode door drops the stamp, so its answer
keeps the fields it had.

FIX cases fail on the code before the rule: the vendor IS called, or the stamp
is missing. CONTROL cases hold on both; each names the part of the change
whose removal it catches.
"""
import json
import types

import pytest

import app.database as database
import app.rag_config as rc
import app.rerank as rr
from app.permissions import GUEST_LEVEL

PUBLIC = database.GLOBAL_COLLECTION      # the global collection: the public tier
DEPT = "kb_restricted"                   # the Owner-only floor department
TEXTS = ["short", "the longest text of them all", "mid text"]
# The local model's stand-in scores by length, so its order is visible.
LOCAL_ORDER = ["the longest text of them all", "mid text", "short"]
# What the vendor would answer, were it asked: an order the local stand-in
# never gives, so the order a test gets back says who ranked.
VENDOR = {"results": [{"index": 0, "relevance_score": 0.9},
                      {"index": 1, "relevance_score": 0.1},
                      {"index": 2, "relevance_score": 0.5}]}
VENDOR_ORDER = ["short", "mid text", "the longest text of them all"]


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Post:
    """Stands in for requests.post and records every call it receives. With no
    payload given it ranks the documents it was sent last to first, sized to
    the request."""
    def __init__(self, payload=None):
        self.calls, self._payload = [], payload

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if self._payload is not None:
            return _Resp(self._payload)
        n = len(kw["json"]["documents"])
        return _Resp({"results": [{"index": i, "relevance_score": float(i)}
                                  for i in range(n)]})


class _Len:
    """The local model, stood in for: it scores by length."""
    def rerank(self, query, docs):
        return [float(len(d)) for d in docs]


class _Lines:
    """Stands in for the module's logger and keeps what it is told."""
    def __init__(self):
        self.lines = []

    def warning(self, msg, *args):
        self.lines.append(msg % args if args else msg)

    info = warning


def _hosted(monkeypatch, payload=None, provider="hosted-api"):
    """The hosted arm opened as a deployer opens it (the provider key, the host
    latch and the vendor's key), with the vendor's endpoint and the local model
    stood in for."""
    cfg = {"rerank_enabled": "true", "rerank_provider": provider,
           "rerank_remote_url": "http://scorer.invalid/rerank"}
    monkeypatch.setattr(rr, "_cfg", lambda k, d: cfg.get(k, d))
    monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", True)
    monkeypatch.setenv("COHERE_API_KEY", "test-key")
    post = _Post(payload)
    monkeypatch.setattr(rr, "requests", type("Requests", (), {"post": post}))
    monkeypatch.setattr(rr, "_get_encoder", lambda model=None: _Len())
    return post


def _pool(*tiers):
    """TEXTS, each stamped with the collection named (None: no stamp)."""
    return [{"text": t, **({"collection": c} if c else {})} for t, c in zip(TEXTS, tiers)]


# -- the rule, at rerank() ------------------------------------------------------

def test_fix_one_department_candidate_keeps_the_whole_pool_off_the_vendor(monkeypatch):
    post = _hosted(monkeypatch, payload=VENDOR)
    stats = {}
    out = rr.rerank("q", _pool(PUBLIC, DEPT, PUBLIC), top_k=3, stats=stats)
    assert post.calls == [], "a department's chunk must never reach the vendor"
    # A valid ordering all the same: the local model ranked the turn.
    assert [c["text"] for c in out] == LOCAL_ORDER
    assert all("rerank_score" in c for c in out)
    assert stats["rerank_provider"] == "local-fallback"
    # The stats say why in plain words: a count and the collection's name, no text.
    why = stats["rerank_hosted_skipped"]
    assert "1 of 3" in why and DEPT in why
    assert "non-public tier never goes to a third-party vendor" in why
    assert not any(t in why for t in TEXTS)


def test_fix_the_log_says_why_the_vendor_was_not_called(monkeypatch):
    post = _hosted(monkeypatch, payload=VENDOR)
    said = _Lines()
    monkeypatch.setattr(rr, "log", said)
    rr.rerank("q", _pool(PUBLIC, PUBLIC, DEPT), top_k=3)
    assert post.calls == []
    line = next(l for l in said.lines if "hosted rerank NOT called" in l)
    assert "1 of 3" in line and DEPT in line and "non-public tier" in line
    assert any("held back by the tier rule" in l for l in said.lines)
    assert not any(t in l for l in said.lines for t in TEXTS)


def test_fix_a_candidate_with_no_stamp_is_not_public(monkeypatch):
    """Fail closed: a candidate nothing stamped is not proven public."""
    post = _hosted(monkeypatch, payload=VENDOR)
    stats = {}
    out = rr.rerank("q", _pool(PUBLIC, None, PUBLIC), top_k=3, stats=stats)
    assert post.calls == []
    assert [c["text"] for c in out] == LOCAL_ORDER
    assert "1 with no tier information" in stats["rerank_hosted_skipped"]


def test_fix_a_peers_piece_is_never_public_whatever_it_says(monkeypatch):
    """query_peer_kb returns a peer's raw JSON with `peer` stamped on it, so a
    `collection` on such a piece is the peer's claim, not this instance's read."""
    post = _hosted(monkeypatch, payload=VENDOR)
    stats = {}
    claimed = [{**c, "peer": "a-peer"} for c in _pool(PUBLIC, PUBLIC, PUBLIC)]
    out = rr.rerank("q", claimed, top_k=3, stats=stats)
    assert post.calls == []
    assert [c["text"] for c in out] == LOCAL_ORDER
    assert "3 of 3" in stats["rerank_hosted_skipped"]
    assert "a peer's piece" in stats["rerank_hosted_skipped"]


def test_fix_the_peer_lane_never_hands_the_vendor_a_peers_pieces(monkeypatch):
    """The chat's peer lane (and the eval's, the same function) ranks what the
    peers sent with this instance's own rerank: peer_boundary.select(...,
    rank=rerank). Those pieces are a peer's corpus, never this instance's
    public collection."""
    from app import peer_boundary
    post = _hosted(monkeypatch, payload=VENDOR)
    sent = [{"text": t, "source": f"peer-{i}.md", "score": 0.9, "peer": "a-peer"}
            for i, t in enumerate(TEXTS)]
    kept = peer_boundary.select("q", sent, 0.4, rank=rr.rerank)
    assert post.calls == [], "a peer's pieces must never reach the vendor"
    assert [c["text"] for c in kept] == LOCAL_ORDER


def test_fix_the_help_lane_is_not_the_public_collection(monkeypatch):
    """The rule names ONE collection. The help lane reads the help collection
    alone, which is not it, so a help turn ranks on the local model."""
    post = _hosted(monkeypatch, payload=VENDOR)
    stats = {}
    out = rr.rerank("q", _pool(*[database.HELP_COLLECTION] * 3), top_k=3, stats=stats)
    assert post.calls == []
    assert [c["text"] for c in out] == LOCAL_ORDER
    assert database.HELP_COLLECTION in stats["rerank_hosted_skipped"]


def test_fix_with_no_local_model_either_the_retrievers_order_stands(monkeypatch):
    post = _hosted(monkeypatch, payload=VENDOR)
    monkeypatch.setattr(rr, "_get_encoder", lambda model=None: None)
    pool = _pool(DEPT, DEPT, PUBLIC)
    stats = {}
    assert rr.rerank("q", pool, top_k=2, stats=stats) == pool[:2]
    assert post.calls == []
    assert stats["rerank_provider"] == "none"
    assert "2 of 3" in stats["rerank_hosted_skipped"]


def test_fix_the_vendor_call_itself_refuses_what_is_not_proven_public(monkeypatch):
    """The rule sits in the function that makes the call, so a caller written
    later inherits it: no tiers, too few, a department, an unknown one, a
    near-miss spelling, each refused before a request is built."""
    post = _hosted(monkeypatch, payload=VENDOR)
    for tiers in (None, [], [PUBLIC, PUBLIC], [PUBLIC, PUBLIC, DEPT],
                  [PUBLIC, None, PUBLIC], [PUBLIC, PUBLIC, PUBLIC.upper()]):
        with pytest.raises(rr.HostedTierRefused):
            rr._score_hosted("q", TEXTS, tiers)
    with pytest.raises(rr.HostedTierRefused):
        rr._score("hosted-api", "q", TEXTS)
    assert post.calls == []
    # CONTROL, at the same door: every text proven public, so the call is made.
    assert rr._score_hosted("q", TEXTS, [PUBLIC] * 3) == [0.9, 0.1, 0.5]
    assert len(post.calls) == 1


def test_fix_an_unreadable_public_tier_sends_nothing(monkeypatch):
    post = _hosted(monkeypatch, payload=VENDOR)
    monkeypatch.setattr(rr, "_public_tier", lambda: None)
    stats = {}
    out = rr.rerank("q", _pool(PUBLIC, PUBLIC, PUBLIC), top_k=3, stats=stats)
    assert post.calls == []
    assert [c["text"] for c in out] == LOCAL_ORDER
    assert "could not be read" in stats["rerank_hosted_skipped"]


def test_control_an_all_public_pool_still_goes_to_the_vendor(monkeypatch):
    """What the rule leaves alone: a pool read wholly from the global collection
    is the hosted arm's to rank, as it was."""
    post = _hosted(monkeypatch, payload=VENDOR)
    stats = {}
    out = rr.rerank("q", _pool(PUBLIC, PUBLIC, PUBLIC), top_k=3, stats=stats)
    assert len(post.calls) == 1
    assert post.calls[0]["json"]["documents"] == TEXTS
    assert [c["text"] for c in out] == VENDOR_ORDER          # the vendor ranked
    assert stats["rerank_provider"] == "hosted-api"
    assert "rerank_hosted_skipped" not in stats


def test_control_the_local_provider_ranks_a_department_pool_as_before(monkeypatch):
    """The rule is about the vendor. On the local provider nothing leaves the
    box, so a department pool is ranked as it always was and no reason is kept."""
    post = _hosted(monkeypatch, payload=VENDOR, provider="local")
    stats = {}
    out = rr.rerank("q", _pool(DEPT, DEPT, None), top_k=3, stats=stats)
    assert post.calls == []
    assert [c["text"] for c in out] == LOCAL_ORDER
    assert stats["rerank_provider"] == "local"
    assert "rerank_hosted_skipped" not in stats


def test_control_the_operators_own_scorer_is_not_the_vendor(monkeypatch):
    """The rule is the third-party vendor's. remote-http posts to the scoring
    endpoint the operator runs (a GPU box), and a department pool still goes
    there as it did."""
    post = _hosted(monkeypatch, payload={"scores": [0.9, 0.1, 0.5]}, provider="remote-http")
    stats = {}
    out = rr.rerank("q", _pool(DEPT, DEPT, PUBLIC), top_k=3, stats=stats)
    assert [call["url"] for call in post.calls] == ["http://scorer.invalid/rerank"]
    assert [c["text"] for c in out] == VENDOR_ORDER
    assert stats["rerank_provider"] == "remote-http"
    assert "rerank_hosted_skipped" not in stats


def test_control_the_status_self_test_still_reaches_the_vendor(monkeypatch):
    """status() scores two literals in this module, not corpus text, and sends
    them as the public tier, so an open hosted arm still reads as loaded. Fails
    if the self-test sends no tiers: the rule would refuse it."""
    _hosted(monkeypatch, payload={"results": [{"index": 0, "relevance_score": 0.9},
                                              {"index": 1, "relevance_score": 0.1}]})
    info = rr.status()
    assert info["provider"] == "hosted-api" and info["error"] is None
    assert info["loaded"] is True and info["self_test"]["ranks_correct"] is True


# -- the stamp, at the database seam --------------------------------------------

class _Col:
    """A chroma collection stand-in with both of query_similar's legs: the
    vector search answers its first rows in order, and get() serves the BM25
    leg (the index build, then the rescued rows' fetch)."""
    def __init__(self, name, rows):
        # rows: (id, text, source, embedding)
        self.name = name
        self._rows = {i: (t, {"source": s}, e) for i, t, s, e in rows}

    def count(self):
        return len(self._rows)

    def query(self, query_embeddings=None, n_results=None, include=None, **kw):
        ids = list(self._rows)[:n_results]
        return {"ids": [ids],
                "documents": [[self._rows[i][0] for i in ids]],
                "distances": [[0.5] * len(ids)],
                "metadatas": [[self._rows[i][1] for i in ids]]}

    def get(self, ids=None, include=None, **kw):
        sel = list(ids) if ids is not None else list(self._rows)
        out = {"ids": sel}
        for field, pos in (("documents", 0), ("metadatas", 1), ("embeddings", 2)):
            if field in (include or []):
                out[field] = [self._rows[i][pos] for i in sel]
        return out


class _Client:
    def __init__(self, cols):
        self._cols = {c.name: c for c in cols}

    def list_collections(self):
        return list(self._cols.values())

    def get_or_create_collection(self, name, metadata=None):
        return self._cols[name]


PUBLIC_ROWS = [("p1", "public faq text about signing in", "faq.md", [1.0, 0.0]),
               ("p2", "public pricing text about plans", "pricing.md", [1.0, 0.0])]
DEPT_ROWS = [("r1", "internal runbook text about the server", "internal/runbook.md",
              [1.0, 0.0])]
# Eleven chunks the vector search reaches first and a twelfth only the BM25
# leg can: at n_results=5 query_similar asks each collection for ten.
FILLER = [(f"f{i}", f"filler chunk number {i}", f"f{i}.md", [0.0, 1.0]) for i in range(11)]
NEEDLE = ("needle", "the server address is 198.51.100.23", "ops.md", [1.0, 0.0])


@pytest.fixture(autouse=True)
def _fresh_lexical_index(monkeypatch):
    """The BM25 leg caches its index by collection NAME, process-wide: these
    stand-ins must not meet another test's cached index, or leave theirs."""
    monkeypatch.setattr(database, "_LEX_INDEX", {})


def _store(monkeypatch, public_rows=PUBLIC_ROWS, dept_rows=DEPT_ROWS):
    """This template's shape, the global collection beside the Owner-only
    restricted department, behind the REAL query_similar: the stamp a test
    reads is the one production writes."""
    monkeypatch.setattr(database, "client", _Client([_Col(PUBLIC, public_rows),
                                                     _Col(DEPT, dept_rows)]))
    monkeypatch.setattr(database, "_embed", lambda *a, **k: [1.0, 0.0])


def test_fix_query_similar_stamps_each_candidate_with_its_collection(monkeypatch):
    _store(monkeypatch)
    out = database.query_similar("text", n_results=5, department="restricted")
    assert {c["text"]: c["collection"] for c in out} == {
        PUBLIC_ROWS[0][1]: PUBLIC, PUBLIC_ROWS[1][1]: PUBLIC, DEPT_ROWS[0][1]: DEPT}


def test_fix_a_chunk_the_bm25_leg_rescued_carries_its_collection(monkeypatch):
    """This template's second leg: full-corpus BM25 unions in a chunk the
    vector search never reached, and that chunk is stamped like the rest."""
    _store(monkeypatch, dept_rows=FILLER + [NEEDLE])
    out = database.query_similar("198.51.100.23", n_results=5, department="restricted")
    rescued = next(c for c in out if c["text"] == NEEDLE[1])
    assert rescued["collection"] == DEPT
    assert {c["collection"] for c in out if c["text"] in {r[1] for r in PUBLIC_ROWS}} == {PUBLIC}


def test_fix_the_stamp_stays_with_its_text_through_the_ghost_filter_and_the_sort():
    """The stamp is only worth reading if it cannot slide onto another text: a
    ghost row (no document) is dropped and the rest re-sorted, each with its own."""
    out = database._hybrid_rank(
        ["public one", None, "internal alpha", "public two"], [0.5, 0.1, 0.2, 0.3],
        [{"source": "a.md"}, None, {"source": "b.md"}, {"source": "c.md"}], "alpha", 5,
        collections=[PUBLIC, PUBLIC, DEPT, PUBLIC])
    assert {c["text"]: c["collection"] for c in out} == {
        "public one": PUBLIC, "internal alpha": DEPT, "public two": PUBLIC}
    assert out[0]["text"] == "internal alpha"       # the sort really moved it


def test_fix_no_stamp_is_written_on_a_guess():
    """No names, not one name per text, a name that is not a string, or only a
    metadata field that says so: no stamp, and to the rule no stamp is not
    public."""
    assert "collection" not in database._hybrid_rank(["x"], [0.2], [{"source": "s"}], "x", 5)[0]
    short = database._hybrid_rank(["x", "y"], [0.2, 0.3], [{}, {}], "x", 5,
                                  collections=[PUBLIC])
    assert len(short) == 2 and all("collection" not in c for c in short)
    odd = database._hybrid_rank(["x"], [0.2], [{}], "x", 5, collections=[123])
    assert "collection" not in odd[0]
    claimed = database._hybrid_rank(["x"], [0.2], [{"source": "s", "collection": PUBLIC}],
                                    "x", 5, collections=[None])
    assert "collection" not in claimed[0]


# -- the rule through the real seam: retrieve() -> query_similar -> rerank() ----

def test_fix_an_owner_turn_with_a_restricted_chunk_never_reaches_the_vendor(monkeypatch):
    """retrieve() at the Owner's level (the chat's, for the Owner, and every
    trusted caller's default): the floor adds the restricted department, so the
    pool is the global collection plus kb_restricted, and none of it is sent."""
    _store(monkeypatch)
    post = _hosted(monkeypatch)
    kept = rr.retrieve("text")
    assert post.calls == [], "the restricted runbook reached the vendor"
    assert {c["text"]: c["collection"] for c in kept} == {
        PUBLIC_ROWS[0][1]: PUBLIC, PUBLIC_ROWS[1][1]: PUBLIC, DEPT_ROWS[0][1]: DEPT}
    assert all("rerank_score" in c for c in kept)       # ranked, by the local model


def test_control_a_guest_turn_still_reranks_on_the_vendor(monkeypatch):
    """A guest's turn reads the global collection alone (the restricted floor is
    Owner-only), so its pool is all public and the vendor ranks it, as before.
    Fails if the vector search stops stamping what it reads."""
    _store(monkeypatch)
    post = _hosted(monkeypatch)
    kept = rr.retrieve("text", user_level=GUEST_LEVEL)
    assert len(post.calls) == 1
    sent = post.calls[0]["json"]["documents"]
    assert sorted(sent) == sorted(r[1] for r in PUBLIC_ROWS)
    assert [c["text"] for c in kept] == list(reversed(sent))     # the vendor's order


def test_control_a_guest_turn_with_a_bm25_rescued_chunk_still_reaches_the_vendor(monkeypatch):
    """The same guest turn when one of its public chunks came through the BM25
    leg rather than the vector search: still all public, still the vendor's.
    Fails if the BM25 leg stops stamping what it rescues."""
    _store(monkeypatch, public_rows=FILLER + [NEEDLE])
    monkeypatch.setattr(rr, "RERANK_FETCH", 5)     # ten asked: the vector search stops short
    post = _hosted(monkeypatch)
    rr.retrieve("what is 198.51.100.23", user_level=GUEST_LEVEL)
    assert len(post.calls) == 1, "an all-public pool was kept off the vendor"
    assert NEEDLE[1] in post.calls[0]["json"]["documents"]


# -- the Eco Mode door ----------------------------------------------------------

def test_control_the_peer_door_answers_without_the_stamp(monkeypatch):
    """The stamp is this instance's own bookkeeping and does not cross the door:
    at the 'owner' scope, which reads a department, the answer keeps exactly the
    fields it always had and names no collection. Fails with the door's strip
    removed."""
    from app.routers.chat import query_kb_for_peer
    _store(monkeypatch)
    req = types.SimpleNamespace(state=types.SimpleNamespace(peer_scope="owner"))
    body = query_kb_for_peer(req, q="text", n=8)
    assert {p["text"] for p in body["results"]} == {r[1] for r in PUBLIC_ROWS + DEPT_ROWS}
    for piece in body["results"]:
        assert set(piece) == {"text", "source", "score", "auto_generated", "trust",
                              "injection_flagged"}
    served = json.dumps(body)
    assert DEPT not in served and PUBLIC not in served
