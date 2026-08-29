"""Security hardening regressions: MFA token type confusion, the JWT-secret
boot guard, proxy-aware client IP resolution, and WAL-safe backups."""

import importlib
import pytest


def test_mfa_challenge_token_rejected_as_access(client, admin_headers):
    from app.jwt_auth import create_mfa_challenge_token, decode_access_token
    from fastapi import HTTPException
    mfa_tok = create_mfa_challenge_token(1)
    with pytest.raises(HTTPException) as exc:
        decode_access_token(mfa_tok)
    assert exc.value.status_code == 401
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {mfa_tok}"})
    assert r.status_code == 401


def test_boot_guard_refuses_default_secret(monkeypatch):
    import app.auth
    monkeypatch.setenv("ENABLE_AUTH", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "change-me-before-deploying")
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(app.auth)
    finally:
        monkeypatch.setenv("ENABLE_AUTH", "false")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
        importlib.reload(app.auth)


def test_client_ip_from_request_behind_proxy():
    """Real client IP behind the reverse proxy - the X-Real-IP header is
    trusted only when the socket peer IS the proxy (private/loopback). A
    direct hit on the published port must not be able to spoof its IP."""
    from app.security import client_ip_from_request

    class _Req:
        def __init__(self, peer, headers=None):
            self.client = type("C", (), {"host": peer})() if peer else None
            self.headers = headers or {}

    # behind the proxy (docker-private peer): proxy-set X-Real-IP wins
    assert client_ip_from_request(_Req("172.18.0.5", {"x-real-ip": "203.0.113.9"})) == "203.0.113.9"
    # direct hit on the published port (public peer): spoofed header ignored
    assert client_ip_from_request(_Req("8.8.8.8", {"x-real-ip": "1.2.3.4"})) == "8.8.8.8"
    # no header: socket peer
    assert client_ip_from_request(_Req("172.18.0.5")) == "172.18.0.5"
    # no client at all: unknown
    assert client_ip_from_request(_Req(None, {"x-real-ip": "1.2.3.4"})) == "unknown"


# -- JWT-secret boot guard stays wired ----------------------------------------
# With auth ON but a missing/placeholder secret, every JWT is signed with a
# world-known key and anyone can forge an admin token. The guard must keep
# failing closed at import time.

def test_jwt_secret_guard_still_present():
    """The boot guard must be UNCONDITIONAL: route-level dependencies
    validate JWTs whether or not the middleware layer is on, so a
    placeholder secret is forgeable-tokens territory in every posture. A
    guard gated on ENABLE_AUTH is the regression this test exists to
    block."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "app" / "auth.py"
    text = src.read_text(encoding="utf-8")
    assert 'if SECRET_KEY in ("", "change-me-before-deploying")' in text
    assert 'if ENABLE_AUTH and SECRET_KEY' not in text


# -- Backup consistency under WAL ---------------------------------------------

def test_backup_uses_the_sqlite_api_not_loose_wal_file_copies(tmp_path):
    """db + -wal + -shm copied as three separate files at three instants can
    restore inconsistent or drop the WAL tail. The backup path must take a
    real snapshot and skip the sidecars."""
    from pathlib import Path
    # The backup handler moved to the admin router. This is the only by-PATH
    # source assertion in the suite, so it does not follow a name - it has to
    # be repointed by hand, and its failure message talks about WAL sidecars
    # rather than about routing.
    src = Path(__file__).resolve().parents[1] / "app" / "routers" / "admin.py"
    text = src.read_text(encoding="utf-8")
    assert 'item.endswith(("-wal", "-shm"))' in text, "sidecars must be skipped"
    assert "src_conn.backup(dst_conn)" in text, "must use the sqlite backup API"


def test_sqlite_backup_snapshot_is_consistent_with_live_writers(tmp_path):
    """Behavioral proof of the mechanism the fix relies on: a WAL database
    with uncommitted-to-main-file rows still backs up complete."""
    import sqlite3
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(f"row{i}",) for i in range(50)])
    con.commit()
    out = tmp_path / "backup.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    con.close()
    check = sqlite3.connect(out)
    assert check.execute("SELECT count(*) FROM t").fetchone()[0] == 50
    check.close()


def test_admin_config_never_returns_a_provider_credential(client, admin_headers):
    """Provider keys live in the same config table as ordinary settings, and
    /api/admin/config used to return that table whole.

    Its guard is manage_system - a permission an Owner can GRANT, and one whose
    stated job is "edit system prompt, instance config, model settings". So a
    non-Owner could read every provider credential in cleartext from a surface
    one tier BELOW the Owner-only settings page that masks the same values.

    Pinned on both verbs, because the PATCH handler returns the config too - a
    write leaked exactly as much as a read.
    """
    from app.config import set_config

    set_config("anthropic_api_key", "sk-ant-not-a-real-key-000")
    try:
        for resp in (client.get("/api/admin/config", headers=admin_headers),
                     client.patch("/api/admin/config", json={"instance_name": "Acme"},
                                  headers=admin_headers)):
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "anthropic_api_key" not in body, "the raw credential is in the response"
            assert "not-a-real-key" not in resp.text, "the credential leaked by value"
            # Reported as presence, the way the Owner-only settings surface does.
            assert body.get("anthropic_key_set") is True
    finally:
        set_config("anthropic_api_key", "")


def test_the_masked_config_still_carries_the_ordinary_settings():
    """Two-sided: masking must not swallow the rows the panel actually edits."""
    from app.config import get_all_config_masked, set_config

    set_config("instance_name", "Acme Corp")
    body = get_all_config_masked()
    assert body["instance_name"] == "Acme Corp"
    assert "system_prompt" in body


# -- METRICS_TOKEN: the scrape credential, and the guard around it ------------
#
# /metrics is authenticated on purpose - it reports request volumes and error
# counts. But the only credential the platform issued was a 30-minute access
# token, so the Monitoring tab's "download scrape config" button produced a file
# that could never work: a scrape 401s forever, or dies half an hour after an
# operator pastes a token in by hand. METRICS_TOKEN is that missing credential.
# These pin that adding it did not open anything that was closed before.

def test_metrics_still_requires_auth_when_no_token_is_set(client, monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert client.get("/metrics").status_code == 401
    # An unset token must not turn any bearer into a valid scraper.
    assert client.get("/metrics", headers={"Authorization": "Bearer anything"}
                      ).status_code == 401


def test_metrics_token_opens_only_metrics(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret-value")
    r = client.get("/metrics", headers={"Authorization": "Bearer scrape-secret-value"})
    assert r.status_code == 200
    assert "az_chat_requests_total" in r.text
    # The scrape credential is for THIS endpoint. It must not authenticate
    # anything else - a metrics secret that is really a session would be a far
    # larger grant than the feature asks for.
    for path in ("/api/users", "/api/admin/config", "/api/sessions", "/api/auth/me"):
        assert client.get(path, headers={"Authorization": "Bearer scrape-secret-value"}
                          ).status_code in (401, 403), f"{path} accepted the metrics token"


def test_a_wrong_or_blank_metrics_token_is_refused(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret-value")
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}
                      ).status_code == 401
    # A blank/whitespace value in .env must read as UNSET, never as "any empty
    # bearer matches" - that would publish the endpoint to the internet.
    monkeypatch.setenv("METRICS_TOKEN", "   ")
    assert client.get("/metrics", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer    "}).status_code == 401
