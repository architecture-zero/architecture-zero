def test_setup_blocked_when_admin_exists(client):
    r = client.post("/api/auth/setup", json={"username": "other", "password": "OtherPass1"})
    assert r.status_code == 403


def test_login_valid(client):
    r = client.post("/api/auth/login", json={"username": "testadmin", "password": "AdminPass1"})
    assert r.status_code == 200
    data = r.json()
    assert "access_token" in data
    assert "refresh_token" in data


def test_login_wrong_password(client):
    r = client.post("/api/auth/login", json={"username": "testadmin", "password": "wrongpassword"})
    assert r.status_code == 401


def test_login_unknown_user(client):
    r = client.post("/api/auth/login", json={"username": "nobody", "password": "AdminPass1"})
    assert r.status_code == 401


def test_refresh_token(client):
    login = client.post("/api/auth/login", json={"username": "testadmin", "password": "AdminPass1"})
    assert login.status_code == 200
    # Refresh token is passed as a Bearer header, matching the endpoint contract.
    r = client.post(
        "/api/auth/refresh",
        headers={"Authorization": f"Bearer {login.json()['refresh_token']}"},
    )
    assert r.status_code == 200
    assert "access_token" in r.json()
