"""A copy before the first one-way change (2026-10-02).

The first boot after an update encrypts plaintext second-factor seeds and
saved provider keys in place, and an older version cannot read either
afterwards - the way back was a copy the operator had to remember to take.
Now the boot takes it, and holds the conversion when it cannot.
"""
import inspect
import sqlite3

import pyotp
import pytest

from app import db as dbmod

_USER = {"username": "pre_update_probe", "password": "PreUpdate#2026x"}


@pytest.fixture
def plaintext_seed(client, admin_headers):
    """A member whose seed sits in the column in the clear, as a pre-encryption
    row does - written raw, past the encrypting write path."""
    from app.models import User
    from app.users import get_user_by_username
    existing = get_user_by_username(_USER["username"])
    if not existing:
        for role in ("user", "member"):
            r = client.post("/api/users", headers=admin_headers,
                            json={"current_password": "AdminPass1", **_USER, "role": role})
            if r.status_code in (200, 201):
                break
        assert r.status_code in (200, 201), r.text
        existing = get_user_by_username(_USER["username"])
    seed = pyotp.random_base32()
    with dbmod.get_session() as s:
        s.query(User).filter(User.id == existing["id"]).update(
            {"mfa_secret": seed, "mfa_enabled": True})
    yield {"id": existing["id"], "seed": seed}
    with dbmod.get_session() as s:
        s.query(User).filter(User.id == existing["id"]).update(
            {"mfa_secret": None, "mfa_enabled": False})


@pytest.fixture
def fresh_boot(monkeypatch, tmp_path):
    """Each test decides the boot's question afresh, with its copies in tmp."""
    monkeypatch.setattr(dbmod, "_one_way_allowed", None)
    monkeypatch.setattr(dbmod, "PRE_UPDATE_COPY_DIR", str(tmp_path))
    return tmp_path


def _raw_seed(user_id, path=None):
    if path is None:
        from app.models import User
        with dbmod.get_session() as s:
            return s.query(User.mfa_secret).filter(User.id == user_id).scalar()
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT mfa_secret FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    finally:
        con.close()


def test_the_boot_copies_the_database_before_it_converts(plaintext_seed, fresh_boot):
    from app.crypto_at_rest import is_encrypted
    assert dbmod._pending_one_way()["mfa_seeds"] >= 1
    dbmod.init_db()
    copies = list(fresh_boot.iterdir())
    assert len(copies) == 1, copies
    # The copy holds the row as it was - the way back - and the live row moved.
    assert _raw_seed(plaintext_seed["id"], str(copies[0])) == plaintext_seed["seed"]
    assert is_encrypted(_raw_seed(plaintext_seed["id"]))


def test_no_copy_no_conversion(plaintext_seed, fresh_boot, monkeypatch):
    """The copy cannot be written: the conversion is held, the row stays
    plaintext - readable by the old version and the new one alike."""
    monkeypatch.setattr(dbmod, "take_pre_update_copy", lambda: None)
    dbmod.init_db()
    assert dbmod.one_way_changes_allowed() is False
    assert _raw_seed(plaintext_seed["id"]) == plaintext_seed["seed"]


@pytest.fixture
def nothing_to_convert(client):
    """A database with nothing left to convert. The suite's database is
    shared, and other tests write stand-ins for pre-encryption rows raw (a
    provider key through set_config, say): those are set aside for this test
    and put back after, so the question is asked of a database that has none."""
    from app.config import get_all_config, is_secret_config_key, set_config
    from app.crypto_at_rest import is_encrypted
    from app.models import User
    keys = {k: v for k, v in get_all_config().items()
            if is_secret_config_key(k) and v and not is_encrypted(v)}
    for k in keys:
        set_config(k, "")
    with dbmod.get_session() as s:
        seeds = {uid: seed for uid, seed in s.query(User.id, User.mfa_secret)
                 .filter(User.mfa_secret.isnot(None)).all() if not is_encrypted(seed)}
        for uid in seeds:
            s.query(User).filter(User.id == uid).update({"mfa_secret": None})
    yield
    for k, v in keys.items():
        set_config(k, v)
    with dbmod.get_session() as s:
        for uid, seed in seeds.items():
            s.query(User).filter(User.id == uid).update({"mfa_secret": seed})


def test_nothing_to_convert_takes_no_copy(nothing_to_convert, fresh_boot):
    assert not any(dbmod._pending_one_way().values())
    dbmod.init_db()
    assert dbmod.one_way_changes_allowed() is True
    assert list(fresh_boot.iterdir()) == []


def test_only_the_newest_copies_are_kept(plaintext_seed, fresh_boot, monkeypatch):
    import time as _time
    monkeypatch.setattr(dbmod, "PRE_UPDATE_COPIES_KEPT", 2)
    stamps = iter(["20260101T000001Z", "20260101T000002Z", "20260101T000003Z"])
    monkeypatch.setattr(_time, "strftime", lambda fmt, t=None: next(stamps))
    for _ in range(3):
        assert dbmod.take_pre_update_copy() is not None
    names = sorted(p.name for p in fresh_boot.iterdir())
    assert len(names) == 2 and names[-1].endswith("20260101T000003Z"), names


def test_the_provider_key_sweep_asks_the_same_question():
    """The def is not the guard: the second sweep's call is gated too."""
    from app import main
    src = inspect.getsource(main)
    assert "_moved = _sweep_secrets() if _one_way_ok() else 0" in src
    assert "_encrypt_mfa_seeds_sweep() if one_way_changes_allowed() else 0" in inspect.getsource(dbmod.init_db)


# -- The copy does not stay and does not travel (2026-10-02) ------------------
# It holds the seeds and provider keys as the older version stored them, in
# plaintext. Kept with no end and carried by every later backup, it undid the
# encryption at rest for exactly the leaked-archive case that encryption is for
# (the wrap's drift sweep found it the day the copy shipped).

def _stamp(days_ago: float) -> str:
    import time
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - days_ago * 86400))


def test_a_copy_past_the_keep_window_is_deleted_at_boot(fresh_boot, capsys):
    old = fresh_boot / f"history.db.{_stamp(dbmod.PRE_UPDATE_COPY_KEEP_DAYS + 1)}"
    new = fresh_boot / f"history.db.{_stamp(1)}"
    old.write_bytes(b"stand-in")
    new.write_bytes(b"stand-in")
    out = dbmod.expire_pre_update_copies()
    assert out == {"deleted": [old.name], "held": [new.name]}
    assert not old.exists() and new.exists()
    said = capsys.readouterr().out
    assert f"pre-update copy deleted: {old.name}" in said
    # A copy still held is NAMED at every boot, with what it holds and until when.
    assert "pre-update copy held: " in said and "in plaintext" in said


def test_the_boot_runs_the_expiry():
    assert "expire_pre_update_copies()" in inspect.getsource(dbmod.init_db)


def _backup_home():
    """The in-app backup route's module: the template keeps it in
    app.routers.admin, a surface without routers in app.main."""
    import importlib
    for name in ("app.routers.admin", "app.main"):
        try:
            m = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(m, "run_backup"):
            return m
    raise AssertionError("no module carries run_backup")


def test_the_in_app_backup_does_not_carry_the_copy(monkeypatch, tmp_path):
    import tarfile
    m = _backup_home()
    data = tmp_path / "data"
    (data / "pre-update").mkdir(parents=True)
    (data / "pre-update" / f"history.db.{_stamp(1)}").write_bytes(b"plaintext stand-in")
    (data / "notes.txt").write_text("kept", encoding="utf-8")
    monkeypatch.setattr(m, "_DATA_DIR", str(data))
    monkeypatch.setattr(m, "_BACKUP_DIR", str(data / "backups"))
    monkeypatch.setattr(dbmod, "PRE_UPDATE_COPY_DIR", str(data / "pre-update"))
    m.run_backup(current_user={"id": 1, "username": "owner", "role": "owner"})
    (archive,) = list((data / "backups").glob("*.tar.gz"))
    with tarfile.open(archive) as t:
        names = t.getnames()
    assert any(n.endswith("notes.txt") for n in names), names      # the control
    assert not any("pre-update" in n for n in names), names
