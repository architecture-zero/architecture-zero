"""A failed alert webhook logs the error's type, never its text (the AZ-02
security read, 2026-10-07).

A Slack- or Discord-style webhook URL carries its credential in the path, and
on a connection failure urllib3's message names that path - so the old
`error=str(e)` wrote the credential into the log, wherever the log goes.
Fails on the code before the fix.
"""
import requests

from app import alerting

_PATH = "/services/example-team/example-channel/example-path-credential"


def test_a_failed_webhook_logs_the_type_not_the_url(monkeypatch):
    monkeypatch.setattr(alerting, "_WEBHOOK_URL", "https://hooks.example.test" + _PATH)

    def _refused(url, **kwargs):
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='hooks.example.test', port=443): Max retries "
            f"exceeded with url: {_PATH} (Caused by NewConnectionError: refused)")
    monkeypatch.setattr(alerting._req, "post", _refused)
    logged = []
    monkeypatch.setattr(alerting, "log_error", lambda event, **kw: logged.append((event, kw)))
    alerting._webhook("title", "body")
    assert [e for e, _ in logged] == ["alert_webhook_failed"]
    assert "example-path-credential" not in repr(logged), logged
    assert logged[0][1]["error"] == "ConnectionError"
