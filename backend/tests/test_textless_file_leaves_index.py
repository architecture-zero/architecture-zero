"""A watched file that now yields no text has its chunks removed (2026-10-29).

app/ingest_sync.py skips a watched file that yields no text - an emptied .md,
a PDF that no longer parses or holds no text - so the chunks an earlier sync
wrote for it (the old code's decoded PDF bytes; on any code, an emptied
file's old text) stayed indexed and served, with nothing left to remove them:
the orphan prune removes a file that is GONE, and this one is still there.

What these pin: at the next sync, the knowledge sync and the docs sync remove
the chunks the file sync wrote for such a file - the rows stamped from_file,
the stamp the orphan prune trusts - log it once, and index a later version
with text as usual. The CONTROL cases pass on the code before the change too:
an upload of the same name keeps its chunks, a file with text is untouched,
and a docs/ file whose name an empty one shares keeps its source.

The store is test_knowledge_orphan_prune's index fixture, real in-memory
chroma and the fake, through test_knowledge_sync_reads_through_extractor's
tree fixture.
"""
import pytest

import app.database as database
import app.ingest_sync as ingest_mod
from tests.test_knowledge_orphan_prune import EMBEDDING, index  # noqa: F401
from tests.test_knowledge_sync_reads_through_extractor import WORDS, _pdf, tree  # noqa: F401

TEXT = ("## Returns\n\nReturns are accepted within thirty days of delivery.\n\n"
        "## Refunds\n\nRefunds go to the original card.\n")


def _gen(tree):
    return tree["index"].get_collection(database.GLOBAL_COLLECTION)


def _docs_of(tree, source: str) -> list[str]:
    return _gen(tree).get(where={"source": source}, include=["documents"])["documents"]


def _events(monkeypatch) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(ingest_mod, "log", lambda event, **kw: seen.append((event, kw)))
    return seen


def _removals(events) -> list[dict]:
    return [kw for event, kw in events if event == "kb_file_no_text_removed"]


# -- FIX ------------------------------------------------------------------------

def test_an_emptied_knowledge_file_leaves_the_index_at_the_next_sync(tree, monkeypatch):
    k = tree["knowledge"]
    (k / "policy.md").write_text(TEXT, encoding="utf-8")
    assert ingest_mod._sync_knowledge_dir(force=False)["policy.md"]["status"] == "ok"
    assert len(_docs_of(tree, "policy.md")) == 2

    events = _events(monkeypatch)
    (k / "policy.md").write_text("\n\n", encoding="utf-8")
    res = ingest_mod._sync_knowledge_dir(force=False)
    assert res.get("policy.md") == {"status": "pruned", "chunks": 2}, res
    assert _docs_of(tree, "policy.md") == []
    assert [r["file"] for r in _removals(events)] == ["policy.md"], events

    # Logged once: the next sync finds nothing of the file's left to remove.
    again = ingest_mod._sync_knowledge_dir(force=False)
    assert "policy.md" not in again, again
    assert len(_removals(events)) == 1, events

    # A later version with text is indexed as usual.
    (k / "policy.md").write_text(TEXT, encoding="utf-8")
    assert ingest_mod._sync_knowledge_dir(force=False)["policy.md"]["status"] == "ok"
    assert len(_docs_of(tree, "policy.md")) == 2


@pytest.mark.parametrize("now", ["broken", "textless"])
def test_a_pdf_indexed_then_unreadable_leaves_the_index_at_the_next_sync(now, tree, monkeypatch):
    k = tree["knowledge"]
    (k / "refunds.pdf").write_bytes(_pdf(WORDS))
    assert ingest_mod._sync_knowledge_dir(force=False)["refunds.pdf"]["status"] == "ok"
    assert any(WORDS in d for d in _docs_of(tree, "refunds.pdf"))

    (k / "refunds.pdf").write_bytes(b"%PDF-1.4\nnot a pdf at all\n" if now == "broken"
                                    else _pdf(None))
    res = ingest_mod._sync_knowledge_dir(force=False)
    assert res.get("refunds.pdf", {}).get("status") == "pruned", res
    assert _docs_of(tree, "refunds.pdf") == []


def test_an_emptied_docs_file_leaves_the_index_at_the_next_sync(tree):
    d = tree["docs"]
    (d / "guide.md").write_text(TEXT, encoding="utf-8")
    assert ingest_mod._sync_docs(force=False)["docs/guide.md"]["status"] == "ok"
    assert _docs_of(tree, "docs/guide.md")

    (d / "guide.md").write_text("", encoding="utf-8")
    res = ingest_mod._sync_docs(force=False)
    assert res.get("docs/guide.md", {}).get("status") == "pruned", res
    assert _docs_of(tree, "docs/guide.md") == []


# -- CONTROL: pass before the change and after ------------------------------------

def test_control_an_upload_of_the_same_name_keeps_its_chunks(tree):
    """An upload is keyed by its file name, as a top-level knowledge file is.
    It carries no from_file stamp, so the file going textless is no reason
    to remove it."""
    k = tree["knowledge"]
    (k / "policy.md").write_text(TEXT, encoding="utf-8")
    assert ingest_mod._sync_knowledge_dir(force=False)["policy.md"]["status"] == "ok"
    _gen(tree).add(ids=["uploaded"], documents=["An uploaded policy page."],
                   metadatas=[{"source": "policy.md", "chunk": 0, "trust": "untrusted"}],
                   embeddings=[EMBEDDING])
    (k / "policy.md").write_text("", encoding="utf-8")
    ingest_mod._sync_knowledge_dir(force=False)
    assert "An uploaded policy page." in _docs_of(tree, "policy.md")


def test_control_a_file_with_text_is_untouched(tree):
    k = tree["knowledge"]
    (k / "policy.md").write_text(TEXT, encoding="utf-8")
    (k / "blank.md").write_text("", encoding="utf-8")
    ingest_mod._sync_knowledge_dir(force=False)
    before = sorted(_docs_of(tree, "policy.md"))
    res = ingest_mod._sync_knowledge_dir(force=True)
    assert res["policy.md"]["status"] == "ok" and "blank.md" not in res, res
    assert sorted(_docs_of(tree, "policy.md")) == before and len(before) == 2


def test_control_a_docs_name_an_empty_file_shares_keeps_its_source(tree):
    """docs/ sources are keyed by file name: an empty guide.md in one folder
    and a guide.md with text in another are one source, and it stays."""
    d = tree["docs"]
    (d / "a").mkdir()
    (d / "b").mkdir()
    (d / "a" / "guide.md").write_text(TEXT, encoding="utf-8")
    (d / "b" / "guide.md").write_text("", encoding="utf-8")
    ingest_mod._sync_docs(force=False)
    ingest_mod._sync_docs(force=True)
    assert len(_docs_of(tree, "docs/guide.md")) == 2
