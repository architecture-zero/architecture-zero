"""/api/backup-status - the backup/DR staleness probe.

The endpoint must fail LOUD (503) on every silent-failure shape: missing
status files, a job that never succeeded, a stale success, or a fresh-success-
but-latest-run-failed. 200 only when BOTH backup and drill are fresh and ok.
"""
import datetime as dt
import json

import pytest

# The status_dir fixture patches BACKUP_STATUS_DIR, and _backup_job_state
# resolves it from its OWN module globals at call time - so the patch has to land
# on the module that DEFINES it, which is now the system router. Deliberately no
# compatibility re-export from main: with one, the setattr would succeed, patch a
# name nobody reads, and these eight tests would quietly start reading the real
# /app/data - with test_missing_files_503 passing for entirely the wrong reason.
import app.routers.system as main_mod


def _stamp(hours_ago: float) -> str:
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    return ts.strftime("%Y-%m-%dT%H%M%SZ")


def _write(dirpath, fname, ok=True, hours_ago=1.0, last_success_hours_ago=None):
    payload = {
        "ok": ok,
        "last_run": _stamp(hours_ago),
        "last_success": _stamp(
            hours_ago if last_success_hours_ago is None else last_success_hours_ago
        ),
    }
    (dirpath / fname).write_text(json.dumps(payload))


@pytest.fixture
def status_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "BACKUP_STATUS_DIR", str(tmp_path))
    return tmp_path


def test_both_fresh_ok(client, status_dir):
    _write(status_dir, "backup-status.json")
    _write(status_dir, "drill-status.json")
    r = client.get("/api/backup-status")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["backup"]["ok"] is True
    assert body["drill"]["ok"] is True


def test_missing_files_503(client, status_dir):
    r = client.get("/api/backup-status")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert "missing" in body["backup"]["reason"]


def test_stale_backup_503(client, status_dir):
    _write(status_dir, "backup-status.json", hours_ago=40)
    _write(status_dir, "drill-status.json")
    r = client.get("/api/backup-status")
    assert r.status_code == 503
    assert "stale" in r.json()["backup"]["reason"]


def test_recent_failure_503_despite_fresh_success(client, status_dir):
    # last night succeeded, tonight's run FAILED - must alarm now, not in 30h
    _write(status_dir, "backup-status.json", ok=False, last_success_hours_ago=20)
    _write(status_dir, "drill-status.json")
    r = client.get("/api/backup-status")
    assert r.status_code == 503
    assert r.json()["backup"]["reason"] == "last run failed"


def test_missing_drill_503(client, status_dir):
    # a drill that never runs is a silent gap - strict by design
    _write(status_dir, "backup-status.json")
    r = client.get("/api/backup-status")
    assert r.status_code == 503
    assert r.json()["drill"]["ok"] is False


def test_never_succeeded_503(client, status_dir):
    (status_dir / "backup-status.json").write_text(json.dumps({"ok": False, "last_run": _stamp(1)}))
    _write(status_dir, "drill-status.json")
    r = client.get("/api/backup-status")
    assert r.status_code == 503
    assert r.json()["backup"]["reason"] == "never succeeded"


def test_unparseable_timestamp_503(client, status_dir):
    (status_dir / "backup-status.json").write_text(
        json.dumps({"ok": True, "last_success": "not-a-date"})
    )
    _write(status_dir, "drill-status.json")
    r = client.get("/api/backup-status")
    assert r.status_code == 503
    assert r.json()["backup"]["reason"] == "unparseable last_success"


def test_unauthenticated_when_auth_on(client, status_dir, monkeypatch):
    # the external uptime prober has no JWT: the path must be excluded from
    # AuthMiddleware
    import app.auth as auth_mod

    _write(status_dir, "backup-status.json")
    _write(status_dir, "drill-status.json")
    monkeypatch.setattr(auth_mod, "ENABLE_AUTH", True)
    r = client.get("/api/backup-status")
    assert r.status_code == 200
