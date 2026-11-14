"""The account doors refuse a department the vector store can never hold.

An account's department is where its writes go, and a department is created
by its first write, as the collection database._collection_name names. The
store refuses a name longer than 60 characters or one that does not end in a
letter or digit, and the knowledge-base write doors already answer that with
database.department_name_refusal's sentence. The two account doors - the
create door (POST /api/users) and the department change (PATCH
/api/users/{id}/department) - took the name as sent, so an account could be
created in, or moved into, "sales-" or a 61-character department, and every
later write for it was refused.

FIX: both doors answer the write doors' sentence as a 400, and nothing is
written. These cases fail on the code before this change.
CONTROL: the longest name the store takes, and "general", are written as
before. These pass on both.
"""
import pytest

from app.users import get_user_by_id_any_status, get_user_by_username

_OWNER_PASSWORD = "AdminPass1"   # conftest's first account
REFUSED = ["sales-", "Sales Team!", "x" * 61]
TAKEN = ["x" * 60, "general"]


def _create(client, headers, username, department):
    return client.post("/api/users", headers=headers, json={
        "current_password": _OWNER_PASSWORD, "username": username,
        "password": "Created1x", "department": department})


@pytest.fixture
def target(client, admin_headers):
    """An account to move, back in general before every test."""
    _create(client, admin_headers, "deptshape-target", "general")
    user = get_user_by_username("deptshape-target")
    assert user is not None
    client.patch(f"/api/users/{user['id']}/department", headers=admin_headers,
                 json={"department": "general"})
    return user["id"]


def _department(user_id):
    return get_user_by_id_any_status(user_id)["department"]


# -- FIX ---------------------------------------------------------------------------

@pytest.mark.parametrize("i,name", list(enumerate(REFUSED)))
def test_fix_the_create_door_refuses_a_name_the_store_cannot_hold(client, admin_headers, i, name):
    username = f"deptshape-refused-{i}"
    r = _create(client, admin_headers, username, name)
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("Department name ")
    assert name not in r.text
    assert get_user_by_username(username) is None, "the account was written"


@pytest.mark.parametrize("name", REFUSED)
def test_fix_the_department_change_refuses_a_name_the_store_cannot_hold(
        client, admin_headers, target, name):
    r = client.patch(f"/api/users/{target}/department", headers=admin_headers,
                     json={"department": name})
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("Department name ")
    assert name not in r.text
    assert _department(target) == "general", "the department was written"


def test_fix_each_door_answers_the_write_doors_sentence(client, admin_headers, target):
    from app.database import department_name_refusal
    for name in REFUSED:
        r = _create(client, admin_headers, "deptshape-sentence", name)
        assert r.json()["detail"] == department_name_refusal(name)
        r = client.patch(f"/api/users/{target}/department", headers=admin_headers,
                         json={"department": name})
        assert r.json()["detail"] == department_name_refusal(name)


# -- CONTROL -------------------------------------------------------------------------

@pytest.mark.parametrize("i,name", list(enumerate(TAKEN)))
def test_control_the_create_door_takes_a_name_the_store_holds(client, admin_headers, i, name):
    username = f"deptshape-taken-{i}"
    r = _create(client, admin_headers, username, name)
    assert r.status_code == 200, r.text
    assert get_user_by_username(username)["department"] == name


@pytest.mark.parametrize("name", TAKEN)
def test_control_the_department_change_takes_a_name_the_store_holds(
        client, admin_headers, target, name):
    r = client.patch(f"/api/users/{target}/department", headers=admin_headers,
                     json={"department": name})
    assert r.status_code == 200, r.text
    assert _department(target) == name
