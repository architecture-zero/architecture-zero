"""A JSON body is read only when the request says it is JSON (2026-11-22).

FastAPI checks the Content-Type of a JSON request by default since 0.132: a
body that arrives with no Content-Type at all is no longer parsed as JSON,
so the route refuses it with a 422 before its code runs. Until fastapi
0.162.7 this app parsed such a body anyway. The check matters because a
browser sends a cross-site form post as a "simple request" with no CORS
preflight, and a body it can be made to send without naming application/json
should never reach a JSON route as JSON.

Pinned here so that switching the check off (strict_content_type=False on the
app, a router or a route) fails a test instead of passing quietly.
"""
import json

BODY = json.dumps({"username": "ctype_probe_nobody", "password": "not-the-password"})


def test_a_json_body_with_no_content_type_is_refused_before_the_route_runs(client):
    answer = client.post("/api/auth/login", content=BODY)
    assert "content-type" not in {k.lower() for k in answer.request.headers}
    assert answer.status_code == 422, answer.text


def test_a_json_body_sent_as_plain_text_is_refused(client):
    answer = client.post("/api/auth/login", content=BODY,
                         headers={"Content-Type": "text/plain"})
    assert answer.status_code == 422, answer.text


def test_the_same_body_named_as_json_reaches_the_route(client):
    """The control: the identical bytes, declared as JSON, get past parsing
    and are refused by the route itself (no such account), not by the
    parser."""
    answer = client.post("/api/auth/login", content=BODY,
                         headers={"Content-Type": "application/json"})
    assert answer.status_code == 401, answer.text
