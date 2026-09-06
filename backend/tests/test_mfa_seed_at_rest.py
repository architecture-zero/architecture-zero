"""MFA TOTP seeds are ENCRYPTED AT REST (2026-09-06 hygiene batch).

A TOTP seed is a mintable credential: anyone who reads the column value
generates valid second-factor codes forever. Until this change the users
table held it in the clear, so a copied DB file, a backup, or a stray
sqlite3 shell got the second factor along with the password hashes it was
supposed to back up. Now:

  1. Enrollment stores Fernet ciphertext (key derived from JWT_SECRET_KEY).
  2. The one read seam (users._user_to_dict) decrypts, so pyotp consumers
     are unchanged and every existing MFA test keeps passing untouched.
  3. Legacy plaintext rows still WORK (prefix discriminator passthrough) -
     a restored pre-encryption backup must not lock every MFA user out.
  4. The boot sweep (db._encrypt_mfa_seeds_sweep) converges legacy rows to
     ciphertext, idempotently.
"""
import pyotp
import pytest

from app.crypto_at_rest import is_encrypted
from app.db import get_session, _encrypt_mfa_seeds_sweep
from app.models import User
from app.users import get_user_by_id, get_user_by_username

_USER = {"username": "mfa_atrest_probe", "password": "MfaRest#2026x"}


@pytest.fixture
def probe(client, admin_headers):
    """Logged-in probe user (mirrors test_mfa_status_and_rekey's fixture)."""
    existing = get_user_by_username(_USER["username"])
    if not existing:
        for role in ("user", "member"):
            r = client.post("/api/users", json={**_USER, "role": role},
                            headers=admin_headers)
            if r.status_code in (200, 201):
                break
        assert r.status_code in (200, 201), f"no accepted role: {r.text}"
        existing = get_user_by_username(_USER["username"])
    r = client.post("/api/auth/login", json=_USER)
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    yield {"id": existing["id"], "headers": headers}
    with get_session() as db:
        db.query(User).filter(User.id == existing["id"]).update(
            {"mfa_enabled": False, "mfa_secret": None})


def _raw_column(user_id):
    """The stored bytes themselves - NOT the decrypting dict path."""
    with get_session() as db:
        return db.query(User.mfa_secret).filter(User.id == user_id).scalar()


def test_enrollment_stores_ciphertext_and_still_verifies(client, probe):
    r = client.post("/api/auth/mfa/setup", headers=probe["headers"], json={})
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]

    raw = _raw_column(probe["id"])
    assert raw != secret, "the seed reached the column IN THE CLEAR"
    assert is_encrypted(raw), f"stored value is not Fernet ciphertext: {raw[:12]}..."
    # The read seam hands consumers the real seed.
    assert get_user_by_id(probe["id"])["mfa_secret"] == secret

    # And the whole flow still works end to end: prove possession, enable.
    code = pyotp.TOTP(secret).now()
    r = client.post("/api/auth/mfa/enable", headers=probe["headers"],
                    json={"code": code})
    assert r.status_code == 200, r.text


def test_legacy_plaintext_row_still_verifies(client, probe):
    """A pre-encryption row (or restored backup) must keep working - the
    passthrough is deliberate, not an accident to fix later."""
    legacy_seed = pyotp.random_base32()
    with get_session() as db:
        db.query(User).filter(User.id == probe["id"]).update(
            {"mfa_secret": legacy_seed, "mfa_enabled": False})
    assert get_user_by_id(probe["id"])["mfa_secret"] == legacy_seed
    code = pyotp.TOTP(legacy_seed).now()
    r = client.post("/api/auth/mfa/enable", headers=probe["headers"],
                    json={"code": code})
    assert r.status_code == 200, r.text


def test_boot_sweep_converges_legacy_rows_idempotently(client, probe):
    legacy_seed = pyotp.random_base32()
    with get_session() as db:
        db.query(User).filter(User.id == probe["id"]).update(
            {"mfa_secret": legacy_seed, "mfa_enabled": True})

    assert _encrypt_mfa_seeds_sweep() >= 1
    raw = _raw_column(probe["id"])
    assert is_encrypted(raw) and raw != legacy_seed
    # Converged row still decrypts to the same seed - nobody got locked out.
    assert get_user_by_id(probe["id"])["mfa_secret"] == legacy_seed
    # Second pass finds nothing left to do on this row set.
    before = _raw_column(probe["id"])
    _encrypt_mfa_seeds_sweep()
    assert _raw_column(probe["id"]) == before, "sweep double-encrypted a row"
