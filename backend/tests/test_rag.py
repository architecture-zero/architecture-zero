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


def _stand_in_index(monkeypatch, rows):
    """The router's index seams over one dict of source -> chunk count: the
    listing reads it, the delete pops the name it was handed and records the
    call. There is no filesystem anywhere in it, which is the point of the
    delete-path tests below."""
    calls = []

    def list_sources(department=None):
        return [{"source": s, "count": c, "department": "general"}
                for s, c in sorted(rows.items())]

    def delete_source(source, department=None):
        calls.append((source, department))
        rows.pop(source, None)

    monkeypatch.setattr("app.routers.kb.list_sources", list_sources)
    monkeypatch.setattr("app.routers.kb.delete_source", delete_source)
    return calls


def test_delete_source_takes_a_subfolder_name(client, admin_headers, monkeypatch):
    """The sync keys a subfolder file by its path relative to the knowledge
    root (ingest_sync._sync_knowledge_dir), so a listed source can hold a
    slash. The route takes the whole remainder of the request path
    ({source:path}, 2026-10-28). Until then it took one segment, and because
    the server decodes the request path before matching, the literal and the
    %2F-encoded form of such a name both missed the route and answered 404:
    the source was listed and could not be deleted. A root name deletes as
    before."""
    rows = {"sub/x.md": 2, "deep/er/y.md": 1, "root.md": 3}
    calls = _stand_in_index(monkeypatch, rows)

    r = client.delete("/api/ingest/source/sub/x.md", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "deleted", "source": "sub/x.md", "department": "general"}
    assert calls == [("sub/x.md", None)]
    listed = [s["source"] for s in
              client.get("/api/ingest/sources", headers=admin_headers).json()["sources"]]
    assert "sub/x.md" not in listed and "deep/er/y.md" in listed

    # The KB page sends the name percent-encoded (encodeURIComponent); the
    # server decodes it before matching, so the handler sees the same name.
    r = client.delete("/api/ingest/source/deep%2Fer%2Fy.md", params={"department": "general"},
                      headers=admin_headers)
    assert r.status_code == 200, r.text
    assert calls[-1] == ("deep/er/y.md", "general")

    r = client.delete("/api/ingest/source/root.md", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert calls[-1] == ("root.md", None)
    assert rows == {}


def test_delete_source_name_is_an_index_key_not_a_path(client, admin_headers, monkeypatch):
    """A name may carry slashes and dot segments now. The delete has no
    filesystem leg for them to reach: the name goes verbatim to the index
    (delete_source, keyed by the stamped source metadata) under the source's
    lock, and nothing in the route builds a path from it. Sent encoded, so
    the client does not fold the dot segments away before the server sees
    them."""
    import inspect
    from app.routers import kb
    rows = {"../escape.md": 1}
    calls = _stand_in_index(monkeypatch, rows)
    r = client.delete("/api/ingest/source/..%2Fescape.md", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert calls == [("../escape.md", None)] and rows == {}
    src = inspect.getsource(kb.remove_source)
    for token in ("KNOWLEDGE_DIR", "pathlib", "os.path", "os.remove", "unlink", "open("):
        assert token not in src, token


def test_delete_source_refuses_an_empty_name(client, admin_headers, monkeypatch):
    """{source:path} (2026-10-28) made the empty remainder match the route:
    DELETE /api/ingest/source/ reached the handler, deleted nothing, and
    answered 200 "deleted" - before the convertor that request missed every
    route and answered 404 - and the bare /api/ingest/source lands on the
    same place through its 307. An empty or whitespace-only name is no
    source's name and answers 400 with a fixed detail (2026-10-29), decided
    from the string alone - the index is never asked. A real name still
    deletes."""
    rows = {"real.md": 1}
    calls = _stand_in_index(monkeypatch, rows)

    for path in ("/api/ingest/source/", "/api/ingest/source/%20%20"):
        r = client.delete(path, headers=admin_headers)
        assert r.status_code == 400, (path, r.status_code, r.text)
        assert r.json() == {"detail": "Invalid source name"}, (path, r.text)
    # The bare path follows its redirect to the empty remainder.
    r = client.delete("/api/ingest/source", headers=admin_headers)
    assert r.status_code == 400, (r.status_code, r.text)
    assert calls == [] and rows == {"real.md": 1}

    r = client.delete("/api/ingest/source/real.md", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert calls == [("real.md", None)] and rows == {}
