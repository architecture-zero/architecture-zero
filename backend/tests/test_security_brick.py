"""Security-audit regression pins, each held by its positive signal (a
fail-open control is silent when off - these tests are the loudness).

Covers: REQUIRE_MFA enforcement (was dead config until wired into login),
and the hardened password-policy defaults (pinned in conftest to permissive
values for fixtures; exercised here as shipped).
"""
import importlib

import app.main as main_mod


# -- REQUIRE_MFA: enforced, not dead config ----------------------------------

def test_require_mfa_refuses_unenrolled_password_login(client, monkeypatch):
    monkeypatch.setattr(main_mod, "REQUIRE_MFA", True)
    r = client.post("/api/auth/login",
                    json={"username": "testadmin", "password": "AdminPass1"})
    assert r.status_code == 403
    assert "MFA" in r.json()["detail"]


def test_require_mfa_still_401s_wrong_password_first(client, monkeypatch):
    """The refusal must come AFTER password verification, or the 403 becomes
    an account-state oracle for unauthenticated callers."""
    monkeypatch.setattr(main_mod, "REQUIRE_MFA", True)
    r = client.post("/api/auth/login",
                    json={"username": "testadmin", "password": "wrongpassword"})
    assert r.status_code == 401


def test_mfa_off_login_unaffected(client):
    r = client.post("/api/auth/login",
                    json={"username": "testadmin", "password": "AdminPass1"})
    assert r.status_code == 200 and "access_token" in r.json()


# -- password policy: shipped defaults are the hardened ones ------------------

def test_shipped_password_policy_defaults(monkeypatch):
    from app import jwt_auth
    monkeypatch.delenv("MIN_PASSWORD_LENGTH", raising=False)
    monkeypatch.delenv("REQUIRE_SPECIAL_CHARS", raising=False)
    monkeypatch.delenv("REQUIRE_UPPERCASE", raising=False)
    try:
        importlib.reload(jwt_auth)
        assert jwt_auth.MIN_PASSWORD_LENGTH == 12
        assert jwt_auth.REQUIRE_SPECIAL_CHARS is True
        assert jwt_auth.REQUIRE_UPPERCASE is True
        assert jwt_auth.validate_password("Short1!")            # too short
        assert jwt_auth.validate_password("lowercaseonly!!!")   # no uppercase
        assert jwt_auth.validate_password("NoSpecialChars123")  # no special
        assert jwt_auth.validate_password("Str0ng!Enough-Pass") == []
    finally:
        # restore the conftest-pinned policy for every later test
        monkeypatch.undo()
        importlib.reload(jwt_auth)
