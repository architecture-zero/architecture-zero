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
            r = client.post("/api/users", json={"current_password": "AdminPass1", **_USER, "role": role},
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


# -- 3. T9: a seed this instance cannot read FAILS CLOSED at every mint --------
# Fleet port 2026-09-10 of the upstream 2026-09-06 fix (torture test T9). A
# seed encrypted under a DIFFERENT key is exactly the row a restore under a
# rotated JWT_SECRET_KEY leaves behind. Before this port the read seam raised
# and every request for such an account was HTTP 500; the danger in fixing
# that is reading "unreadable" as "not enrolled" and letting the password in
# alone. Every door that mints a session must refuse, with the remedy named.

_STRANDED_DETAIL_MUST_NAME = ("rekey_at_rest.py", "mfa-reset")


def _strand(user_id, enabled=True):
    """Write ciphertext this instance cannot open. A raw column write on
    purpose: set_mfa_secret would encrypt under the CURRENT key, which is the
    one case this is not."""
    from cryptography.fernet import Fernet
    from app.db import get_session
    from app.models import User
    raw = Fernet(Fernet.generate_key()).encrypt(pyotp.random_base32().encode()).decode()
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update(
            {"mfa_enabled": enabled, "mfa_secret": raw})
    return raw


@pytest.fixture
def stranded_events(monkeypatch):
    """Every auth_mfa_seed_unreadable log line the app emits during the test.
    This surface has no security_events table, so the log line IS the durable
    record; the helper imports `log` at call time, so patching the source
    module is enough."""
    import app.logger as logger_mod
    seen = []
    real = logger_mod.log

    def spy(event, **kw):
        if event == "auth_mfa_seed_unreadable":
            seen.append(kw)
        return real(event, **kw)
    monkeypatch.setattr(logger_mod, "log", spy)
    return seen


def test_stranded_seed_reads_as_unreadable_not_as_unenrolled(client, probe):
    from app.users import get_user_by_id
    _strand(probe["id"])
    row = get_user_by_id(probe["id"])
    assert row["mfa_enabled"] is True
    assert row["mfa_secret"] is None, "an unreadable seed must never hand back a value"
    assert row["mfa_secret_unreadable"] is True, \
        "the PAIR is the signal - without the flag this reads as never enrolled"
    # /me reports it, so a client can render the truth instead of "set up".
    r = client.get("/api/auth/me", headers=probe["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["mfa_enabled"] is True and r.json()["mfa_secret_unreadable"] is True


def test_unfinished_enrollment_with_unreadable_seed_is_not_stranded(client, probe):
    """mfa_enabled=False + unreadable = enrollment never finished; the seed was
    never a factor, setup overwrites it under the current key, and login stays
    password-only. Pins the predicate's polarity."""
    _strand(probe["id"], enabled=False)
    r = client.post("/api/auth/login", json=_USER)
    assert r.status_code == 200 and "access_token" in r.json(), r.text
    r = client.post("/api/auth/mfa/setup", headers=probe["headers"])
    assert r.status_code == 200, r.text            # self-heals


def test_stranded_seed_refuses_login_and_records_it(client, probe, stranded_events):
    _strand(probe["id"])
    r = client.post("/api/auth/login", json=_USER)
    assert r.status_code == 403, r.text
    body = r.json()
    assert "mfa_token" not in body and "access_token" not in body
    for remedy in _STRANDED_DETAIL_MUST_NAME:
        assert remedy in body["detail"], f"the 403 must name the remedy {remedy}"
    assert [e["stage"] for e in stranded_events] == ["login"]


def test_stranded_seed_refuses_refresh_and_kills_the_family(client, probe, stranded_events):
    """A refresh needs neither password nor second factor, so a token minted
    BEFORE the restore would keep the account alive indefinitely on an
    instance that can no longer verify its second factor. 403 AND every
    token revoked."""
    from app.db import get_session
    from app.models import RefreshToken
    r0 = client.post("/api/auth/login", json=_USER)
    assert r0.status_code == 200, r0.text
    pre_restore_refresh = r0.json()["refresh_token"]
    _strand(probe["id"])
    r = client.post("/api/auth/refresh",
                    headers={"Authorization": f"Bearer {pre_restore_refresh}"})
    assert r.status_code == 403, r.text
    assert "access_token" not in r.json()
    for remedy in _STRANDED_DETAIL_MUST_NAME:
        assert remedy in r.json()["detail"]
    with get_session() as db:
        live = db.query(RefreshToken).filter(
            RefreshToken.user_id == probe["id"],
            RefreshToken.revoked == False).count()  # noqa: E712
    assert live == 0, f"{live} refresh token(s) survived the family revoke"
    assert stranded_events and stranded_events[-1]["stage"] == "refresh"
    assert stranded_events[-1]["action"] == "family_revoked"


def test_stranded_seed_refuses_a_live_mfa_challenge(client, probe, stranded_events):
    """A challenge minted before the row went bad stays valid for its life and
    lands on /mfa/complete; that path must refuse too, before it ever verifies
    a code - and before its own "not configured" check, which the tolerant
    seam would otherwise trip."""
    r = client.post("/api/auth/mfa/setup", headers=probe["headers"], json={})
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    r = client.post("/api/auth/mfa/enable", headers=probe["headers"],
                    json={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json=_USER)
    assert r.status_code == 200 and r.json().get("mfa_required"), r.text
    challenge = r.json()["mfa_token"]
    _strand(probe["id"])
    r = client.post("/api/auth/mfa/complete",
                    json={"mfa_token": challenge, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 403, r.text
    assert "access_token" not in r.json()
    assert stranded_events[-1]["stage"] == "mfa_complete"


def test_stranded_seed_refuses_the_username_change_mint(client, probe, stranded_events):
    """PATCH /api/auth/me/username re-issues a token pair for any bearer - a
    mint like any other (review rider A2-2), so it refuses too, and leaves the
    name untouched."""
    from app.users import get_user_by_id
    _strand(probe["id"])
    r = client.patch("/api/auth/me/username", headers=probe["headers"],
                     json={"new_username": _USER["username"] + "_renamed"})
    assert r.status_code == 403, r.text
    assert "access_token" not in r.json()
    assert get_user_by_id(probe["id"])["username"] == _USER["username"]
    assert stranded_events[-1]["stage"] == "username_change"
