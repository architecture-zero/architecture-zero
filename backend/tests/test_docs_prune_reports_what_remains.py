"""The docs prune reports as remaining only what is still indexed after it ran (2026-10-29).

app/ingest_sync.py's _prune_orphan_docs answers with what it removed and the
docs/ sources still in the index (the prune-orphans endpoint passes both on as
the observable record of the purge). The remaining list was built from the
listing taken BEFORE the deletes, so every source the prune had just removed
was reported as still indexed beside the report of its removal.

What these pin: after a real prune the remaining list is what is left - a
source it emptied is absent, and a source that keeps another writer's rows is
listed with the rows left. The CONTROL passes on the code before the change
too: a dry run removes nothing, so it lists everything still indexed, as
before.

The store is test_knowledge_orphan_prune's index fixture, real in-memory
chroma and the fake, through test_knowledge_sync_reads_through_extractor's
tree fixture.
"""
import app.database as database
import app.ingest_sync as ingest_mod
from tests.test_knowledge_orphan_prune import EMBEDDING, index  # noqa: F401
from tests.test_knowledge_sync_reads_through_extractor import tree  # noqa: F401

TEXT = ("## Returns\n\nReturns are accepted within thirty days of delivery.\n\n"
        "## Refunds\n\nRefunds go to the original card.\n")


def _two_docs_one_removed(tree):
    d = tree["docs"]
    (d / "gone.md").write_text(TEXT, encoding="utf-8")
    (d / "keep.md").write_text("## Keep\n\nStill here.\n", encoding="utf-8")
    ingest_mod._sync_docs(force=True)
    (d / "gone.md").unlink()


# -- FIX ------------------------------------------------------------------------

def test_a_pruned_docs_source_is_absent_from_the_remaining_list(tree):
    _two_docs_one_removed(tree)
    out = ingest_mod._prune_orphan_docs()
    assert out["deleted"] == [{"source": "docs/gone.md", "department": "general", "count": 2}]
    assert out["docs_sources_in_index"] == [
        {"source": "docs/keep.md", "department": "general", "count": 1}], \
        "a source the prune just removed was reported as still indexed"


def test_a_source_keeping_an_upload_is_listed_with_the_rows_left(tree):
    _two_docs_one_removed(tree)
    tree["index"].get_collection(database.GLOBAL_COLLECTION).add(
        ids=["uploaded"], documents=["An uploaded policy page."],
        metadatas=[{"source": "docs/gone.md", "chunk": 0, "trust": "curated"}],
        embeddings=[EMBEDDING])
    out = ingest_mod._prune_orphan_docs()
    assert out["docs_sources_in_index"] == [
        {"source": "docs/gone.md", "department": "general", "count": 1},
        {"source": "docs/keep.md", "department": "general", "count": 1}]


# -- CONTROL: passes before the change and after ----------------------------------

def test_control_a_dry_run_lists_everything_still_indexed(tree):
    _two_docs_one_removed(tree)
    plan = ingest_mod._prune_orphan_docs(dry_run=True)
    assert plan["docs_sources_in_index"] == [
        {"source": "docs/gone.md", "department": "general", "count": 2},
        {"source": "docs/keep.md", "department": "general", "count": 1}]
