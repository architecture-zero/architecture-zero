"""Provider API keys are ENCRYPTED AT REST (2026-09-06 hygiene batch).

The GET/PATCH responses already masked stored keys (`<provider>_key_set`
booleans), but the config TABLE carried the raw credential - so a copied
DB file or backup held every provider key even though no endpoint would
serve one. Same treatment as the MFA seed, softer failure mode (see
config.decrypt_secret's docstring): write path encrypts, the one runtime
read seam (providers._get_runtime) decrypts, legacy plaintext passes
through, and the boot sweep (config.encrypt_plaintext_secrets) converges
pre-fix rows.
"""
from app.config import (decrypt_secret, encrypt_plaintext_secrets,
                        encrypt_secret, get_all_config, get_config,
                        set_config)
from app.crypto_at_rest import is_encrypted
from app.providers import _get_runtime

_KEY = "sk-ant-atrest-probe-000000000000"


def _cleanup():
    set_config("anthropic_api_key", "")


def test_settings_write_stores_ciphertext_and_masked_read_unchanged(
        client, admin_headers):
    try:
        r = client.put("/api/settings", headers=admin_headers,
                       json={"anthropic_api_key": _KEY})
        assert r.status_code == 200, r.text

        raw = get_config("anthropic_api_key", "")
        assert raw != _KEY, "the key reached the config table IN THE CLEAR"
        assert is_encrypted(raw), f"stored value is not ciphertext: {raw[:12]}..."

        # The masked surface still reports presence, never the value.
        body = r.json()
        assert body.get("anthropic_key_set") is True
        assert _KEY not in r.text

        # And the runtime seam hands providers the real key.
        assert _get_runtime("anthropic_api_key", "ANTHROPIC_API_KEY") == _KEY
    finally:
        _cleanup()


def test_legacy_plaintext_row_reads_through_and_sweep_converges():
    try:
        set_config("anthropic_api_key", _KEY)  # a pre-fix write
        assert _get_runtime("anthropic_api_key", "ANTHROPIC_API_KEY") == _KEY

        moved = encrypt_plaintext_secrets()
        assert moved >= 1
        raw = get_config("anthropic_api_key", "")
        assert is_encrypted(raw)
        assert decrypt_secret(raw) == _KEY
        assert _get_runtime("anthropic_api_key", "ANTHROPIC_API_KEY") == _KEY

        # Idempotent: a second pass moves nothing on this row.
        before = get_config("anthropic_api_key", "")
        encrypt_plaintext_secrets()
        assert get_config("anthropic_api_key", "") == before
    finally:
        _cleanup()


def test_no_secret_config_row_remains_plaintext_after_sweep():
    """The sweep's own invariant, asserted over the whole table."""
    from app.config import is_secret_config_key
    encrypt_plaintext_secrets()
    for key, value in get_all_config().items():
        if is_secret_config_key(key) and value:
            assert is_encrypted(value), f"{key} still plaintext at rest"
