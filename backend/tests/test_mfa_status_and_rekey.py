"""The MFA card's blind spot, pinned server-side (2026-09-05, ported from the 2026-09-04 upstream fix).

The admin card assumed "not enrolled" on every mount and offered "Set up
authenticator" to enrolled accounts; one tap called /api/auth/mfa/setup, which
replaced the secret AND flipped mfa_enabled off - silent de-enrollment behind
a button that looked like status. Found by the operator being confused at his
own page while his row read mfa_enabled=1. Two server-side facts now hold:

  1. /api/auth/me reports mfa_enabled, so a client can render the truth.
  2. /api/auth/mfa/setup refuses an enabled account unless rekey is explicit,
     and the refusal leaves the stored secret untouched.
"""
import pyotp
import pytest

_USER = {"username": "mfa_state_probe", "password": "MfaState#2026x"}


@pytest.fixture
def probe(client, admin_headers):
    """A logged-in probe user whose MFA state each test sets directly."""
    from app.users import get_user_by_username
    from app.db import get_session
    from app.models import User

    existing = get_user_by_username(_USER["username"])
    if not existing:
        # Role names differ between builds (see test_mfa_challenge_guard.py's
        # fixture, which this mirrors) - ask for whichever the instance takes.
        for role in ("user", "member"):
            r = client.post("/api/users", json={**_USER, "role": role},
                            headers=admin_headers)
            if r.status_code in (200, 201):
                break
        assert r.status_code in (200, 201), f"no accepted role: {r.text}"
        existing = get_user_by_username(_USER["username"])

    # Log in BEFORE any test enables MFA, so the password path hands tokens
    # straight back; the access token stays valid across the state flips.
    r = client.post("/api/auth/login", json=_USER)
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    yield {"id": existing["id"], "headers": headers}

    with get_session() as db:
        db.query(User).filter(User.id == existing["id"]).update(
            {"mfa_enabled": False, "mfa_secret": None})


def _force_mfa(user_id, enabled, secret):
    from app.db import get_session
    from app.models import User
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update(
            {"mfa_enabled": enabled, "mfa_secret": secret})


def _stored_secret(user_id):
    from app.users import get_user_by_id
    return get_user_by_id(user_id)["mfa_secret"]


# -- 1. /api/auth/me tells the truth about enrollment -------------------------

def test_me_reports_mfa_state(client, probe):
    r = client.get("/api/auth/me", headers=probe["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["mfa_enabled"] is False

    _force_mfa(probe["id"], True, pyotp.random_base32())
    r = client.get("/api/auth/me", headers=probe["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["mfa_enabled"] is True


# -- 2. A bare setup cannot de-enroll an enabled account ----------------------

def test_setup_refused_while_enabled_and_secret_survives(client, probe):
    secret = pyotp.random_base32()
    _force_mfa(probe["id"], True, secret)

    # No body at all - the pre-fix client's exact call shape.
    r = client.post("/api/auth/mfa/setup", headers=probe["headers"])
    assert r.status_code == 409, r.text
    assert "rekey" in r.json()["detail"]
    assert _stored_secret(probe["id"]) == secret  # refusal touched nothing

    # rekey explicitly false is refused the same way.
    r = client.post("/api/auth/mfa/setup", json={"rekey": False},
                    headers=probe["headers"])
    assert r.status_code == 409, r.text
    assert _stored_secret(probe["id"]) == secret


def test_setup_rekeys_only_with_explicit_flag(client, probe):
    from app.users import get_user_by_id
    secret = pyotp.random_base32()
    _force_mfa(probe["id"], True, secret)

    r = client.post("/api/auth/mfa/setup", json={"rekey": True},
                    headers=probe["headers"])
    assert r.status_code == 200, r.text
    row = get_user_by_id(probe["id"])
    assert row["mfa_secret"] != secret       # re-keyed
    assert row["mfa_enabled"] is False       # disabled until the new verify


def test_setup_plain_for_unenrolled_account(client, probe):
    _force_mfa(probe["id"], False, None)
    r = client.post("/api/auth/mfa/setup", headers=probe["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["qr"].startswith("data:image/png;base64,")
