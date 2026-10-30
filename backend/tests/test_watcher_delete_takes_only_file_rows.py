"""A removed knowledge file takes only the rows the file sync wrote (2026-10-29).

app/ingest_sync.py's watcher handled a removed file by deleting its WHOLE
source - delete_source in the file's department, and again in general for the
legacy pre-routing chunks - so an upload of the same name went with it. An
upload is keyed by its file name, as a top-level knowledge file is, and lands
in general by default under the name its client sent. Every other delete
path already decides by ownership: the knowledge orphan prune, the legacy-key
purge and the textless-file removal take only rows stamped from_file="true".

What these pin: the watcher's removal is that same rule (delete_file_chunks,
the textless path's helper), in the file's department and in general. The
file's chunks go, its fingerprint goes, the upload stays. The CONTROL passes
on the code before the change too: a file with no upload of its name is
removed whole, as before.

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


def _col(tree, department: str | None = None):
    return tree["index"].get_collection(database._collection_name(department))


def _docs_of(tree, source: str, department: str | None = None) -> list[str]:
    return _col(tree, department).get(where={"source": source},
                                      include=["documents"])["documents"]


def _upload(tree, source: str, doc_id: str = "uploaded") -> None:
    """An upload's row: keyed by the file name, no from_file stamp."""
    _col(tree).add(ids=[doc_id], documents=[UPLOADED],
                   metadatas=[{"source": source, "chunk": 0, "trust": "curated"}],
                   embeddings=[EMBEDDING])


# -- FIX ------------------------------------------------------------------------

def test_a_removed_knowledge_file_leaves_an_upload_of_its_name(tree):
    k = tree["knowledge"]
    (k / "policy.md").write_text(TEXT, encoding="utf-8")
    assert ingest_mod._sync_knowledge_dir(force=False)["policy.md"]["status"] == "ok"
    _upload(tree, "policy.md")
    assert len(_docs_of(tree, "policy.md")) == 3
    assert "policy.md" in tree["state"]

    (k / "policy.md").unlink()
    ingest_mod._handle_watched_change(True, k / "policy.md")

    assert _docs_of(tree, "policy.md") == [UPLOADED], "the upload went with the file"
    assert "policy.md" not in tree["state"]


def test_the_legacy_general_cleanup_leaves_an_upload_of_the_name(tree):
    """A file routed out of general (internal/ is restricted by default) had
    its pre-routing chunks cleaned from general by name too. An upload can sit
    in general under that name - general is the upload's default department -
    so the cleanup takes only the file sync's stamped rows there as well."""
    tree["index"].create_collection(database._collection_name("restricted"))
    k = tree["knowledge"]
    (k / "internal").mkdir()
    (k / "internal" / "notes.md").write_text(TEXT, encoding="utf-8")
    source = "internal/notes.md"
    assert ingest_mod._sync_knowledge_dir(force=False)[source]["status"] == "ok"
    assert len(_docs_of(tree, source, "restricted")) == 2
    # A pre-routing chunk the file sync left in general, and an upload there.
    _col(tree).add(ids=["legacy"], documents=["The old general copy."],
                   metadatas=[{"source": source, "chunk": 0, "from_file": "true"}],
                   embeddings=[EMBEDDING])
    _upload(tree, source)

    (k / "internal" / "notes.md").unlink()
    ingest_mod._handle_watched_change(True, k / "internal" / "notes.md")

    assert _docs_of(tree, source, "restricted") == []
    assert _docs_of(tree, source) == [UPLOADED], "the legacy cleanup took the upload"
    assert source not in tree["state"]


# -- CONTROL: passes before the change and after ----------------------------------

def test_control_a_removed_file_with_no_upload_of_its_name_goes_whole(tree, monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(ingest_mod, "log", lambda event, **kw: seen.append((event, kw)))
    k = tree["knowledge"]
    (k / "policy.md").write_text(TEXT, encoding="utf-8")
    (k / "shipping.md").write_text("## Shipping\n\nTwo to five days.\n", encoding="utf-8")
    ingest_mod._sync_knowledge_dir(force=False)
    assert len(_docs_of(tree, "policy.md")) == 2

    (k / "policy.md").unlink()
    ingest_mod._handle_watched_change(True, k / "policy.md")

    assert _docs_of(tree, "policy.md") == []
    assert len(_docs_of(tree, "shipping.md")) == 1
    assert set(tree["state"]) == {"shipping.md"}
    assert ("kb_file_deleted", {"file": "policy.md"}) in seen
