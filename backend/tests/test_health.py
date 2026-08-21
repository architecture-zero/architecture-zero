def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200


def test_config_has_instance_name(client, admin_headers):
    # authed since the route-level auth pass - /api/config was already
    # middleware-gated in prod, so route auth mirrors real behavior
    r = client.get("/api/config", headers=admin_headers)
    assert r.status_code == 200
    assert "instance_name" in r.json()


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
