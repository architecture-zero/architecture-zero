"""The knowledge sync's legacy-key purge deletes only what a file sync wrote.

A knowledge subdir file was once keyed by its bare name, so the sync purges
that key's leftovers - and an upload is keyed by its bare file name too.
Deleting by name alone took any upload that shared its name with a knowledge
subdir file, on every sync, boot included.

Against a real in-memory chroma, because the point is the metadata filter
chroma actually applies, which a mock would only echo back. The client wiring
follows test_real_chroma.py: conftest patches the chromadb namespace, so the
real Settings comes from chromadb.config.
"""
import chromadb
import pytest
from chromadb.config import Settings as _RealSettings

import app.database as database
from app import ingest_sync

EMBEDDING = [1.0, 0.0, 0.0, 0.0]


@pytest.fixture
def general(monkeypatch):
    """The general department's collection - real, empty, and the one
    database.py's seams hand out."""
    client = chromadb.EphemeralClient(
        settings=_RealSettings(is_persistent=False, anonymized_telemetry=False))
    # The in-memory System is shared per settings, so a collection left by
    # another test would still be here: start from a fresh one.
    try:
        client.delete_collection(database.GLOBAL_COLLECTION)
    except Exception:
        pass
    col = client.create_collection(name=database.GLOBAL_COLLECTION,
                                   metadata={"hnsw:space": "cosine"})
    monkeypatch.setattr(database, "client", client)
    monkeypatch.setattr(database, "_get_collection", lambda department=None: col)
    yield col
    client.delete_collection(database.GLOBAL_COLLECTION)


def test_an_upload_named_like_a_subdir_file_survives_the_sync(monkeypatch, tmp_path, general):
    (tmp_path / "support").mkdir()
    (tmp_path / "support" / "faq.md").write_text("## Returns\n\nThirty days.\n",
                                                 encoding="utf-8")
    # Both under the bare name in general: an upload of faq.md, and a chunk
    # the knowledge sync wrote when support/faq.md was still keyed that way.
    general.add(ids=["upload", "legacy"],
                documents=["An uploaded faq.", "The faq, as the old key stored it."],
                metadatas=[{"source": "faq.md", "chunk": 0, "trust": "curated"},
                           {"source": "faq.md", "chunk": 0, "from_file": "true"}],
                embeddings=[EMBEDDING, EMBEDDING])
    monkeypatch.setattr(ingest_sync, "KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr(ingest_sync, "_ingest_file", lambda name, text: 1)
    monkeypatch.setattr(ingest_sync, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_sync, "_save_ingest_state", lambda state: None)
    ingest_sync._sync_knowledge_dir(force=True)
    left = general.get(where={"source": "faq.md"})["ids"]
    assert "upload" in left, "the sync deleted an upload that shares a knowledge file's name"
    assert "legacy" not in left, "the file sync's leftover under the old key survived"
