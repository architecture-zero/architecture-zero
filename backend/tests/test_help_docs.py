"""In-product help (ported 2026-09-21).

The assistant answers questions about itself from help pages shipped in the
image, synced at boot into a RESERVED collection that is never a department.
These pin the claims the design rests on: the pages exist and chunk by
section; the sync is idempotent and cleans up after itself, with the store as
the witness; the help collection is invisible to every corpus-wide
enumeration (department list, residue, source listing, document count,
fingerprint, the flagged-source lists); the chat route's help lane reads that
collection ALONE, for a guest and for a signed-in user alike, with the peers
left out and the tier gate not in the way; the reserved name is refused on
every write door; the page read is gated like chat and exempt from the
middleware like chat.
"""
import inspect
import json
from unittest.mock import patch

import pytest

from app import database, help_docs


def _mock_stream_events(messages, model, tools=None, system_prompt="", max_tokens=1024):
    yield {"type": "text", "text": "Hello"}
    yield {"type": "text", "text": " world"}


def _capturing_stream(captured):
    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        captured["messages"] = messages
        yield {"type": "text", "text": "Type your username and password."}
    return _stream


def _guest_cfg(key, default=None):
    if key == "guest_mode_enabled":
        return "true"
    return default


PAGES = {"getting-started.md", "answers-and-sources.md", "documents-and-access.md",
         "privacy-and-data.md", "for-administrators.md"}


# -- the pages themselves ---------------------------------------------------------

def test_help_pages_ship_in_the_image_and_chunk_by_section():
    names = {p.name for p in help_docs.pages()}
    assert PAGES <= names
    for p in help_docs.pages():
        chunks = help_docs.chunk_sections(p.read_text(encoding="utf-8"))
        assert len(chunks) >= 3, p.name
        # every section chunk carries its own heading, so a citation names the
        # section that answered, and no chunk is a runaway
        assert all(c.startswith("## ") for c in chunks[1:]), p.name
        assert all(len(c) <= help_docs._CHUNK + help_docs._OVERLAP + 200 for c in chunks), p.name


def test_the_pages_describe_this_surface_and_not_the_product_it_was_ported_from():
    """Every fact in a help page was checked against this repo's code. The
    easiest drift is a page carried over from the product surface describing
    a connector or a chat integration the template does not ship."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in help_docs.pages()).lower()
    assert "microsoft teams" not in text and "sharepoint" not in text
    assert "google drive" not in text and "onedrive" not in text


def test_read_page_is_membership_gated():
    assert help_docs.read_page("help/getting-started.md").startswith("# ")
    assert help_docs.read_page("help/../main.py") is None
    assert help_docs.read_page("getting-started.md") is None
    assert help_docs.read_page("help/nope.md") is None


# -- the sync ---------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HELP_SYNC_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def store(state_dir):
    """A fake help collection that REMEMBERS what was written, so the sync's
    witness (list_sources per page) answers from the same writes the test
    made - and so a lost index can be simulated by forgetting a page."""
    added: dict[str, int] = {}

    def _add(entries, department=None, **kw):
        assert department == "help"
        for _doc_id, _text, meta in entries:
            assert meta["trust"] == "curated" and meta["source"].startswith("help/")
        added[entries[0][2]["source"]] = len(entries)
        return len(entries)

    def _delete(source, department=None):
        assert department == "help"
        added.pop(source, None)

    def _list(department=None):
        assert department == "help"
        return [{"source": s, "count": c, "department": "help"} for s, c in added.items()]

    with patch.object(help_docs.database, "add_documents_batch", side_effect=_add) as add, \
         patch.object(help_docs.database, "delete_source", side_effect=_delete) as delete, \
         patch.object(help_docs.database, "list_sources", side_effect=_list), \
         patch.object(help_docs.database, "flush_vector_segments",
                      return_value={"flushed": 1, "clean": 0, "errors": 0}) as flush:
        yield {"added": added, "add": add, "delete": delete, "flush": flush}


def test_sync_ingests_once_and_skips_unchanged_pages(state_dir, store):
    first = help_docs.sync()
    assert first["pages"] >= 5
    assert first["ingested"] == first["pages"] and first["unchanged"] == 0
    assert store["add"].call_count == first["pages"]
    # the page's old chunks go first, so a shorter rewrite leaves no tail
    assert store["delete"].call_count == first["pages"]
    # a sync that wrote persists the vectors now, not at the next graceful stop
    assert store["flush"].call_count == 1
    second = help_docs.sync()
    assert second["ingested"] == 0 and second["unchanged"] == first["pages"]
    assert store["add"].call_count == first["pages"]
    assert store["flush"].call_count == 1          # nothing written, nothing flushed
    assert (state_dir / "help_sync_state.json").is_file()


def test_sync_re_embeds_a_changed_page_and_removes_a_deleted_one(state_dir, store):
    help_docs.sync()
    n = store["add"].call_count
    state_file = state_dir / "help_sync_state.json"
    state = json.loads(state_file.read_text())
    state["help/getting-started.md"]["sha"] = "stale-sha"            # the page changed
    state["help/retired-page.md"] = {"sha": "gone", "chunks": 3}     # a page left the image
    state_file.write_text(json.dumps(state))
    res = help_docs.sync()
    assert res["ingested"] == 1 and res["removed"] == 1
    assert store["add"].call_count == n + 1
    store["delete"].assert_any_call("help/retired-page.md", department="help")
    assert "help/retired-page.md" not in json.loads(state_file.read_text())
    assert help_docs.sync(force=True)["ingested"] == res["pages"]


def test_the_store_is_the_witness_not_the_state_file(state_dir, store):
    """An unclean stop can lose a small collection's vectors while the
    metadata and the state file survive. A state file that says "unchanged"
    must not be believed over a store that holds nothing."""
    help_docs.sync()
    n = store["add"].call_count
    store["added"].pop("help/privacy-and-data.md")        # the index lost this page
    res = help_docs.sync()
    assert res["ingested"] == 1 and res["unchanged"] == res["pages"] - 1
    assert store["add"].call_count == n + 1
    assert store["add"].call_args.args[0][0][2]["source"] == "help/privacy-and-data.md"


def test_the_boot_stage_calls_the_sync_and_honours_the_switch():
    """The def is not the sync; the boot call is. main's background task must
    call help_docs.sync when HELP_DOCS is on and log the disabled line when
    off - a lane silent when off is indistinguishable from one that failed."""
    from app import main as main_mod
    src = inspect.getsource(main_mod.startup_tasks)
    assert "help_docs.sync" in src
    assert 'log("help_docs_sync_disabled")' in src
    assert "runtime_config.HELP_DOCS_SYNC" in src


# -- reserved, by construction --------------------------------------------------------

class _Col:
    def __init__(self, name, count=0):
        self.name, self._count = name, count

    def count(self):
        return self._count

    def get(self, include=None, **kw):
        return {"metadatas": [{"source": f"{self.name}-doc"}] * self._count}


class _Client:
    def __init__(self, cols):
        self._cols = list(cols)
        self.created: list[str] = []

    def list_collections(self):
        return list(self._cols)

    def get_or_create_collection(self, name, metadata=None):
        self.created.append(name)
        return next((c for c in self._cols if c.name == name), _Col(name))


def test_the_help_collection_is_invisible_to_every_corpus_enumeration(monkeypatch):
    monkeypatch.setattr(database, "client", _Client([
        _Col("kb_help", 40), _Col("kb_restricted", 3), _Col("knowledge_base", 10)]))
    assert database.list_departments() == ["general", "restricted"]
    assert database.department_residue() == []
    assert database.count_documents() == 13
    assert {r["department"] for r in database.list_sources()} == {"general", "restricted"}
    assert database.corpus_fingerprint().startswith("src=2;chunks=13;")
    assert database.list_pii_sources() == []
    assert database.list_injection_flagged_sources() == []


def test_only_department_reads_the_named_collection_alone(monkeypatch):
    """The scope leg at the database seam: the global collection is never
    opened on the help lane, and always is on every other read."""
    fake = _Client([_Col("kb_help", 40), _Col("knowledge_base", 10)])
    monkeypatch.setattr(database, "client", fake)
    seen: list = []
    monkeypatch.setattr(database, "_existing_collection",
                        lambda dept=None: (seen.append(dept), fake._cols[0])[1])
    monkeypatch.setattr(database, "_embed", lambda q, retries=2: [0.0])
    monkeypatch.setattr(database, "_hybrid_rank", lambda docs, dists, metas, q, n: [])
    monkeypatch.setattr(database, "_lexical_top_ids", lambda *a, **k: [])
    database.query_similar("how do I sign in", department="help", only_department=True)
    assert seen == ["help"] and fake.created == []
    database.query_similar("how do I sign in", department="help")
    assert fake.created == ["knowledge_base"]


def test_retrieve_help_lane_bypasses_the_tier_gate_for_a_guest(monkeypatch):
    """retrieve() drops every department above the caller's rung, and an
    unlisted department is Owner-only - so without its own leg the help lane
    would read nothing for a guest. The leg reads the help collection alone,
    for level 0, and never widens to the global merge; the normal path still
    gates the same name out, which is what keeps it from being a department."""
    import app.rerank as rr
    seen: dict = {}

    def _spy(q, n_results=5, department=None, only_department=False):
        seen.clear()
        seen.update(department=department, only_department=only_department)
        return []

    monkeypatch.setattr("app.database.query_similar", _spy)
    monkeypatch.setattr(rr, "rerank", lambda q, cands, top_k=None, stats=None: list(cands))
    rr.retrieve("how do I sign in", department="help", user_level=0, only_department=True)
    assert seen == {"department": ["help"], "only_department": True}
    rr.retrieve("how do I sign in", department="help", user_level=0)
    assert seen == {"department": None, "only_department": False}


# -- the chat route's help lane -----------------------------------------------------------

def test_help_lane_reads_only_the_help_collection_for_a_guest(client):
    """The guest door open (both halves), no account: help is honoured, read
    alone at the guest rung, and refused in help's own words when nothing
    matches."""
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_cfg), \
         patch("app.rerank.retrieve", return_value=[]) as retrieve, \
         patch("app.routers.chat.stream_chat_events", side_effect=_mock_stream_events):
        r = client.post("/api/chat", json={"prompt": "How do I sign in?", "department": "help",
                                           "use_peers": True})
    assert r.status_code == 200
    kw = retrieve.call_args.kwargs
    assert kw["department"] == "help" and kw["only_department"] is True
    assert kw["user_level"] == 0
    assert "help page" in r.text


def test_help_lane_for_a_signed_in_user_overrides_their_department_and_skips_peers(
        client, admin_headers):
    page = [{"source": "help/getting-started.md", "trust": "curated", "score": 0.9,
             "text": "## Signing in\nType your username and password."}]
    captured: dict = {}
    with patch("app.rerank.retrieve", return_value=page) as retrieve, \
         patch("app.routers.chat.stream_chat_events", side_effect=_capturing_stream(captured)), \
         patch("app.routers.chat.get_peers", return_value=[{"id": "p1", "enabled": True}]), \
         patch("app.routers.chat.query_peer_kb") as peer_call:
        r = client.post("/api/chat", json={"prompt": "How do I sign in?", "department": "help",
                                           "use_peers": True, "use_rag": False},
                        headers=admin_headers)
    assert r.status_code == 200
    kw = retrieve.call_args.kwargs
    assert kw["department"] == "help" and kw["only_department"] is True
    peer_call.assert_not_called()
    # retrieval forced on despite use_rag=false, the model held to the pages,
    # and the answer cites the page
    assert "help pages" in captured["messages"][-1]["content"]
    assert "help/getting-started.md" in r.text


def test_the_help_lane_attaches_no_agent_tools(client, admin_headers):
    """A help answer comes from the pages alone - never from a workspace file
    the agent tools could read, even on an instance that turned them on."""
    seen: dict = {}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        seen["tools"] = tools
        yield {"type": "text", "text": "ok"}

    page = [{"source": "help/getting-started.md", "trust": "curated", "score": 0.9,
             "text": "## Signing in\nType your username and password."}]
    with patch("app.rerank.retrieve", return_value=page), \
         patch("app.routers.chat.get_active_tools", return_value=[{"name": "read_file"}]), \
         patch("app.routers.chat.supports_tools", return_value=True), \
         patch("app.routers.chat.stream_chat_events", side_effect=_stream):
        r = client.post("/api/chat", json={"prompt": "How do I sign in?", "department": "help"},
                        headers=admin_headers)
        assert r.status_code == 200 and seen["tools"] is None
        r = client.post("/api/chat", json={"prompt": "How do I sign in?"},
                        headers=admin_headers)
        assert r.status_code == 200 and seen["tools"] == [{"name": "read_file"}]


def test_the_release_path_and_the_manual_sync_carry_the_help_seams():
    """The def is not the guard; the call is. The quarantine release re-ingests
    to the row's department, so it refuses the reserved name like every other
    write door; the operator's manual sync restores the help index too."""
    from app.routers import kb as kb_mod
    assert "refuse_reserved_department(department)" in inspect.getsource(kb_mod.admin_release_quarantine)
    assert "help_docs.sync()" in inspect.getsource(kb_mod.kb_sync)


def test_any_other_department_value_is_ignored_as_before(client, admin_headers):
    """The field exists for the help lane only: a caller's department is their
    account's, and naming another one changes nothing."""
    with patch("app.rerank.retrieve", return_value=[]) as retrieve, \
         patch("app.routers.chat.stream_chat_events", side_effect=_mock_stream_events):
        r = client.post("/api/chat", json={"prompt": "Hi", "department": "restricted"},
                        headers=admin_headers)
    assert r.status_code == 200
    kw = retrieve.call_args.kwargs
    assert kw["department"] == "general" and "only_department" not in kw


def test_the_reserved_name_is_refused_on_every_write_door(client, admin_headers):
    r = client.post("/api/ingest", json={"doc_id": "x", "text": "hello", "department": "help"},
                    headers=admin_headers)
    assert r.status_code == 400 and "reserved" in r.json()["detail"]
    r = client.post("/api/ingest/upload", files={"file": ("note.txt", b"hello")},
                    data={"department": "Help"}, headers=admin_headers)
    assert r.status_code == 400 and "reserved" in r.json()["detail"]
    r = client.delete("/api/ingest/source/anything", params={"department": "help"},
                      headers=admin_headers)
    assert r.status_code == 400 and "reserved" in r.json()["detail"]


def test_no_account_can_live_in_the_reserved_department(client, admin_headers):
    """An account whose department is "help" would get the help pages merged
    into its NORMAL answers (dept resolves to help without only_department).
    Both writes of the department column refuse."""
    me = client.get("/api/auth/me", headers=admin_headers).json()["id"]
    r = client.patch(f"/api/users/{me}/department", json={"department": "help"},
                     headers=admin_headers)
    assert r.status_code == 400 and "reserved" in r.json()["detail"]
    r = client.post("/api/users", json={"username": "helpdesk-user", "password": "HelpDesk1x!!",
                                        "role": "member", "department": "Help",
                                        "current_password": "AdminPass1"},
                    headers=admin_headers)
    assert r.status_code == 400 and "reserved" in r.json()["detail"]


def test_help_page_endpoint_is_gated_like_chat(client):
    with patch("app.routers.chat.guest_chat_available", return_value=False):
        assert client.get("/api/help/page",
                          params={"name": "help/getting-started.md"}).status_code == 403
    with patch("app.routers.chat.guest_chat_available", return_value=True):
        r = client.get("/api/help/page", params={"name": "help/getting-started.md"})
        assert r.status_code == 200 and r.json()["content"].startswith("# ")
        assert client.get("/api/help/page",
                          params={"name": "help/../main.py"}).status_code == 404
    with patch("app.routers.chat.HELP_DOCS_SYNC", False), \
         patch("app.routers.chat.guest_chat_available", return_value=True):
        assert client.get("/api/help/page",
                          params={"name": "help/getting-started.md"}).status_code == 404


def test_help_page_route_is_exempt_from_the_auth_middleware():
    """On an ENABLE_AUTH=true instance the middleware 401s every bearer-less
    request that is not listed - a guest could chat but never open a
    citation. The handler's own gate stays the control."""
    from app.auth import EXCLUDED_PATHS
    assert "/api/help/page" in EXCLUDED_PATHS and "/api/chat" in EXCLUDED_PATHS


def test_the_public_config_advertises_the_lane(client):
    assert client.get("/api/auth/config").json()["help_enabled"] is True
    with patch("app.runtime_config.HELP_DOCS_SYNC", False):
        assert client.get("/api/auth/config").json()["help_enabled"] is False
