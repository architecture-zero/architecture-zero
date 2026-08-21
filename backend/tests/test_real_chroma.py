"""Retrieval + instrument tests against a REAL ChromaDB, not the global mock.

Why this file exists. Every other test in this suite runs against the
chromadb mock installed in conftest, and bugs have shipped that the mock
could not see:

  - the knn health probe (the index-rot tripwire in eval_retrieval's
    pre-flight) reported all healthy collections BROKEN on its first live
    run: it did `got.get("embeddings") or [...]`, and real chroma returns a
    NUMPY ARRAY, whose truth value raises. The fake collection returned a
    plain list, which is truthy, so the failing branch was never executed by
    any test.
  - the same shape sits one call away in database.py's BM25 rescue leg, which
    reads embeddings out of a real `get` and feeds them to _cosine_distance.

A mock encodes what we ASSUMED the dependency does; these tests check what it
actually does. They are deliberately few and fast - this is a realism pin on
the seams where the real type crosses into our code, not a second suite.

The client here is EphemeralClient (in-memory, no disk): conftest patches
`chromadb.PersistentClient` and `chromadb.Settings` in the chromadb namespace,
so the real Settings is imported from chromadb.config and passed explicitly.
Embeddings are a deterministic toy embedder - the point is real chroma
STORAGE/QUERY semantics (numpy returns, include= shapes, hnsw behaviour), not
real embedding quality.
"""
import re

import numpy as np
import pytest

import chromadb
from chromadb.config import Settings as _RealSettings

import app.database as database
from app.database import query_similar

DIM = 8


def _toy_embed(text: str) -> list[float]:
    """Deterministic bag-of-words vector. Not semantic - just stable and
    dimension-consistent, so knn returns a real ordering rather than ties."""
    vec = [0.0] * DIM
    for tok in re.findall(r"\w+", (text or "").lower()):
        vec[sum(ord(c) for c in tok) % DIM] += 1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


NEEDLE_ID = "needle"
NEEDLE_TEXT = "The warehouse gateway IP is 192.0.2.10 - ops runbook."


@pytest.fixture
def real_collection(monkeypatch):
    """A real chroma collection with enough rows that the vector fetch cannot
    reach every chunk - so the BM25 rescue leg has something real to rescue."""
    client = chromadb.EphemeralClient(
        settings=_RealSettings(is_persistent=False, anonymized_telemetry=False))
    # chromadb caches the System per settings object, so every test in this
    # module gets the SAME in-memory instance and the collection outlives the
    # fixture. Drop it first rather than reusing it - a test that inherits the
    # previous test's rows is not measuring what it says it measures.
    try:
        client.delete_collection("knowledge_base")
    except Exception:
        pass
    col = client.create_collection(name="knowledge_base",
                                   metadata={"hnsw:space": "cosine"})

    docs = {f"filler-{i}": (f"Filler chunk {i} about deployment notes and changelogs.",
                            {"source": f"f{i}.md"})
            for i in range(30)}
    docs["rate"] = ("The expedited surcharge is posted on the site.", {"source": "a.md"})
    docs[NEEDLE_ID] = (NEEDLE_TEXT, {"source": "ops-runbook.md"})

    ids = list(docs)
    col.add(ids=ids,
            documents=[docs[i][0] for i in ids],
            metadatas=[docs[i][1] for i in ids],
            embeddings=[_toy_embed(docs[i][0]) for i in ids])

    database._LEX_INDEX.clear()
    monkeypatch.setattr(database, "_get_collection", lambda department=None: col)
    monkeypatch.setattr(database, "_embed", _toy_embed)
    yield col
    database._LEX_INDEX.clear()


# -- The assumption the mocks encode, checked against the real thing ----------

def test_real_chroma_returns_embeddings_as_a_numpy_array(real_collection):
    """The fact that broke the knn probe, pinned. If a future chroma returns
    plain lists here, this fails and tells us the fakes' realism claim (and the
    `is not None` guards written for numpy) need re-checking - rather than us
    finding out from a false alarm in production again."""
    got = real_collection.get(limit=1, include=["embeddings"])
    embs = got.get("embeddings")
    assert isinstance(embs, np.ndarray)
    with pytest.raises(ValueError):
        bool(embs)  # exactly what the old `or` fallback did, per collection


def test_preflight_knn_probe_passes_on_a_real_healthy_collection(monkeypatch,
                                                                 real_collection):
    """End-to-end proof the tripwire no longer false-alarms: this is the code
    path that reported every healthy collection BROKEN on its first live
    run."""
    import sys, os
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "scripts")))
    import eval_retrieval as ev

    class _Client:
        def list_collections(self):
            return [real_collection]

        def get_or_create_collection(self, name, metadata=None):
            return real_collection

    monkeypatch.setattr(database, "client", _Client())
    monkeypatch.setattr(ev, "list_sources", lambda department=None: [
        {"source": "ops-runbook.md", "department": "general", "count": 1}])

    assert ev.preflight([type("Q", (), {"expected_source": "local:ops-runbook.md"})()]) is True


# -- The retrieval seams that read real embeddings ----------------------------

def test_bm25_rescue_leg_survives_real_numpy_embeddings(real_collection):
    """The rescue leg fetches stored embeddings and scores them with
    _cosine_distance. With a real collection those rows are numpy - if any of
    that path regressed to a truthiness check, this is where it shows."""
    out = query_similar("what is the IP 192.0.2.10", n_results=5)
    assert any("192.0.2.10" in r["text"] for r in out)
    assert all(isinstance(r["score"], float) for r in out)
