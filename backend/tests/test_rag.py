def test_ingest_text(client, admin_headers):
    r = client.post("/api/ingest", json={
        "doc_id": "test-doc-1",
        "text": "Architecture Zero is a self-hosted AI platform.",
        "metadata": {"source": "test.txt"},
    }, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "ingested"


def test_ingest_requires_auth(client):
    r = client.post("/api/ingest", json={
        "doc_id": "test-doc-2",
        "text": "Should be rejected without a token.",
        "metadata": {"source": "test2.txt"},
    })
    assert r.status_code == 401


def test_list_sources(client, admin_headers):
    r = client.get("/api/ingest/sources", headers=admin_headers)
    assert r.status_code == 200
    assert "sources" in r.json()


def test_upload_text_file(client, admin_headers):
    r = client.post(
        "/api/ingest/upload",
        files={"file": ("smoke.txt", b"Architecture Zero smoke test document.", "text/plain")},
        headers=admin_headers,
    )
    assert r.status_code == 200
