"""rekey_at_rest.py - re-encrypt every JWT-derived at-rest secret in the
database from an OLD JWT_SECRET_KEY to a NEW one.

Why this exists (torture test T9, upstream 2026-09-06; fleet port
2026-09-10): the MFA TOTP seed and the stored provider keys are Fernet
ciphertext under a key derived from JWT_SECRET_KEY (app/crypto_at_rest.py).
Rotate that secret without this sweep, or restore a database into an instance
that boots with a different secret, and every one of those rows becomes
unreadable at once. The app then REFUSES to sign in every account whose
second factor it can no longer verify (a 403 naming this script) rather than
crash or downgrade to password-only. This script is the remedy.

Run OFFLINE, inside the backend container, with the backend STOPPED:

    docker compose stop backend
    docker compose run --rm --no-deps backend python scripts/rekey_at_rest.py --db /app/data/history.db --dry-run
    docker compose run --rm --no-deps backend python scripts/rekey_at_rest.py --db /app/data/history.db
    # set the new JWT_SECRET_KEY in the host environment, then
    docker compose up -d backend

A restore into a fresh instance is the same event: run it after the restore
and before the first boot, with OLD = the secret the backup was written under.

Secrets are NEVER taken on the command line (they would sit in `ps` output
and shell history). On a terminal the script prompts for both; otherwise it
reads REKEY_OLD_SECRET and REKEY_NEW_SECRET from the environment.

Imports NOTHING from app/ on purpose: app.crypto_at_rest refuses to import
when JWT_SECRET_KEY is unset, and "unset" is the normal state of an offline
shell. Stdlib plus the `cryptography` package the image already carries.

Exit codes: 0 every ciphertext row is readable under NEW afterwards;
2 at least one row is undecryptable under BOTH keys (listed, never
rewritten - a restore procedure can gate on this); 3 usage.
"""
import argparse
import base64
import getpass
import hashlib
import os
import sqlite3
import sys

PREFIX = "gAAAAA"   # every Fernet token starts this way (version byte 0x80)

# Every column this surface writes as Fernet ciphertext under each secret,
# enumerated from the WRITERS in app/ (encrypt_at_rest / encrypt_secret).
# (table, key column, value column, key-suffix filter or None). Keep this in
# step with the app: a tenant missing here is a row that stays unreadable
# after a rotation, silently.
TENANTS = {
    "JWT_SECRET_KEY": [
        ("users", "id", "mfa_secret", None),          # the MFA TOTP seed
        ("config", "key", "value", "_api_key"),      # stored provider keys
    ],
}


def _fernet(secret: str):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _read_secrets() -> tuple[str, str]:
    # An environment variable that is SET wins, even if empty (an empty one is
    # a usage error reported below, never a prompt). Only when neither is set
    # AND stdin is a terminal does the script prompt. The order matters on
    # Windows, where isatty() answers True for the NUL device, so a
    # non-interactive caller with stdin on NUL would otherwise hang in getpass.
    if "REKEY_OLD_SECRET" in os.environ or "REKEY_NEW_SECRET" in os.environ:
        old = os.environ.get("REKEY_OLD_SECRET", "")
        new = os.environ.get("REKEY_NEW_SECRET", "")
    elif sys.stdin.isatty():
        old = getpass.getpass("Previous secret (the one the rows were written under): ")
        new = getpass.getpass("New secret (the one the instance will boot with): ")
    else:
        old = new = ""
    return old.strip(), new.strip()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-encrypt at-rest secrets from an old secret to a new one. "
                    "Secrets are prompted for, or read from REKEY_OLD_SECRET / "
                    "REKEY_NEW_SECRET - never passed as arguments.")
    ap.add_argument("--db", required=True,
                    help="path to the SQLite file (inside the container: /app/data/history.db)")
    ap.add_argument("--secret", default="JWT_SECRET_KEY", choices=sorted(TENANTS),
                    help="which secret is being rotated")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    a = ap.parse_args()

    if not os.path.isfile(a.db):
        print(f"no such database file: {a.db}", file=sys.stderr)
        return 3
    old_raw, new_raw = _read_secrets()
    if not old_raw or not new_raw:
        print("both secrets are required - prompted on a terminal, or "
              "REKEY_OLD_SECRET and REKEY_NEW_SECRET in the environment; "
              "never on the command line", file=sys.stderr)
        return 3
    old, new = _fernet(old_raw), _fernet(new_raw)
    from cryptography.fernet import InvalidToken

    conn = sqlite3.connect(a.db)
    cur = conn.cursor()
    rekeyed, kept, stuck = 0, 0, []

    def process(table, key_col, key, val_col, val):
        nonlocal rekeyed, kept
        if not val or not str(val).startswith(PREFIX):
            kept += 1                      # NULL or legacy plaintext: not ours
            return
        try:
            new.decrypt(val.encode())
            kept += 1                      # already under the new key
            return
        except InvalidToken:
            pass
        try:
            plain = old.decrypt(val.encode())
        except InvalidToken:
            stuck.append(f"{table}.{val_col} where {key_col}={key!r}")
            return
        if not a.dry_run:
            cur.execute(f"UPDATE {table} SET {val_col}=? WHERE {key_col}=?",
                        (new.encrypt(plain).decode(), key))
        rekeyed += 1

    try:
        for table, key_col, val_col, suffix in TENANTS[a.secret]:
            rows = cur.execute(f"SELECT {key_col}, {val_col} FROM {table}").fetchall()
            for key, val in rows:
                if suffix and not str(key).endswith(suffix):
                    continue
                process(table, key_col, key, val_col, val)
    except sqlite3.OperationalError as e:
        conn.close()
        print(f"database does not match this surface's TENANTS table: {e}", file=sys.stderr)
        return 3
    if not a.dry_run:
        conn.commit()
    conn.close()
    print(f"{'DRY RUN - ' if a.dry_run else ''}{a.secret}: rekeyed {rekeyed}, "
          f"untouched {kept}, undecryptable under both keys {len(stuck)}")
    for s in stuck:
        print("  STUCK:", s)
    return 2 if stuck else 0


if __name__ == "__main__":
    sys.exit(main())
