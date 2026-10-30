"""The add path's legacy general cleanup takes only the rows the file sync wrote (2026-10-29).

app/ingest_sync.py's _ingest_file_locked ran delete_source(name, "general") on
every ingest of a file routed outside general - the self-healing purge of the
chunks a file left in general before it was department-routed. delete_source
takes every row of the name, and an upload is keyed by its file name and lands
in general by default, so an upload sharing the name of a restricted knowledge
file was deleted each time that file re-ingested: any change to it, or a
forced sync.

What these pin: the cleanup is the rule every other file-sync delete follows
(delete_file_chunks, the watcher's delete's helper) - the file sync's stamped
rows in general go, an upload of the same name there stays. The CONTROL passes
on the code before the change too: legacy rows with no upload of the name are
removed as before.

The store is test_knowledge_orphan_prune's index fixture, real in-memory
chroma and the fake, through test_knowledge_sync_reads_through_extractor's
tree fixture.
"""
import pytest

import app.database as database
import app.ingest_sync as ingest_mod
from tests.test_knowledge_orphan_prune import EMBEDDING, index  # noqa: F401
from tests.test_knowledge_sync_reads_through_extractor import tree  # noqa: F401

SOURCE = "internal/notes.md"      # internal/ routes to restricted by default
TEXT = ("## Returns\n\nReturns are accepted within thirty days of delivery.\n\n"
        "## Refunds\n\nRefunds go to the original card.\n")
CHANGED = TEXT + "\n## Exchanges\n\nExchanges ship within five days.\n"
UPLOADED = "An uploaded policy page."


def _col(tree, department: str | None = None):
    return tree["index"].get_collection(database._collection_name(department))


def _docs_of(tree, source: str, department: str | None = None) -> list[str]:
    return _col(tree, department).get(where={"source": source},
                                      include=["documents"])["documents"]


def _legacy(tree, n: int = 1) -> None:
    """Pre-routing rows the file sync left in general: stamped from_file."""
    _col(tree).add(ids=[f"legacy-{i}" for i in range(n)],
                   documents=[f"The old general copy, part {i}." for i in range(n)],
                   metadatas=[{"source": SOURCE, "chunk": i, "from_file": "true"}
                              for i in range(n)],
                   embeddings=[EMBEDDING] * n)


def _upload(tree) -> None:
    """An upload's row in general under the same name: no from_file stamp."""
    _col(tree).add(ids=["uploaded"], documents=[UPLOADED],
                   metadatas=[{"source": SOURCE, "chunk": 0, "trust": "curated"}],
                   embeddings=[EMBEDDING])


def _synced(tree):
    tree["index"].create_collection(database._collection_name("restricted"))
    k = tree["knowledge"]
    (k / "internal").mkdir()
    f = k / "internal" / "notes.md"
    f.write_text(TEXT, encoding="utf-8")
    assert ingest_mod._sync_knowledge_dir(force=False)[SOURCE]["status"] == "ok"
    assert len(_docs_of(tree, SOURCE, "restricted")) == 2
    return f


def _reingest(entry: str, f) -> None:
    if entry == "the file changes":
        f.write_text(CHANGED, encoding="utf-8")
        ingest_mod._handle_watched_change(False, f)     # the watcher's modify event
    else:
        assert ingest_mod._sync_knowledge_dir(force=True)[SOURCE]["status"] == "ok"


# -- FIX ------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["the file changes", "a forced sync"])
def test_a_reingest_leaves_a_general_upload_of_the_name(entry, tree):
    f = _synced(tree)
    _legacy(tree)
    _upload(tree)
    assert len(_docs_of(tree, SOURCE)) == 2

    _reingest(entry, f)

    assert _docs_of(tree, SOURCE) == [UPLOADED], "the legacy cleanup took the upload"
    want = 3 if entry == "the file changes" else 2
    assert len(_docs_of(tree, SOURCE, "restricted")) == want


# -- CONTROL: passes before the change and after ----------------------------------

@pytest.mark.parametrize("entry", ["the file changes", "a forced sync"])
def test_control_legacy_rows_with_no_upload_are_removed(entry, tree):
    f = _synced(tree)
    _legacy(tree, n=2)
    assert len(_docs_of(tree, SOURCE)) == 2

    _reingest(entry, f)

    assert _docs_of(tree, SOURCE) == []
    want = 3 if entry == "the file changes" else 2
    assert len(_docs_of(tree, SOURCE, "restricted")) == want
