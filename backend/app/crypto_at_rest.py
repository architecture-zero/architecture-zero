"""Fernet encryption for secrets that must live in the database.

First (and so far only) tenant: the MFA TOTP seed. A TOTP seed is a shared
symmetric secret - anyone who reads it mints valid codes forever, so a DB
file copied out of a backup, a stray sqlite3 shell, or a log line that
dumps a user row must see ciphertext, not the seed.

Key derivation: SHA-256 of JWT_SECRET_KEY, urlsafe-base64 - the instance's
one boot-guarded secret, and the same derivation the upstream instance uses
for its connector tokens. Deliberate consequence, stated so nobody learns
it in an incident: ROTATING JWT_SECRET_KEY BREAKS DECRYPTION of everything
this module stored. A rotation therefore needs a re-encrypt sweep
(decrypt with the old key, encrypt with the new) BEFORE the old key is
discarded; the alembic migration that first encrypted this column is the
template for that sweep.

Legacy tolerance: values written before this module existed are plaintext.
Fernet ciphertext always begins with "gAAAAA" (version byte 0x80), and a
base32 TOTP seed (A-Z, 2-7) cannot, so the prefix is a reliable
discriminator here. decrypt_at_rest() passes non-ciphertext through
unchanged - that keeps a restored pre-encryption backup WORKING instead of
locking every MFA user out, and the migration sweep converges such rows the
next time it runs. This tolerance is safe ONLY while every legitimate
plaintext value is base32-shaped; do not reuse this module for a column
where plaintext could itself start with "gAAAAA".
"""
import base64
import hashlib
import os

_SECRET_RAW = os.getenv("JWT_SECRET_KEY", "")
if _SECRET_RAW in ("", "change-me-before-deploying"):
    # Same refusal as app.auth - restated here so this module is safe even
    # if an entrypoint someday imports it before app.auth.
    raise RuntimeError(
        "SECURITY: JWT_SECRET_KEY is unset or the default placeholder - "
        "at-rest encryption would use a published key. Set a strong "
        'secret before boot: python -c "import secrets; print(secrets.token_hex(32))"'
    )
_FERNET_KEY = base64.urlsafe_b64encode(
    hashlib.sha256(_SECRET_RAW.encode()).digest())

_CIPHERTEXT_PREFIX = "gAAAAA"


def _get_fernet():
    from cryptography.fernet import Fernet
    return Fernet(_FERNET_KEY)


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(_CIPHERTEXT_PREFIX)


def encrypt_at_rest(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_at_rest(value: str | None) -> str | None:
    """Ciphertext decrypts; legacy plaintext passes through; None stays None.

    A value that LOOKS like ciphertext but will not decrypt raises - that is
    a wrong key (rotation without the sweep) or a tampered row, and silently
    returning garbage to a TOTP check would just read as 'my codes stopped
    working' with no cause in sight.
    """
    if value is None or not is_encrypted(value):
        return value
    return _get_fernet().decrypt(value.encode()).decode()


def try_decrypt_at_rest(value: str | None) -> tuple[str | None, bool]:
    """decrypt_at_rest, but it REPORTS the failure instead of raising.

    Returns (plaintext, True) when the value is readable - ciphertext that
    opens, legacy plaintext, or None - and (None, False) when the value looks
    like ciphertext and will not open under this instance's key: a rotated or
    replaced JWT_SECRET_KEY without the re-key sweep
    (backend/scripts/rekey_at_rest.py), or a tampered row.

    Why a SECOND function rather than softening decrypt_at_rest: a read seam
    many callers share must not take the process down over one column, but a
    caller that genuinely needs the seed must still be TOLD the seed is gone
    rather than handed something to guess with. Splitting the two keeps the
    strict version available for future tenants of this module and makes every
    tolerant caller declare itself by name (today exactly one:
    users._user_to_dict - torture test T9 upstream, 2026-09-06, where a
    restore under a different secret turned every read of an MFA user into
    HTTP 500; ported fleet-wide 2026-09-10).

    The value half is None on failure ON PURPOSE - there is no partially
    usable TOTP seed. The False half is the caller's obligation: it means "a
    second factor IS enrolled and cannot be read", which must FAIL CLOSED
    (jwt_auth.refuse_if_mfa_seed_stranded). Reading False as "no MFA" would
    mean anyone able to corrupt or replace this one column has STRIPPED the
    second factor, which is the attack, not the recovery.

    Catching Exception and not only InvalidToken: is_encrypted() is a
    six-character prefix test, so a value that merely starts with the prefix
    can fail inside base64 decoding before Fernet ever rules on it. Every one
    of those failures means the same thing to the caller - unreadable - and
    letting a different exception type escape would reintroduce exactly the
    crash this function exists to prevent.
    """
    if value is None or not is_encrypted(value):
        return value, True
    try:
        return _get_fernet().decrypt(value.encode()).decode(), True
    except Exception:
        return None, False
