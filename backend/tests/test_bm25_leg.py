"""Full-corpus BM25 leg + light stemming.

Covers the three behaviors in app/database.py:
  - _stem/_tokenize: measured vocabulary-mismatch pairs converge, guards keep
    short words / false suffixes / identifiers intact
  - the lexical index: built once per collection, invalidated by every write
    path (add_document / delete_source)
  - query_similar union: a chunk the vector fetch never surfaces is rescued by
    full-corpus BM25, deduped against vector candidates, and disabled cleanly
    when BM25_FETCH=0
"""
import pytest

import app.database as database
from app.database import (
    _stem, _tokenize, _get_lexical_index, _lexical_top_ids,
    _cosine_distance, query_similar,
)


class FakeCol:
    """Minimal chroma-collection stand-in: deterministic vector results
    (insertion order), real get/upsert/delete semantics."""

    def __init__(self, name, rows):
        # rows: dict id -> (text, meta, embedding)
        self.name = name
        self.rows = dict(rows)

    def count(self):
        return len(self.rows)

    def query(self, query_embeddings, n_results, include):
        ids = list(self.rows)[:n_results]
        return {
            "ids": [ids],
            "documents": [[self.rows[i][0] for i in ids]],
            "distances": [[0.5 for _ in ids]],
            "metadatas": [[self.rows[i][1] for i in ids]],
        }

    def get(self, ids=None, include=None, where=None):
        sel = list(ids) if ids is not None else list(self.rows)
        if where and "source" in where:
            sel = [i for i in sel if self.rows[i][1].get("source") == where["source"]]
        out = {"ids": sel}
        for field, pos in (("documents", 0), ("metadatas", 1), ("embeddings", 2)):
            if field in (include or []):
                out[field] = [self.rows[i][pos] for i in sel]
        return out

    def upsert(self, ids, embeddings, documents, metadatas):
        for i, e, d, m in zip(ids, embeddings, documents, metadatas):
            self.rows[i] = (d, m, e)

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)


@pytest.fixture(autouse=True)
def _clean_lexical_cache():
    database._LEX_INDEX.clear()
    yield
    database._LEX_INDEX.clear()


def _use(monkeypatch, col):
    # BOTH collection seams. _get_collection is the get-or-create one;
    # _existing_collection is the read/delete one that declines to create an
    # absent collection (2026-08-26). A stub that patched only the first left
    # delete_source reaching the real client, finding nothing, and returning
    # before it could invalidate the index - the test failed for a reason that
    # had nothing to do with what it asserts. Stub the access, not one door.
    monkeypatch.setattr(database, "_get_collection", lambda department=None: col)
    monkeypatch.setattr(database, "_existing_collection", lambda department=None: col)


# -- Stemming -----------------------------------------------------------------

def test_stem_converges_the_measured_mismatch_pairs():
    # The measured miss class: doc vocabulary vs question vocabulary
    for a, b in [
        ("plugs", "plug"), ("plugging", "plug"),
        ("restarted", "restart"), ("restarting", "restart"), ("restarts", "restart"),
        ("machines", "machine"), ("specs", "spec"),
        ("changed", "change"), ("changes", "change"),
        ("shipped", "ship"), ("queries", "query"), ("stories", "story"),
    ]:
        assert _stem(a) == _stem(b), f"{a!r} and {b!r} should share a stem"


def test_stem_guards_leave_words_and_identifiers_alone():
    for w in ["class", "status", "this", "was", "the", "string", "thing", "us"]:
        assert len(_stem(w)) >= len(w) - 1  # never butchered below the guard
    # false suffixes stay intact
    assert _stem("string") == "string"
    assert _stem("thing") == "thing"
    # digits / identifiers never match a rule
    for tok in ["198", "51", "100", "23", "8080", "v3"]:
        assert _stem(tok) == tok


def test_tokenize_applies_stemming_and_keeps_identifiers():
    assert _tokenize("Restarted the plugs") == ["restart", "the", "plug"]
    assert _tokenize("IP 198.51.100.23") == ["ip", "198", "51", "100", "23"]


# -- Lexical index: build, cache, invalidation --------------------------------

def _corpus_col(name="kb_test"):
    return FakeCol(name, {
        "a": ("The expedited surcharge is posted on the site.", {"source": "a.md"}, [1.0, 0.0]),
        "b": ("The VM external IP is 198.51.100.23 for ops.", {"source": "b.md"}, [0.0, 1.0]),
        "c": ("Background jobs stream through the worker queue.", {"source": "c.md"}, [1.0, 1.0]),
    })


def test_lexical_index_is_cached_per_collection():
    col = _corpus_col()
    idx1 = _get_lexical_index(col)
    idx2 = _get_lexical_index(col)
    assert idx1 is idx2
    assert idx1["N"] == 3 and len(idx1["tf"]) == 3


def test_add_document_invalidates_the_index(monkeypatch):
    col = _corpus_col()
    _use(monkeypatch, col)
    _get_lexical_index(col)
    assert col.name in database._LEX_INDEX
    database.add_document("d", "New fact about promo codes.", {"source": "d.md"})
    assert col.name not in database._LEX_INDEX
    assert _get_lexical_index(col)["N"] == 4  # rebuild sees the new chunk


def test_delete_source_invalidates_the_index(monkeypatch):
    col = _corpus_col()
    _use(monkeypatch, col)
    _get_lexical_index(col)
    database.delete_source("b.md")
    assert col.name not in database._LEX_INDEX
    assert _get_lexical_index(col)["N"] == 2


def test_lexical_top_ids_requires_a_term_match_and_respects_exclude():
    col = _corpus_col()
    assert _lexical_top_ids("198.51.100.23", col, 5, exclude=set()) == ["b"]
    assert _lexical_top_ids("198.51.100.23", col, 5, exclude={"b"}) == []
    assert _lexical_top_ids("zebra xylophone", col, 5, exclude=set()) == []


# -- Cosine distance ----------------------------------------------------------

def test_cosine_distance_conventions():
    assert _cosine_distance([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
    assert _cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
    assert _cosine_distance([0.0, 0.0], [1.0, 0.0]) == 1.0  # zero-norm guard


# -- query_similar union ------------------------------------------------------

def _big_col():
    """12+ chunks so the vector fetch (fetch_k=10 at n_results=5) can NOT
    reach the last row - only the BM25 leg can rescue it."""
    rows = {
        f"filler-{i}": (f"Filler chunk number {i} about unrelated topics.",
                        {"source": f"f{i}.md"}, [0.1, 0.2])
        for i in range(11)
    }
    rows["needle"] = ("The VM external IP is 198.51.100.23 - ops runbook.",
                      {"source": "ops-runbook.md"}, [0.9, 0.1])
    return FakeCol("knowledge_base", rows)


def test_bm25_leg_rescues_a_chunk_the_vector_fetch_missed(monkeypatch):
    _use(monkeypatch, _big_col())
    out = query_similar("what is the IP 198.51.100.23", n_results=5)
    assert any("198.51.100.23" in r["text"] for r in out), \
        "full-corpus BM25 must union the exact-identifier chunk into the pool"


def test_bm25_leg_disabled_when_fetch_is_zero(monkeypatch):
    _use(monkeypatch, _big_col())
    monkeypatch.setattr(database, "BM25_FETCH", 0)
    out = query_similar("what is the IP 198.51.100.23", n_results=5)
    assert not any("198.51.100.23" in r["text"] for r in out)


def test_union_dedupes_vector_and_lexical_hits(monkeypatch):
    col = _corpus_col("knowledge_base")  # 3 rows: vector fetch returns ALL of them
    _use(monkeypatch, col)
    out = query_similar("what is 198.51.100.23", n_results=10)
    hits = [r for r in out if "198.51.100.23" in r["text"]]
    assert len(hits) == 1, "chunk in both legs must appear exactly once"


# -- Fusion-leg IDF: pool-local BY MEASUREMENT, not by accident ---------------
# An outside review correctly showed pool-local IDF inverts intent (the
# topic-defining term, present in every candidate, scores ~0). The
# corpus-stats fix was implemented and A/B'd on a live corpus twice (IDF-only,
# then IDF+avg_dl): both variants scored at or slightly below baseline - zero
# misses converted, one boundary hit lost, rank-1 unchanged. The
# cross-encoder, not this ordering, decides the final top-5. Re-open only
# with a new measurement that clears the baseline - not on theory, however
# correct.

# -- knn halve-and-retry (small-graph hnswlib refusal) ------------------------

class FussyCol(FakeCol):
    """Refuses knn queries above a threshold k, like hnswlib on a small graph
    when the request approaches the index's element count."""

    def __init__(self, name, rows, max_k):
        super().__init__(name, rows)
        self.max_k = max_k
        self.asked = []

    def query(self, query_embeddings, n_results, include):
        self.asked.append(n_results)
        if n_results > self.max_k:
            raise RuntimeError(
                "Cannot return the results in a contigious 2D array. "
                "Probably ef or M is too small")
        return super().query(query_embeddings, n_results, include)


def test_knn_retry_halves_k_instead_of_dropping_the_vector_leg(monkeypatch):
    col = FussyCol("kb_history", _corpus_col().rows, max_k=2)
    _use(monkeypatch, col)
    out = query_similar("expedited surcharge", n_results=5)
    assert out, "halved retry must restore the vector leg, not return nothing"
    assert col.asked == [3, 1], "k halves from min(fetch_k, count) until accepted"


def test_unrelated_query_errors_still_skip_the_collection(monkeypatch):
    class BrokenCol(FakeCol):
        def query(self, *a, **kw):
            raise ValueError("boom")

    _use(monkeypatch, BrokenCol("knowledge_base", _corpus_col().rows))
    out = query_similar("expedited surcharge", n_results=5)
    assert out == []  # skipped cleanly - no retry loop, no crash
