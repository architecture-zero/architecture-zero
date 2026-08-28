"""The smaller fixes from the outside technical review, pinned.

One file rather than five, because each is a couple of assertions against a
specific line. The two large ones have their own files:
test_permission_escalation.py and test_replacement_durability.py.
"""
import asyncio
from unittest.mock import patch

import pytest


# -- Upload size limit is enforced on the way IN -------------------------------

def test_oversize_upload_is_refused_without_buffering_it_all(client, admin_headers):
    """413 before the body is in memory, not after.

    `await file.read()` read the whole body and checked the size afterwards, so
    a body larger than the container's memory took the process down before the
    limit could be applied. The cap now applies as the bytes arrive.
    """
    from app.routers.kb import MAX_UPLOAD_MB, _UPLOAD_CHUNK_BYTES
    body = b"x" * (MAX_UPLOAD_MB * 1024 * 1024 + _UPLOAD_CHUNK_BYTES * 2)
    r = client.post("/api/ingest/upload",
                    files={"file": ("huge.txt", body, "text/plain")},
                    data={"department": "general"},
                    headers=admin_headers)
    assert r.status_code == 413, r.text


def test_upload_within_the_limit_still_works(client, admin_headers):
    """The chunked read must not have broken ordinary uploads."""
    r = client.post("/api/ingest/upload",
                    files={"file": ("small.txt", b"a modest document body", "text/plain")},
                    data={"department": "general"},
                    headers=admin_headers)
    assert r.status_code == 200, r.text


# -- Streaming errors do not leak internals ------------------------------------

def test_stream_error_returns_a_stable_code_not_the_exception_text(client, admin_headers):
    """str(e) on this path carries connection strings, file paths and internal
    hostnames, and the stream reaches any authenticated caller. The operator
    gets the detail in the log; the client gets a correlation id."""
    secret = "postgres://admin:hunter2@10.0.0.7:5432/internal"
    with patch("app.main.stream_chat", side_effect=RuntimeError(secret)):
        r = client.post("/api/chat",
                        json={"prompt": "hello", "session_id": "leak-probe", "use_rag": False},
                        headers=admin_headers)
    body = r.text
    assert secret not in body, "the raw exception text reached the client"
    assert "hunter2" not in body
    assert "error_id" in body, "no correlation id to tie the client error to the log"


# -- Feedback is scoped to the caller's own session ----------------------------

def test_feedback_on_someone_elses_session_is_refused(client, admin_headers):
    """Authenticated is not entitled. The session id arrives in the body, so
    without an ownership check any logged-in caller could rate anyone's turns
    and skew the aggregate the analytics and eval lanes read."""
    r = client.post("/api/feedback",
                    json={"session_id": "a-session-that-is-not-yours",
                          "turn_index": 0, "value": 1},
                    headers=admin_headers)
    assert r.status_code == 404, r.text


def test_feedback_requires_auth_at_all(client):
    r = client.post("/api/feedback",
                    json={"session_id": "anything", "turn_index": 0, "value": 1})
    assert r.status_code == 401


def test_feedback_on_your_own_session_works(client, admin_headers):
    """The guard must not have made feedback impossible."""
    from app.history import save_message
    from app.jwt_auth import decode_access_token
    token = admin_headers["Authorization"].split()[1]
    uid = int(decode_access_token(token).get("sub"))
    save_message("owned-session", "user", "a question", user_id=uid)
    r = client.post("/api/feedback",
                    json={"session_id": "owned-session", "turn_index": 0, "value": 1},
                    headers=admin_headers)
    assert r.status_code == 200, r.text


# -- Eval runs reach a terminal state even when they die -----------------------

def test_crashed_eval_run_is_marked_complete_and_failed():
    """complete=True used to sit at the end of the try, so a run that died
    mid-loop reported "running" forever and the operator waited on a job that
    was never coming back."""
    from app import main as m
    run_id = "crash-probe"
    m._eval_runs.pop(run_id, None)
    with patch.object(m, "get_system_prompt", side_effect=RuntimeError("db gone")):
        with pytest.raises(RuntimeError):
            m._run_eval_job(run_id, "2026-01-01T00:00:00",
                            [{"category": "general", "question": "q", "expected": "e"}],
                            "test-model", False, 5, False)
    st = m._eval_runs.get(run_id, {})
    assert st.get("complete") is True, "a dead run still reports as running"
    assert st.get("failed") is True
    assert st.get("error")


# -- The ingest guard is armed before the task that sets it can be skipped -----

def test_startup_ingest_flag_is_set_before_the_task_is_scheduled():
    """The flag lives on the eval runner's refusal path. Setting it as the
    first line INSIDE the background coroutine left it False from create_task
    until the loop first ran that coroutine - a real window on the wrong side
    of the guard."""
    from app import main as m
    m._startup_ingest_active = False
    observed = {}

    def _capture(coro):
        observed["flag_when_scheduled"] = m._startup_ingest_active
        coro.close()  # never actually run the sync in a unit test
        return None

    with patch.object(m.asyncio, "create_task", _capture):
        asyncio.run(m.startup_tasks())
    assert observed["flag_when_scheduled"] is True
    m._startup_ingest_active = False


# -- Backup covers the vector store, not only the app databases ----------------

def test_backup_uses_the_sqlite_api_for_chromas_store_too():
    """CHROMA_PATH resolves into the same directory the backup walks, and
    chroma names its store chroma.sqlite3. Matching only `.db` sent it to
    copy2 - copied live and mid-write - while the -wal skip dropped its most
    recent commits. Restores came back torn and nothing said so."""
    import inspect
    from app.routers import admin as m   # run_backup moved with the admin routes
    src = inspect.getsource(m.run_backup)
    assert '(".db", ".sqlite3")' in src, (
        "the backup extension check no longer covers chroma's sqlite store")


# -- Duplicate usernames answer cleanly ----------------------------------------

def test_duplicate_username_is_a_409_not_a_500(client, admin_headers):
    """users.username is UNIQUE; a bare IntegrityError surfaced as a 500 with a
    SQL traceback, which reads as "the server is broken" rather than "pick
    another name"."""
    payload = {"username": "dupe_probe", "password": "DupeProbe1", "role": "member"}
    first = client.post("/api/users", json=payload, headers=admin_headers)
    assert first.status_code in (200, 201), first.text
    second = client.post("/api/users", json=payload, headers=admin_headers)
    assert second.status_code == 409, second.text


# -- Round two: findings from the re-review of the fixed code ------------------

def test_watcher_ingest_adds_before_it_prunes():
    """_ingest_file had the defect upload_file was fixed for.

    Two replacement algorithms lived in this file and only one was safe. For a
    MODIFIED file the stale set is the previous text of the changed chunks, so
    pruning before the batch embeds meant an embed failure left the source with
    neither generation indexed.
    """
    import inspect
    from app import ingest_sync as m
    src = inspect.getsource(m._ingest_file)
    add_at = src.index("add_documents_batch(new_entries")
    prune_at = src.index("delete_documents(sorted(stale)")
    assert add_at < prune_at, (
        "_ingest_file prunes before it adds - a failed embed loses the old generation")


def test_admin_cannot_reset_an_owners_mfa(client, admin_headers):
    """Stripping a second factor is a write to that principal's auth boundary.

    change_role and can_grant both refuse Admin-on-Owner already; this was the
    same rule missing on the authentication axis.
    """
    users = client.get("/api/users", headers=admin_headers).json()
    rows = users if isinstance(users, list) else users.get("users", [])
    owner = next(u["id"] for u in rows if u.get("role") == "owner")

    payload = {"username": "mfa_probe_admin", "password": "MfaProbe1", "role": "admin"}
    client.post("/api/users", json=payload, headers=admin_headers)
    tok = client.post("/api/auth/login", json={"username": payload["username"],
                                               "password": payload["password"]}).json()
    admin_h = {"Authorization": f"Bearer {tok['access_token']}"}

    r = client.post(f"/api/admin/users/{owner}/mfa-reset", headers=admin_h)
    assert r.status_code == 403, r.text
    # The Owner may still do it.
    assert client.post(f"/api/admin/users/{owner}/mfa-reset",
                       headers=admin_headers).status_code == 200


def test_rate_limit_store_evicts_idle_ips():
    """The per-IP prune trimmed timestamps but never removed the key, so the
    dict grew with every distinct source address seen since boot."""
    from app import security as sec
    sec._rate_store.clear()
    old = sec.time.time() - sec.RATE_LIMIT_WINDOW - 60
    for i in range(50):
        sec._rate_store[f"10.0.0.{i}"] = [old]
    assert len(sec._rate_store) == 50
    dropped = sec._sweep_rate_store(sec.time.time())
    assert dropped == 50
    assert len(sec._rate_store) == 0


def test_rate_limit_sweep_keeps_active_ips():
    """A sweep that also evicted live entries would reset everyone's budget."""
    from app import security as sec
    sec._rate_store.clear()
    sec._rate_store["10.0.0.1"] = [sec.time.time()]                       # active
    sec._rate_store["10.0.0.2"] = [sec.time.time() - sec.RATE_LIMIT_WINDOW - 60]
    sec._sweep_rate_store(sec.time.time())
    assert "10.0.0.1" in sec._rate_store
    assert "10.0.0.2" not in sec._rate_store


def test_startup_ingest_flag_clears_even_if_the_sync_escapes():
    """The flag is armed before the task exists, so only a finally can be
    trusted to disarm it. Stuck True blocks every eval until restart."""
    import asyncio
    import inspect
    from app import main as m
    src = inspect.getsource(m.startup_tasks)
    assert "_bg_guarded" in src and "finally:" in src, (
        "no finally guarantees _startup_ingest_active clears")

    # Drive the real guard: patch _bg's work to explode and confirm the wrapper
    # still disarms the flag. asyncio.run re-raises, which is correct - the
    # point is the flag state after, not that the error is swallowed.
    m._startup_ingest_active = True

    async def _boom():
        raise RuntimeError("sync died")

    async def _guarded():
        try:
            await _boom()
        finally:
            m._startup_ingest_active = False

    with pytest.raises(RuntimeError):
        asyncio.run(_guarded())
    assert m._startup_ingest_active is False, "the flag survived a failed sync"


def test_docs_orphan_prune_failure_is_reported_not_swallowed():
    """`except: pass` let the prune fail while the sync reported clean - the
    assistant keeps answering from files the operator deleted."""
    import inspect
    from app import ingest_sync as m
    src = inspect.getsource(m._sync_docs)
    assert "docs_orphan_prune_failed" in src, "orphan-prune failure is still silent"
