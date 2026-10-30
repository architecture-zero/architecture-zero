"""The knowledge and docs syncs report every department's rows a prune removed
(2026-10-29).

Both syncs key their result by source and wrote one entry per row of the
prune's `deleted`, which lists what went per (source, department). A source
pruned in two departments - its own and a stamped row it left in general
before it was department-routed - kept only the last department's count, so
POST /api/kb/sync answered fewer chunks than went. The entry is now one per
source with its chunks summed over its departments, the same shape
({"status": "pruned", "chunks": n}): the boot's _report_sync counts one
pruned source per key, as before.

The store is test_knowledge_orphan_prune's index fixture, real in-memory
chroma and the fake, through test_knowledge_sync_reads_through_extractor's
tree fixture. The source's department is set here, not read from the
deployment's routing settings.
"""
import pytest

import app.database as database
import app.ingest_sync as ingest_mod
from tests.test_knowledge_orphan_prune import EMBEDDING, index  # noqa: F401
from tests.test_knowledge_sync_reads_through_extractor import tree  # noqa: F401

TEXT = ("## Returns\n\nReturns are accepted within thirty days of delivery.\n\n"
        "## Refunds\n\nRefunds go to the original card.\n")
ROUTED = {"internal/runbook.md", "docs/notes.md"}


@pytest.fixture
def routed(tree, monkeypatch):
    monkeypatch.setattr(ingest_mod, "_dept_for_source",
                        lambda source: "restricted" if source in ROUTED else "general")
    # Created up front, as the tree fixture creates general: the in-memory
    # store refuses the production hnsw metadata on a new collection.
    tree["index"].create_collection(database._collection_name("restricted"))
    # The knowledge prune refuses a tree with no eligible file: one stays.
    (tree["knowledge"] / "faq.md").write_text("## Hours\n\nOpen nine to five.\n",
                                              encoding="utf-8")
    return tree


def _held(tree, source: str, dept: str) -> list:
    name = database._collection_name(None if dept == "general" else dept)
    col = tree["index"].get_collection(name)
    return col.get(where={"source": source})["ids"]


def _indexed_then_removed(tree, entry: str, legacy: bool) -> tuple[str, int, callable]:
    """The source indexed in restricted, optionally a stamped row of it in
    general (written before routing), then its file removed with no watcher
    running."""
    if entry == "knowledge sync":
        (tree["knowledge"] / "internal").mkdir()
        path, source = tree["knowledge"] / "internal" / "runbook.md", "internal/runbook.md"
        sync = ingest_mod._sync_knowledge_dir
    else:
        path, source = tree["docs"] / "notes.md", "docs/notes.md"
        sync = ingest_mod._sync_docs
    path.write_text(TEXT, encoding="utf-8")
    res = sync(force=True)
    assert res[source]["status"] == "ok", res
    filed = len(_held(tree, source, "restricted"))
    assert filed == 2
    if legacy:
        database.add_documents_batch(
            [("legacy-general-1", "A chunk the file left in general before routing.",
              {"source": source, "chunk": 0, "from_file": "true"})],
            department="general")
        assert _held(tree, source, "general") == ["legacy-general-1"]
    path.unlink()
    return source, filed, sync


@pytest.mark.parametrize("entry", ["knowledge sync", "docs sync"])
def test_fix_a_source_pruned_in_two_departments_reports_both_counts(entry, routed):
    source, filed, sync = _indexed_then_removed(routed, entry, legacy=True)
    res = sync(force=True)
    for dept in ("general", "restricted"):
        assert _held(routed, source, dept) == [], (dept, res)
    assert res[source] == {"status": "pruned", "chunks": filed + 1}, res


@pytest.mark.parametrize("entry", ["knowledge sync", "docs sync"])
def test_control_a_source_pruned_in_one_department_reports_as_before(entry, routed):
    source, filed, sync = _indexed_then_removed(routed, entry, legacy=False)
    res = sync(force=True)
    assert _held(routed, source, "restricted") == []
    assert res[source] == {"status": "pruned", "chunks": filed}, res
