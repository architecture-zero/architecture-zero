"""The docs prune takes only the rows the file sync wrote (2026-10-29).

app/ingest_sync.py's _prune_orphan_docs deleted every 'docs/<name>' source
with no file behind it, through delete_source, on the word of its docstring
that uploads are never prefixed 'docs/'. The upload route keeps the client's
filename as the source name, so an upload named 'docs/x.md' was deleted by the
next docs sync - and a docs/ source holding both the file sync's rows and an
upload's lost the upload with the file.

What these pin: the prune is the rule every other file-sync delete follows
(delete_file_chunks, by the from_file stamp) - the file sync's rows go, an
upload's stay - and its report says what it removed: the rows it took, never
the name's whole count, in the dry run as in the real one. The CONTROL passes
on the code before the change too: a docs file removed from disk has its rows
pruned and reported as before.

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
UPLOADED = "An uploaded policy page."


def _docs_of(tree, source: str) -> list[str]:
    gen = tree["index"].get_collection(database.GLOBAL_COLLECTION)
    return gen.get(where={"source": source}, include=["documents"])["documents"]


def _upload(tree, source: str) -> None:
    """An upload's row: the client's filename as the source, no from_file stamp."""
    tree["index"].get_collection(database.GLOBAL_COLLECTION).add(
        ids=[f"uploaded-{source}"], documents=[UPLOADED],
        metadatas=[{"source": source, "chunk": 0, "trust": "curated"}],
        embeddings=[EMBEDDING])


# -- FIX ------------------------------------------------------------------------

def test_an_upload_named_under_docs_with_no_file_stays(tree):
    _upload(tree, "docs/x.md")

    out = ingest_mod._prune_orphan_docs()
    assert _docs_of(tree, "docs/x.md") == [UPLOADED], "the prune deleted the upload"
    assert out["deleted"] == [], "the prune reported the upload as removed"

    res = ingest_mod._sync_docs(force=True)       # the sync's own call of the prune
    assert _docs_of(tree, "docs/x.md") == [UPLOADED], "the docs sync deleted the upload"
    assert "docs/x.md" not in res


def test_the_dry_run_does_not_offer_an_upload_named_under_docs(tree):
    _upload(tree, "docs/x.md")
    plan = ingest_mod._prune_orphan_docs(dry_run=True)
    assert plan["would_delete"] == [], "the dry run offered the upload for deletion"


def _file_and_upload_of_one_name(tree):
    d = tree["docs"]
    (d / "policy.md").write_text(TEXT, encoding="utf-8")
    assert ingest_mod._sync_docs(force=True)["docs/policy.md"]["status"] == "ok"
    _upload(tree, "docs/policy.md")
    assert len(_docs_of(tree, "docs/policy.md")) == 3
    (d / "policy.md").unlink()


def test_a_removed_docs_file_takes_its_rows_and_leaves_an_upload_of_its_name(tree):
    _file_and_upload_of_one_name(tree)

    res = ingest_mod._sync_docs(force=True)

    assert _docs_of(tree, "docs/policy.md") == [UPLOADED], "the upload went with the file"
    assert res["docs/policy.md"] == {"status": "pruned", "chunks": 2}, \
        "the report counted rows the prune did not remove"


def test_the_dry_run_counts_only_the_file_syncs_rows(tree):
    _file_and_upload_of_one_name(tree)
    plan = ingest_mod._prune_orphan_docs(dry_run=True)
    assert plan["would_delete"] == [{"source": "docs/policy.md",
                                     "department": "general", "count": 2}]
    assert len(_docs_of(tree, "docs/policy.md")) == 3, "the dry run removed rows"


# -- CONTROL: passes before the change and after ----------------------------------

def test_control_a_removed_docs_file_is_pruned_and_reported(tree):
    d = tree["docs"]
    (d / "gone.md").write_text(TEXT, encoding="utf-8")
    (d / "keep.md").write_text("## Keep\n\nStill here.\n", encoding="utf-8")
    ingest_mod._sync_docs(force=True)
    assert len(_docs_of(tree, "docs/gone.md")) == 2
    (d / "gone.md").unlink()

    plan = ingest_mod._prune_orphan_docs(dry_run=True)
    assert plan["would_delete"] == [{"source": "docs/gone.md",
                                     "department": "general", "count": 2}]
    assert len(_docs_of(tree, "docs/gone.md")) == 2, "the dry run removed rows"
    out = ingest_mod._prune_orphan_docs()

    assert _docs_of(tree, "docs/gone.md") == []
    assert len(_docs_of(tree, "docs/keep.md")) == 1
    assert out["deleted"] == [{"source": "docs/gone.md", "department": "general", "count": 2}]
