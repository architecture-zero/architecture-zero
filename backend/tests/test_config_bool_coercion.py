"""The admin-config boolean coercion, pinned by its input shapes (2026-09-10).

These rows are STORED as strings and GET /api/admin/config hands them back as
strings, so reading a value and PATCHing it back is the obvious operator move.
The write path parses strings by value: recognizably true stores "true",
anything else stores "false" - an unrecognized string must close a toggle,
never open it. Real booleans keep their truthiness.
"""
from app.config import get_config

_KEY = "guest_mode_enabled"


def test_string_booleans_parse_by_value_and_unknown_strings_close(client, admin_headers):
    before = get_config(_KEY, "")
    try:
        for sent, expected in (("false", "false"), ("true", "true"),
                               ("no", "false"), ("0", "false"), ("on", "true"),
                               ("off", "false"), ("yes please", "false"),
                               ("", "false"), (False, "false"), (True, "true")):
            r = client.patch("/api/admin/config", headers=admin_headers,
                             json={_KEY: sent})
            assert r.status_code == 200, r.text
            assert get_config(_KEY, "") == expected, (
                f"PATCH {_KEY}={sent!r} stored {get_config(_KEY, '')!r}, "
                f"expected {expected!r} - a toggle that mis-parses can only fail open")
    finally:
        if before:
            client.patch("/api/admin/config", headers=admin_headers, json={_KEY: before})
