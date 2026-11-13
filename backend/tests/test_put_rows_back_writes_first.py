"""database.put_rows_back writes first and deletes only where it must.

The quarantine release's rollback puts back the rows it took. A put-back
that deleted every row of a slice and then wrote them back lost up to a
slice of rows when the write failed after the delete, and left the
release's text where the earlier row belonged when the delete failed. One
upsert of every row comes first (no row absent; the release's text out of
each one), and only a row that held a key the restored row does not is then
deleted and written again.

The FIX case is the refused first write: a row holding the release's
text is deleted before the error goes on (held means not searchable comes
first); the put-back's first version left it there. The CONTROL cases pass
on both versions; one pins what is left, which the docstring explains: a
wider row is absent between its delete and its second write, so a write
that fails there loses it. Run on real in-memory chroma and the fake store
(the index fixture).
"""
import pytest

import app.database as database
from tests.test_kept_id_own_text import store  # noqa: F401
from tests.test_knowledge_orphan_prune import index  # noqa: F401


@pytest.fixture
def col(store):
    return store.get_collection(database.GLOBAL_COLLECTION)


SOURCE = "policy.md"


class _Failing:
    """The collection, with the n-th upsert or delete raising before it
    reaches the col, and every delete's ids recorded."""

    def __init__(self, col, plan):
        self._col, self._plan = col, plan

    def __getattr__(self, name):
        return getattr(self._col, name)

    def _call(self, name, kw):
        after_delete = bool(self._plan["calls"]) and self._plan["calls"][-1][0] == "delete"
        self._plan["calls"].append((name, sorted(kw.get("ids") or [])))
        n = sum(1 for c, _ in self._plan["calls"] if c == name)
        if self._plan.get(name) == n or (
                name == "upsert" and after_delete and self._plan.get("upsert_after_delete")):
            raise RuntimeError(f"the store refused the {name}")

    def upsert(self, *a, **kw):
        self._call("upsert", kw)
        out = self._col.upsert(*a, **kw)
        n = sum(1 for c, _ in self._plan["calls"] if c == "upsert")
        if self._plan.get("upsert_lands_then_raises") == n:
            raise RuntimeError("the store answered an error after the write")
        return out

    def delete(self, *a, **kw):
        self._call("delete", kw)
        return self._col.delete(*a, **kw)


def _failing(monkeypatch, **plan) -> dict:
    plan["calls"] = []
    for seam in ("_get_collection", "_existing_collection"):
        real = getattr(database, seam)
        monkeypatch.setattr(database, seam,
                            lambda department=None, _real=real: _Failing(_real(department), plan))
    return plan


def _stored(col, ids, texts, metas) -> None:
    vector = database._embed("a")
    col.upsert(ids=ids, documents=texts, metadatas=metas, embeddings=[vector] * len(ids))


def _now(col, ids) -> dict:
    got = col.get(ids=ids, include=["documents", "metadatas"])
    return {i: (d, dict(m)) for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])}


def _earlier(n: int) -> dict:
    """The rows as read before the release: text and metadata, no vector."""
    return {f"r{i}": (f"The earlier text of piece {i}.",
                      {"source": SOURCE, "chunk": i, "trust": "curated", "from_watch": "false"},
                      None)
            for i in range(n)}


def _release_over(col, n: int, extra: dict | None = None) -> None:
    """The release's pieces standing where the earlier rows were: its text,
    a key set the earlier rows cover (or wider by `extra`)."""
    _stored(col, [f"r{i}" for i in range(n)],
            [f"Held text the release wrote, piece {i}." for i in range(n)],
            [{"source": SOURCE, "chunk": i, "trust": "untrusted", **(extra or {})}
             for i in range(n)])


def _as_read(rows) -> dict:
    return {i: (t, m) for i, (t, m, _) in rows.items()}


def test_control_rows_whose_keys_the_earlier_rows_cover_are_never_deleted(col, monkeypatch):
    _release_over(col, 3)
    rows = _earlier(3)
    plan = _failing(monkeypatch)
    assert database.put_rows_back(rows) == 3
    assert [c for c in plan["calls"] if c[0] == "delete"] == [], (
        "a row was deleted before it was written back (absent in between)")
    assert _now(col, list(rows)) == _as_read(rows)


def test_control_a_delete_that_fails_leaves_no_held_text(col, monkeypatch):
    """Every delete raises. The rows whose keys the earlier rows cover are
    back whole; the delete was the first step before, so its failure left
    every row holding the release's text."""
    _release_over(col, 3)
    rows = _earlier(3)
    _failing(monkeypatch, delete=1)
    database.put_rows_back(rows)
    assert _now(col, list(rows)) == _as_read(rows)


def test_control_a_wider_row_whose_delete_fails_holds_the_earlier_text(col, monkeypatch):
    """A row holding a key the earlier row does not needs its delete; when
    that delete fails, the earlier text is already back (a stray key is
    left), where it held the release's text before."""
    _release_over(col, 2, extra={"injection_types": "instruction_override"})
    rows = _earlier(2)
    _failing(monkeypatch, delete=1)
    with pytest.raises(RuntimeError):
        database.put_rows_back(rows)
    now = _now(col, list(rows))
    assert {i: t for i, (t, _) in now.items()} == {i: t for i, (t, _, _) in rows.items()}, (
        "the release's text stayed where the earlier row belonged")


def test_fix_a_first_write_that_lands_and_then_raises_deletes_nothing_it_put_back(
        col, monkeypatch):
    """The store writes the first upsert and then raises. The rows are
    back by then; the delete that follows a refused write reads again, or it
    deletes the rows it has just put back."""
    _release_over(col, 4)
    rows = _earlier(4)
    _failing(monkeypatch, upsert_lands_then_raises=1)
    with pytest.raises(RuntimeError):
        database.put_rows_back(rows)
    assert _now(col, list(rows)) == _as_read(rows), (
        "the put-back deleted the rows it had just put back")


def test_control_a_wider_row_ends_holding_exactly_the_earlier_row(col, monkeypatch):
    _release_over(col, 2, extra={"injection_types": "instruction_override"})
    rows = _earlier(2)
    assert database.put_rows_back(rows) == 2
    assert _now(col, list(rows)) == _as_read(rows)


def test_control_rows_that_are_gone_are_written_back(col, monkeypatch):
    rows = _earlier(3)
    assert database.put_rows_back(rows) == 3
    assert _now(col, list(rows)) == _as_read(rows)


def test_fix_a_refused_first_write_takes_the_held_text_out(col, monkeypatch):
    """What is left, (2): the store refuses the write. The rows holding the
    release's text are deleted before the error goes on; their earlier
    versions are lost, as they were before."""
    _release_over(col, 3)
    rows = _earlier(3)
    _failing(monkeypatch, upsert=1)
    with pytest.raises(RuntimeError):
        database.put_rows_back(rows)
    assert _now(col, list(rows)) == {}, "the release's text stayed searchable"


def test_control_a_wider_row_whose_second_write_fails_is_lost_and_holds_no_held_text(
        col, monkeypatch):
    """What is left, (1): between a wider row's delete and its second write
    the row is absent; a write that fails there loses it. (The write that
    follows a delete fails: before, that was the slice's only write.)"""
    _release_over(col, 2, extra={"injection_types": "instruction_override"})
    rows = _earlier(2)
    _failing(monkeypatch, upsert_after_delete=True)
    with pytest.raises(RuntimeError):
        database.put_rows_back(rows)
    assert all("Held text" not in t for t, _ in _now(col, list(rows)).values())
