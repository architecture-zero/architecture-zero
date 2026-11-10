"""A write door refuses a department name the vector store cannot hold
(2026-11-10).

A department is created by its first write: its documents go into the
collection database._collection_name names ("kb_" and the name lowercased,
every character outside [a-z0-9_-] an underscore), and the store takes a
collection name of 3 to 63 characters that starts and ends with a letter or
digit. The ingest and upload routes passed any name through, so "sales-",
"Sales Team!" (its key is "sales_team_") and a 61-character name each reached
the store's own ValueError after the body had been read and scanned - a 500
that named neither the department nor the rule.

FIX: both routes answer a 400 with one plain sentence, for every name the
store would refuse, and the store is never asked.
CONTROL: every name the store takes is written as before, and the source
delete - which creates no collection - is not asked. The rule is the store's
own: for every name here the door's verdict is chromadb's check_index_name's.

The suite's store takes any collection name. In these tests opening a
department's collection asks chroma's own name check first, as its client
does, so the code before this change fails the way it failed in production.
"""
import pytest
from chromadb.api.segment import check_index_name

import app.database as database

REFUSED = ["sales-", "sales_", "Sales Team!", "ops ", "-", "   ", "x" * 61]
TAKEN = ["sales", "Sales Team", "hr-2", "_ops", "x" * 60, "general", "General"]


@pytest.fixture
def opened(monkeypatch):
    """The collection names opened for a write, each checked by chroma's own
    rule before the suite's stand-in store answers."""
    names: list[str] = []
    real = database._get_collection

    def _open(department=None):
        name = database._collection_name(department)
        check_index_name(name)
        names.append(name)
        return real(department)
    monkeypatch.setattr(database, "_get_collection", _open)
    return names


def _ingest(client, headers, department: str, doc_id: str = "shape-probe"):
    return client.post("/api/ingest", headers=headers, json={
        "doc_id": doc_id, "text": "A page of the handbook.",
        "department": department, "metadata": {"source": "shape-probe.md"}})


def _upload(client, headers, department: str):
    return client.post("/api/ingest/upload", headers=headers,
                       data={"department": department},
                       files={"file": ("shape-probe.md", b"A page of the handbook.",
                                       "text/markdown")})


# -- FIX -------------------------------------------------------------------------

@pytest.mark.parametrize("name", REFUSED)
def test_ingest_refuses_a_name_the_store_cannot_hold(client, admin_headers, opened, name):
    r = _ingest(client, admin_headers, name)
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("Department name ")
    assert opened == [], "the store was asked for a collection it would refuse"


@pytest.mark.parametrize("name", ["sales-", "x" * 61])
def test_upload_refuses_a_name_the_store_cannot_hold(client, admin_headers, opened, name):
    r = _upload(client, admin_headers, name)
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("Department name ")
    assert opened == []


def test_the_refusal_says_which_rule_and_echoes_no_name(client, admin_headers, opened):
    long_name = "x" * 61
    r = _ingest(client, admin_headers, long_name)
    assert r.json()["detail"] == "Department name is too long: at most 60 characters"
    assert long_name not in r.text
    r = _ingest(client, admin_headers, "Sales Team!")
    assert "must end with a letter or a digit" in r.json()["detail"]
    assert "Sales Team!" not in r.text


@pytest.mark.parametrize("name", REFUSED + TAKEN)
def test_the_doors_verdict_is_the_stores_own(name):
    try:
        check_index_name(database._collection_name(name))
        store_takes_it = True
    except ValueError:
        store_takes_it = False
    assert (database.department_name_refusal(name) is None) == store_takes_it


# -- CONTROL ---------------------------------------------------------------------

@pytest.mark.parametrize("name", TAKEN)
def test_control_a_name_the_store_takes_is_written_as_before(
        client, admin_headers, opened, name):
    r = _ingest(client, admin_headers, name, doc_id="shape-probe-ok")
    assert r.status_code == 200 and r.json()["status"] == "ingested", r.text
    assert database._collection_name(name) in opened


def test_control_an_upload_into_a_name_the_store_takes_lands(client, admin_headers, opened):
    try:
        r = _upload(client, admin_headers, "Sales Team")
        assert r.status_code == 200, r.text
        assert r.json()["status"] in ("ingested", "queued")
        assert "kb_sales_team" in opened
    finally:
        # Whatever the upload left on disk for this name goes with its source.
        client.delete("/api/ingest/source/shape-probe.md",
                      params={"department": "Sales Team"}, headers=admin_headers)


def test_control_the_source_delete_is_not_asked(client, admin_headers, opened):
    """It creates no collection: a name the store cannot hold names nothing
    to delete, and answers as it did."""
    r = client.delete("/api/ingest/source/shape-probe.md",
                      params={"department": "sales-"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert "Department name" not in r.text
    assert opened == []
