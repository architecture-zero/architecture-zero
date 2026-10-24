"""The backup flushes the vector index before it copies it.

run_backup snapshots every SQLite file through the backup API - the vector
store's chroma.sqlite3 included - but the HNSW segment files are plain copies,
and chroma persists them only when a batch crosses its sync threshold. An
archive taken without a flush could pair an up-to-date chroma.sqlite3 with
vector files missing their tail, which nothing reports until a restore.
"""
import logging
import sys
import tarfile

import pytest

from app import database
from app.routers import admin

_OWNER = {"id": 1, "username": "owner", "role": "owner"}


class _Lines(logging.Handler):
    """The app logger's lines, as (level, event, fields)."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelname, record.getMessage(),
                           dict(getattr(record, "data", {}))))


@pytest.fixture
def lines():
    from app.logger import get_logger
    handler = _Lines()
    get_logger().addHandler(handler)
    yield handler.lines
    get_logger().removeHandler(handler)


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    """A data directory holding an HNSW segment directory beside a file."""
    data = tmp_path / "data"
    segment = data / "segment-0001"
    segment.mkdir(parents=True)
    (segment / "data_level0.bin").write_bytes(b"vectors")
    (data / "notes.txt").write_text("kept", encoding="utf-8")
    monkeypatch.setattr(admin, "_DATA_DIR", str(data))
    monkeypatch.setattr(admin, "_BACKUP_DIR", str(data / "backups"))
    return data


def _archived(data):
    (archive,) = list((data / "backups").glob("*.tar.gz"))
    with tarfile.open(archive) as t:
        return t.getnames()


def test_the_backup_flushes_before_it_copies(monkeypatch, data_dir, lines):
    order = []
    monkeypatch.setattr(database, "flush_vector_segments",
                        lambda: order.append("flush") or {"flushed": 1, "clean": 3, "errors": 0})
    real_copytree = admin.shutil.copytree
    monkeypatch.setattr(admin.shutil, "copytree",
                        lambda src, dst, *a, **kw: order.append("copy") or real_copytree(src, dst, *a, **kw))
    assert admin.run_backup(current_user=_OWNER)["status"] == "completed"
    assert order == ["flush", "copy"]
    assert ("INFO", "backup_chroma_flush", {"flushed": 1, "clean": 3, "errors": 0}) in lines
    assert any(n.endswith("data_level0.bin") for n in _archived(data_dir))   # the copy still ran


def test_a_flush_that_raises_does_not_fail_the_backup(monkeypatch, data_dir, lines):
    """An unflushed archive still carries every SQLite file correctly, which is
    most of its value - so the flush's failure is logged, never raised."""
    def flush():
        raise RuntimeError("segment manager unreachable")

    monkeypatch.setattr(database, "flush_vector_segments", flush)
    assert admin.run_backup(current_user=_OWNER)["status"] == "completed"
    assert ("ERROR", "backup_chroma_flush_failed", {"error": "segment manager unreachable"}) in lines
    assert any(n.endswith("data_level0.bin") for n in _archived(data_dir))


def test_a_process_without_the_vector_store_skips_the_flush(monkeypatch, data_dir, lines):
    """scripts/backup_cron.py runs run_backup in a process of its own, which
    never opened the vector store. Importing app.database there would open a
    second client on a live store to flush nothing - so the run says it
    skipped, and the store stays closed."""
    import app
    monkeypatch.delitem(sys.modules, "app.database")
    monkeypatch.setattr(app, "database", database)   # put back as it was, whatever an import rebinds
    assert admin.run_backup(current_user=_OWNER)["status"] == "completed"
    assert "app.database" not in sys.modules, "the backup opened the vector store to flush it"
    assert ("INFO", "backup_chroma_flush", {"skipped": "vector store not open in this process"}) in lines
