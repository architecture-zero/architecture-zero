"""Boot the app in THIS interpreter on the database DATABASE_URL names, use it
the way its people would, and print what happened as one JSON line.

Not a test. tests/test_upgrade_from_release.py runs it as a subprocess,
because the path under test - create_all, the ADD COLUMN list, the rebuild,
the two at-rest sweeps and the orphan sweep - runs while app.main is being
IMPORTED. The suite's own interpreter did that import long ago, against a
database today's models created, so it cannot be asked to do it again against
an old one.

    cd backend && DATABASE_URL=sqlite:///old.db JWT_SECRET_KEY=... \\
        UPGRADE_PEOPLE='{...}' python -m tests.upgrade_boot
"""
import json
import os
from unittest.mock import MagicMock, patch

# The same stand-ins conftest.py uses, for the same reason: the vector store
# and the embedder are not what this proves, and neither may reach a socket.
_col = MagicMock()
_col.count.return_value = 0
_col.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
_col.get.return_value = {"ids": [], "metadatas": []}
_col.upsert.return_value = None
_col.delete.return_value = None
_chroma = MagicMock()
_chroma.get_or_create_collection.return_value = _col
_chroma.list_collections.return_value = []
patch("chromadb.PersistentClient", return_value=_chroma).start()
patch("chromadb.Settings", return_value=MagicMock()).start()

from app.main import app  # noqa: E402 - THE BOOT: everything under test runs on this line

patch("app.database._embed", return_value=[0.0] * 768).start()


def _no_outbound(*args, **kwargs):
    import requests
    raise requests.exceptions.ConnectionError("upgrade_boot: no outbound calls")


patch("app.database.requests.post", side_effect=_no_outbound).start()

import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import inspect  # noqa: E402

from app.db import Base, engine  # noqa: E402

P = json.loads(os.environ["UPGRADE_PEOPLE"])
report: dict = {}

# -- What today's models declare that this database does not hold -------------
# create_all() builds a missing TABLE and never touches one that exists, so a
# column or an index added to a model reaches a new deployment and no upgraded
# one unless app/db.py carries it across. Columns and indexes only: a changed
# type or constraint on an existing column is not visible from here.
insp = inspect(engine)
missing_columns: dict = {}
missing_indexes: dict = {}
for table in Base.metadata.sorted_tables:
    if not insp.has_table(table.name):
        missing_columns[table.name] = "THE WHOLE TABLE"
        continue
    have = {c["name"] for c in insp.get_columns(table.name)}
    gone = sorted(c.name for c in table.columns if c.name not in have)
    if gone:
        missing_columns[table.name] = gone
    have_ix = {i["name"] for i in insp.get_indexes(table.name)}
    gone_ix = sorted(i.name for i in table.indexes if i.name and i.name not in have_ix)
    if gone_ix:
        missing_indexes[table.name] = gone_ix
report["missing_columns"] = missing_columns
report["missing_indexes"] = missing_indexes

# -- The saved provider key, as the code that calls the provider reads it -----
from app.providers import _get_runtime  # noqa: E402

report["provider_key_reads_as_saved"] = (
    _get_runtime("anthropic_api_key", "ANTHROPIC_API_KEY", "") == P["provider_key"])

with TestClient(app) as c:
    # The Owner: the password, then a code from the same authenticator.
    r = c.post("/api/auth/login",
               json={"username": P["owner"], "password": P["owner_password"]})
    body = r.json()
    report["owner_password_step"] = {"status": r.status_code,
                                     "mfa_required": bool(body.get("mfa_required"))}
    owner = {}
    if body.get("mfa_token"):
        r = c.post("/api/auth/mfa/complete",
                   json={"mfa_token": body["mfa_token"],
                         "code": pyotp.TOTP(P["totp_seed"]).now()})
        report["owner_code_step"] = {"status": r.status_code,
                                     "detail": r.json().get("detail")}
        if r.status_code == 200:
            owner = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = c.post("/api/auth/login",
               json={"username": P["member"], "password": P["member_password"]})
    report["member_login"] = {"status": r.status_code}
    member = ({"Authorization": f"Bearer {r.json()['access_token']}"}
              if r.status_code == 200 else {})

    # A session someone left signed in across the upgrade.
    r = c.post("/api/auth/refresh",
               headers={"Authorization": f"Bearer {P['refresh_token']}"})
    report["left_signed_in"] = {"status": r.status_code,
                                "detail": r.json().get("detail")}

    r = c.get("/api/settings", headers=owner)
    report["settings"] = {"status": r.status_code,
                          "anthropic_key_set": r.json().get("anthropic_key_set")}

    r = c.get("/api/users", headers=owner)
    report["roster"] = {"status": r.status_code,
                        "users": sorted(
                            [u["username"], u["role"], u["department"],
                             bool(u["mfa_enabled"]), bool(u.get("mfa_secret_unreadable"))]
                            for u in r.json().get("users", []))}

    r = c.get(f"/api/history/{P['session_id']}", headers=member)
    report["history"] = {"status": r.status_code,
                         "contents": [m.get("content") for m in r.json().get("messages", [])]}

    r = c.get("/api/sessions/mine", headers=member)
    report["sidebar"] = {"status": r.status_code,
                         "sessions": sorted([s.get("session"), s.get("name")]
                                            for s in r.json().get("sessions", []))}

    r = c.get("/api/admin/kb/quarantine", headers=owner)
    report["quarantine"] = {"status": r.status_code,
                            "held": sorted(i.get("source") for i in r.json().get("items", []))}

# -- A write into the columns the upgrade added -------------------------------
from app.audit import log_audit_entry  # noqa: E402

try:
    log_audit_entry(user_id=None, username=None, session_id="after-the-upgrade",
                    prompt="a question asked after the upgrade", response_length=1,
                    model=None, use_rag=False, sources=[],
                    pii_out_hits=2, pii_out_redacted=2, pii_out_types="email,phone")
    report["audit_write"] = "ok"
except Exception as e:  # the report carries it; the test names it
    report["audit_write"] = f"{type(e).__name__}: {e}"

print("REPORT " + json.dumps(report), flush=True)
