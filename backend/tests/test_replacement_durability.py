"""A failed write must never be worse than no write at all.

Three paths used to delete the known-good copy BEFORE the replacement was
indexed, so any failure in between - an embed timeout, a Chroma upsert error,
the process dying - turned "update this document" into "delete this document".
The upload path caught only QuarantinedContent, so ordinary infrastructure
failures escaped with the old copy already gone.

The rule these pin: add first, prune last. Worst case the index briefly holds
both versions of a document, which retrieval reads as the same source. There is
no case where it holds neither.
"""
import re
from unittest.mock import patch

import pytest

import chromadb
from chromadb.config import Settings as _RealSettings

import app.database as database

DIM = 8


def _toy_embed(text: str) -> list[float]:
    """Deterministic, dimension-consistent. Same approach as test_real_chroma."""
    vec = [0.0] * DIM
    for tok in re.findall(r"\w+", (text or "").lower()):
        vec[sum(ord(c) for c in tok) % DIM] += 1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


@pytest.fixture
def real_collection(monkeypatch):
    """A REAL chroma collection, because the conftest mock stores nothing.

    These tests assert what survives a failure, so they need a store that
    actually holds rows between calls - against the mock, "the old version is
    still there" and "everything was deleted" look identical.
    """
    cl = chromadb.EphemeralClient(
        settings=_RealSettings(is_persistent=False, anonymized_telemetry=False))
    try:
        cl.delete_collection("knowledge_base")
    except Exception:
        pass
    col = cl.create_collection(name="knowledge_base", metadata={"hnsw:space": "cosine"})
    database._LEX_INDEX.clear()
    monkeypatch.setattr(database, "_get_collection", lambda department=None: col)
    # Both seams: get_source_ids reads through _existing_collection, which
    # would otherwise escape this fixture and consult the real module client.
    monkeypatch.setattr(database, "_existing_collection", lambda department=None: col)
    monkeypatch.setattr(database, "_embed", _toy_embed)
    yield col
    database._LEX_INDEX.clear()


def _upload(client, headers, name, body, department="general"):
    return client.post("/api/ingest/upload",
                       files={"file": (name, body, "text/plain")},
                       data={"department": department},
                       headers=headers)


# -- The upload replacement path ----------------------------------------------

def test_failed_replacement_leaves_the_old_version_intact(client, admin_headers, real_collection):
    """The core promise. A mid-replacement failure must not lose the document.

    The failure is injected into add_document, which is where an embed timeout
    or an upsert error would actually surface.
    """
    name = "durability_doc.txt"
    assert _upload(client, admin_headers, name, b"the original content").status_code == 200

    from app.database import get_source_ids
    before = set(get_source_ids(name, "general"))
    assert before, "setup failed - the original never indexed"

    with patch("app.routers.kb.add_document", side_effect=RuntimeError("embed backend down")):
        with pytest.raises(RuntimeError):
            _upload(client, admin_headers, name, b"a replacement that will not land")

    after = set(get_source_ids(name, "general"))
    assert after == before, (
        "the document changed when its replacement failed - the delete ran before the write landed")


def test_successful_replacement_still_replaces(client, admin_headers, real_collection):
    """Durability must not have been bought by never pruning."""
    from app.database import get_source_ids
    name = "durability_swap.txt"
    assert _upload(client, admin_headers, name, b"generation one text").status_code == 200
    first = set(get_source_ids(name, "general"))
    assert first

    assert _upload(client, admin_headers, name, b"generation two text").status_code == 200
    second = set(get_source_ids(name, "general"))
    assert second
    # Content-addressed ids: different text means different ids, and the old
    # generation must be gone rather than accumulating beside the new one.
    assert second != first
    assert not (first & second), "stale chunks from the previous version survived the swap"


def test_reupload_of_identical_content_is_a_noop_not_a_rewrite(client, admin_headers, real_collection):
    """Content-addressed ids make an unchanged re-upload a no-op."""
    from app.database import get_source_ids
    name = "durability_same.txt"
    _upload(client, admin_headers, name, b"unchanged body")
    ids_a = set(get_source_ids(name, "general"))
    _upload(client, admin_headers, name, b"unchanged body")
    ids_b = set(get_source_ids(name, "general"))
    assert ids_a == ids_b


# -- The quarantine release path ----------------------------------------------

def test_failed_release_stays_held_and_records_why(client, admin_headers):
    """A release that fails must not report success.

    status used to be flipped to "released" and committed BEFORE the re-ingest,
    with no exception handling on the path at all - so a failure left the review
    record claiming the content was live when it was neither indexed nor held.
    """
    from app.db import get_session
    from app.models import QuarantinedDoc

    with get_session() as db:
        row = QuarantinedDoc(source="held_doc.txt", department="general",
                             trust_tier="untrusted", text="some withheld text",
                             findings="[]", status="held", created_at="2026-01-01T00:00:00")
        db.add(row)
        db.flush()
        item_id = row.id

    with patch("app.routers.kb.add_document", side_effect=RuntimeError("embed backend down")):
        r = client.post(f"/api/admin/kb/quarantine/{item_id}/release", headers=admin_headers)
    assert r.status_code == 500, r.text

    with get_session() as db:
        row = db.get(QuarantinedDoc, item_id)
        assert row.status == "held", "a failed release reported itself as released"
        assert row.release_error, "the failure left no trace on the row"


def test_successful_release_marks_released(client, admin_headers):
    from app.db import get_session
    from app.models import QuarantinedDoc

    with get_session() as db:
        row = QuarantinedDoc(source="release_ok.txt", department="general",
                             trust_tier="untrusted", text="withheld but approved",
                             findings="[]", status="held", created_at="2026-01-01T00:00:00")
        db.add(row)
        db.flush()
        item_id = row.id

    r = client.post(f"/api/admin/kb/quarantine/{item_id}/release", headers=admin_headers)
    assert r.status_code == 200, r.text
    with get_session() as db:
        row = db.get(QuarantinedDoc, item_id)
        assert row.status == "released"
        assert row.reviewed_at
        assert row.release_error is None
