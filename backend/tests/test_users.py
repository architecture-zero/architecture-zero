def test_list_users_requires_token(client):
    r = client.get("/api/users")
    assert r.status_code == 401


def test_list_users_with_admin(client, admin_headers):
    r = client.get("/api/users", headers=admin_headers)
    assert r.status_code == 200
    assert "users" in r.json()


def test_create_user(client, admin_headers):
    r = client.post(
        "/api/users",
        headers=admin_headers,
        json={"username": "newuser1", "password": "NewUser1pass", "role": "member"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "created"


def test_create_user_invalid_role(client, admin_headers):
    r = client.post(
        "/api/users",
        headers=admin_headers,
        json={"username": "baduser", "password": "BadUser1pass", "role": "superuser"},
    )
    assert r.status_code == 400
