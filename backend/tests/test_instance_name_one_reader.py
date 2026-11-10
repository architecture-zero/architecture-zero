"""One name, wherever the backend says it (2026-11-10).

An instance can be named in two places: VITE_INSTANCE_NAME in the host's
environment, and the `instance_name` setting (seeded from that variable at the
first boot, written afterwards through the admin config). The page and
/api/config read the setting. Three other places read the variable alone, with
this product's name as the fallback - /api/status, the issuer label an
authenticator app shows after MFA enrollment, and the subject of every
self-check alert. So an instance named in its settings, on a backend whose
environment does not name it, answered with the product's name in all three:
a status body, an authenticator entry and a disk alarm naming something the
operator never called it.

FIX: where the environment names nothing - the variable unset, or holding the
product's own name, which is the line .env.example ships - each of the three
reports the name saved in the setting (config.instance_name is the one
reader).
CONTROL: an instance the environment gives a name of its own keeps that name
in all three, even beside a different saved one - nothing moves for a
deployment that names itself there; one with neither still reports the
product's name; and /api/config hands back the setting as stored.
GUARD: the setting is free text an admin account can write, and it now
reaches a mail header and a QR code. What the three places receive from it is
one line of at most INSTANCE_NAME_MAX_CHARS characters, and an alert is still
composed when the setting cannot be read at all.
"""
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from app import self_check

SAVED = "Acme Assistant"
PRODUCT = "Architecture Zero"
_USER = {"username": "name_reader_probe", "password": "NameReader#2026x"}


@pytest.fixture
def save_name():
    """Saves a name in the setting for one test, then puts back what was
    there - other files in this session leave names of their own in the row."""
    from app.config import get_config, set_config
    before = get_config("instance_name", "")
    yield lambda value: set_config("instance_name", value)
    set_config("instance_name", before)


@pytest.fixture
def no_variable(monkeypatch):
    """A backend whose environment does not name the instance."""
    monkeypatch.delenv("VITE_INSTANCE_NAME", raising=False)


@pytest.fixture
def titles(monkeypatch):
    """The subject of every alert the self-check raises, in order. Its own
    record of what each pass found goes back after the test."""
    for name in ("_rag_last", "_ollama_last", "_redis_last"):
        monkeypatch.setattr(self_check, name, getattr(self_check, name))
    sent = []
    monkeypatch.setattr(self_check, "fire_alert",
                        lambda key, title, body: sent.append(title))
    return sent


@pytest.fixture
def probe(client, admin_headers):
    """A signed-in account with no authenticator, to enroll one."""
    from app.db import get_session
    from app.models import User
    from app.users import get_user_by_username

    existing = get_user_by_username(_USER["username"])
    if not existing:
        for role in ("user", "member"):
            r = client.post("/api/users", headers=admin_headers,
                            json={"current_password": "AdminPass1", **_USER, "role": role})
            if r.status_code in (200, 201):
                break
        assert r.status_code in (200, 201), f"no accepted role: {r.text}"
        existing = get_user_by_username(_USER["username"])
    r = client.post("/api/auth/login", json=_USER)
    assert r.status_code == 200, r.text
    yield {"Authorization": f"Bearer {r.json()['access_token']}"}
    with get_session() as db:
        db.query(User).filter(User.id == existing["id"]).update(
            {"mfa_enabled": False, "mfa_secret": None})


class _Usage:
    def __init__(self, used, total):
        self.used, self.total, self.free = used, total, total - used


class _NotOllama:
    """An answer from the Ollama URL that is not Ollama's model list."""
    status_code = 404

    def json(self):
        return {"detail": "Not Found"}


def _no_answer(path, timeout):
    raise ConnectionError("refused")


def _status_name(client, admin_headers):
    return client.get("/api/status", headers=admin_headers).json()["instance_name"]


def _enroll(client, headers):
    """The authenticator's entry for a fresh enrollment: its label and issuer."""
    r = client.post("/api/auth/mfa/setup", headers=headers,
                    json={"current_password": _USER["password"]})
    assert r.status_code == 200, r.text
    uri = urlparse(r.json()["uri"])
    return unquote(uri.path).lstrip("/"), parse_qs(uri.query)["issuer"]


def _disk_alarm(monkeypatch, tmp_path):
    monkeypatch.setattr(self_check.shutil, "disk_usage", lambda p: _Usage(90, 100))
    self_check.probe_disk(str(tmp_path))


def _alarm_each_check(monkeypatch, tmp_path):
    """Trips the self-check's alerts, one of each: the disk, Ollama silent,
    Ollama's URL answering something else, a backup heartbeat, the retrieval
    lane."""
    _disk_alarm(monkeypatch, tmp_path)
    self_check.probe_ollama(_no_answer)
    self_check.probe_ollama(lambda path, timeout: _NotOllama())
    self_check.probe_backups(lambda fname: {"ok": False, "age_hours": None,
                                            "reason": "status file missing/unreadable"},
                             kinds=("backup",))
    self_check.probe_rag(lambda: {"state": "error", "reason": "embed_unreachable"})


# -- FIX: where the environment names nothing, the saved name is the name -----

def test_status_reports_the_saved_name(client, admin_headers, no_variable, save_name):
    save_name(SAVED)
    assert _status_name(client, admin_headers) == SAVED
    assert (client.get("/api/config", headers=admin_headers).json()["instance_name"]
            == SAVED), "the page's name and the status body's are one name"


def test_mfa_enrollment_labels_the_authenticator_with_the_saved_name(
        client, probe, no_variable, save_name):
    save_name(SAVED)
    assert _enroll(client, probe) == (f"{SAVED}:{_USER['username']}", [SAVED])


def test_every_self_check_alert_carries_the_saved_name(
        monkeypatch, tmp_path, titles, no_variable, save_name):
    save_name(SAVED)
    _alarm_each_check(monkeypatch, tmp_path)
    assert titles == [f"Disk usage high - {SAVED}",
                      f"Ollama unreachable - {SAVED}",
                      f"Ollama unreachable - {SAVED}",
                      f"Backup alarm: backup not ok - {SAVED}",
                      f"Retrieval lane failing - {SAVED}"]


def test_the_example_files_line_in_the_environment_names_nothing(
        client, admin_headers, probe, monkeypatch, tmp_path, titles, save_name):
    """.env.example ships VITE_INSTANCE_NAME=Architecture Zero. A deployment
    that copied the file and then named itself in its settings carries the
    variable and still means the saved name."""
    monkeypatch.setenv("VITE_INSTANCE_NAME", PRODUCT)
    save_name(SAVED)
    assert _status_name(client, admin_headers) == SAVED
    assert _enroll(client, probe)[1] == [SAVED]
    _disk_alarm(monkeypatch, tmp_path)
    assert titles == [f"Disk usage high - {SAVED}"]


def test_a_rename_holds_from_the_next_alert(
        monkeypatch, tmp_path, titles, no_variable, save_name):
    """Read when the alert is composed. It was read once, when the module was
    imported, so even the variable's own value waited for a restart."""
    save_name("First Name")
    _disk_alarm(monkeypatch, tmp_path)
    save_name("Second Name")
    _disk_alarm(monkeypatch, tmp_path)
    monkeypatch.setenv("VITE_INSTANCE_NAME", "Named In The Environment")
    _disk_alarm(monkeypatch, tmp_path)
    assert titles == ["Disk usage high - First Name", "Disk usage high - Second Name",
                      "Disk usage high - Named In The Environment"]


# -- CONTROL: what these places said before, they still say -------------------

def test_control_an_instance_the_environment_names_keeps_that_name(
        client, admin_headers, probe, monkeypatch, save_name):
    """The variable outranks the setting here, as it did when it was the only
    thing read: a deployment that sets it sees nothing move."""
    monkeypatch.setenv("VITE_INSTANCE_NAME", "Named In The Environment")
    save_name(SAVED)
    assert _status_name(client, admin_headers) == "Named In The Environment"
    assert _enroll(client, probe)[1] == ["Named In The Environment"]


def test_control_no_variable_and_no_saved_name_is_the_products_name(
        client, admin_headers, probe, monkeypatch, tmp_path, titles, no_variable, save_name):
    for unnamed in ("", PRODUCT):          # a blank setting, and the seeded one
        save_name(unnamed)
        assert _status_name(client, admin_headers) == PRODUCT
        assert _enroll(client, probe) == (f"{PRODUCT}:{_USER['username']}", [PRODUCT])
    _alarm_each_check(monkeypatch, tmp_path)
    assert titles and all(t.endswith(f" - {PRODUCT}") for t in titles)


def test_control_the_config_route_hands_back_the_setting_as_stored(
        client, admin_headers, save_name):
    save_name("  Spaced   Out  ")
    assert (client.get("/api/config", headers=admin_headers).json()["instance_name"]
            == "  Spaced   Out  ")


def test_control_the_example_file_still_ships_the_products_name():
    """The rule above rests on this line: a value equal to it is read as no
    choice at all. If the example's name ever changes, the reader's must."""
    from pathlib import Path
    example = Path(__file__).resolve().parents[2] / ".env.example"
    if not example.exists():
        pytest.skip("the repo's .env.example is not beside this suite")
    assert f"VITE_INSTANCE_NAME={PRODUCT}" in example.read_text(
        encoding="utf-8").splitlines()


# -- GUARD: what an admin can save, and what the three places receive ---------

def test_a_saved_name_reaches_them_as_one_bounded_line(
        client, admin_headers, probe, monkeypatch, tmp_path, titles, no_variable, save_name):
    from app.config import DEFAULT_INSTANCE_NAME, INSTANCE_NAME_MAX_CHARS, instance_name
    assert DEFAULT_INSTANCE_NAME == PRODUCT
    save_name("Acme" + chr(13) + chr(10) + "Bcc: someone@example.com" + chr(9) + "x" * 5000)
    name = instance_name()
    assert name.startswith("Acme Bcc: someone@example.com x")
    assert len(name) == INSTANCE_NAME_MAX_CHARS and name.isprintable()
    assert _status_name(client, admin_headers) == name
    assert _enroll(client, probe)[1] == [name]      # a QR code was drawn for it
    _disk_alarm(monkeypatch, tmp_path)
    assert titles == [f"Disk usage high - {name}"]
    # The mail channel sets the subject as a header, and a header refuses a
    # line break outright: as saved, this name would have cost the alert.
    EmailMessage()["Subject"] = titles[0]


def test_an_alert_is_still_composed_when_the_setting_cannot_be_read(
        monkeypatch, tmp_path, titles, no_variable):
    import app.config as config

    def broken(key, default=""):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(config, "get_config", broken)
    _disk_alarm(monkeypatch, tmp_path)
    assert titles == [f"Disk usage high - {PRODUCT}"]


def test_the_shared_module_alerts_as_before_on_a_surface_without_the_reader(
        monkeypatch, tmp_path, titles, save_name):
    """app/self_check.py is one text on every surface that carries it, and a
    surface may take the file before its config has instance_name. There an
    alert carries what it always did - the variable, else the product's
    name - so the file can be copied as it is."""
    import app.config as config
    monkeypatch.delattr(config, "instance_name", raising=False)
    save_name(SAVED)
    monkeypatch.setenv("VITE_INSTANCE_NAME", "Named In The Environment")
    _disk_alarm(monkeypatch, tmp_path)
    monkeypatch.delenv("VITE_INSTANCE_NAME")
    _disk_alarm(monkeypatch, tmp_path)
    assert titles == ["Disk usage high - Named In The Environment",
                      f"Disk usage high - {PRODUCT}"]
