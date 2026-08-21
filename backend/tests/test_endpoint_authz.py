"""Route-level authorization on operator endpoints (defense in depth).

The KB-mutation endpoints (ingest/upload/sources/delete-source) already
require manage_kb (see test_rag.py). This covers the operator views - they
must require auth at the route level, holding even if ENABLE_AUTH is ever
flipped off (conftest runs ENABLE_AUTH=false).
"""


def test_sessions_requires_auth(client):
    assert client.get("/api/sessions").status_code == 401


def test_analytics_requires_auth(client):
    assert client.get("/api/analytics").status_code == 401


def test_feedback_summary_requires_auth(client):
    assert client.get("/api/feedback/summary").status_code == 401


def test_sessions_admin_ok(client, admin_headers):
    assert client.get("/api/sessions", headers=admin_headers).status_code == 200


def test_analytics_admin_ok(client, admin_headers):
    assert client.get("/api/analytics", headers=admin_headers).status_code == 200
