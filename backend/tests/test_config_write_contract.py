"""THE CONFIG WRITE CONTRACT - three controls that reported success and did nothing.

Found 2026-08-27 while mapping the seams a guided first-run setup would write
to. All three share one failure shape: a write path that answers "fine" whether
or not it wrote, so nobody looking at the response, the log, or the source could
tell. That shape is worse than a missing control, because it stops the operator
looking.

1. PERSONA. get_system_prompt() read `env_val or get_config(...)`, so
   SYSTEM_PROMPT in the environment beat the stored row. .env.example ships
   that variable uncommented, so on every instance cloned from the template the
   admin panel's persona editor saved a row the server never read, and said
   "Saved".

2. ALLOWLIST. PATCH /api/admin/config dropped any key outside its allowlist
   with a bare `continue`, answered 200, and logged the keys the caller SENT
   rather than the keys it wrote - so the audit record agreed with the caller
   that a discarded write had happened.

3. CLAIM THROTTLE. check_rate_limit was wired to exactly one route in the whole
   application (/api/chat), and it returns immediately unless ENABLE_RATE_LIMIT
   is true - which defaults to false. /api/auth/setup, the unauthenticated
   endpoint that hands out ownership of the instance, had no throttle at all.
"""
import pytest

from app import config, security


# ── 1. Persona: the DB row is what gets served ───────────────────────────────

def test_admin_edit_beats_the_environment(monkeypatch):
    """The bug itself. Env sets one persona, an admin saved another: serve the admin's."""
    monkeypatch.setenv("SYSTEM_PROMPT", "FROM THE ENVIRONMENT")
    monkeypatch.setattr(config, "get_config",
                        lambda key, default="": "FROM THE ADMIN PANEL"
                        if key == "system_prompt" else default)

    served = config.get_system_prompt()

    assert served.startswith("FROM THE ADMIN PANEL")
    assert "FROM THE ENVIRONMENT" not in served


def test_untouched_instance_still_serves_its_environment(monkeypatch):
    """The half that must NOT change: no row, so the env-seeded default is served.

    init_config_db() writes _DEFAULTS["system_prompt"] (itself
    os.getenv("SYSTEM_PROMPT", ...)) on a fresh instance's first boot, so a
    never-edited instance reads exactly what its environment set. Simulated
    here by a get_config that finds no row and hands back the default.
    """
    monkeypatch.setitem(config._DEFAULTS, "system_prompt", "SEEDED FROM ENV")
    monkeypatch.setattr(config, "get_config", lambda key, default="": default)

    assert config.get_system_prompt().startswith("SEEDED FROM ENV")


def test_served_prompt_always_carries_the_rails(monkeypatch):
    """Persona is editable; the rails appended to it are not.

    WHERE the rails are appended diverges across the fleet on purpose: some
    instances append them inside get_system_prompt (config.PROMPT_RAILS), others
    at the single LLM-facing call site in main.py (_GROUNDING_RULES /
    _SAFETY_RULES), where that repo's own test_prompt_rails.py pins them. This
    file is kept byte-identical in every repo, so it asserts the property only
    where this seam owns it and defers to test_prompt_rails.py otherwise -
    rather than hardcoding one architecture and failing everywhere else.
    """
    rails = getattr(config, "PROMPT_RAILS", None)
    if rails is None:
        pytest.skip("rails are appended at the call site here - see test_prompt_rails.py")
    monkeypatch.setattr(config, "get_config",
                        lambda key, default="": "anything at all")
    assert config.get_system_prompt().endswith(rails)


# ── 1b. The one instance class the fix moves, reported not hidden ────────────

def test_divergence_names_the_mismatch(monkeypatch):
    """An instance whose env was edited AFTER first boot is the only one that
    changes behavior. It is otherwise indistinguishable from a healthy one."""
    monkeypatch.setenv("SYSTEM_PROMPT", "NEWER, SET IN ENV AFTER FIRST BOOT")
    monkeypatch.setattr(config, "get_config",
                        lambda key, default="": "OLDER, SEEDED AT FIRST BOOT")

    diverged = config.system_prompt_divergence()

    assert diverged == ("NEWER, SET IN ENV AFTER FIRST BOOT",
                        "OLDER, SEEDED AT FIRST BOOT")


def test_no_divergence_when_they_agree(monkeypatch):
    monkeypatch.setenv("SYSTEM_PROMPT", "SAME ON BOTH SIDES")
    monkeypatch.setattr(config, "get_config",
                        lambda key, default="": "SAME ON BOTH SIDES")
    assert config.system_prompt_divergence() is None


def test_no_divergence_when_env_sets_nothing(monkeypatch):
    """The common case - env silent, row authoritative - is not a mismatch."""
    monkeypatch.delenv("SYSTEM_PROMPT", raising=False)
    monkeypatch.setattr(config, "get_config",
                        lambda key, default="": "whatever the row says")
    assert config.system_prompt_divergence() is None


# ── 2. The allowlist refuses by name instead of dropping in silence ──────────

def test_unknown_key_is_refused_and_named(client, admin_headers):
    r = client.patch("/api/admin/config",
                     json={"onboarding_state": '{"step": 3}'},
                     headers=admin_headers)

    assert r.status_code == 400
    assert "onboarding_state" in r.json()["detail"]


def test_unknown_key_does_not_partially_write(client, admin_headers):
    """One bad key rejects the whole body. A caller that gets a 400 must not
    have to guess which half of its write survived."""
    before = client.get("/api/admin/config", headers=admin_headers).json()

    r = client.patch("/api/admin/config",
                     json={"instance_name": "Should Not Land",
                           "wizard_progress": "4"},
                     headers=admin_headers)

    assert r.status_code == 400
    after = client.get("/api/admin/config", headers=admin_headers).json()
    assert after["instance_name"] == before["instance_name"]


def test_suggestions_must_be_a_list(client, admin_headers):
    """The second silent `continue`: a non-list suggestions value was dropped
    and answered 200."""
    r = client.patch("/api/admin/config",
                     json={"suggestions": "not a list"},
                     headers=admin_headers)

    assert r.status_code == 400
    assert "list" in r.json()["detail"].lower()


def test_allowlisted_keys_still_write(client, admin_headers):
    """The contract cuts both ways - refusing unknown keys must not break the
    admin panel, which only ever sends allowlisted ones."""
    original = client.get("/api/admin/config", headers=admin_headers).json()
    try:
        r = client.patch("/api/admin/config",
                         json={"instance_name": "Northwind Traders Assistant"},
                         headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["instance_name"] == "Northwind Traders Assistant"
    finally:
        # Restore: the client fixture is session-scoped, so a stray write here
        # would follow every test that runs after this file.
        client.patch("/api/admin/config",
                     json={"instance_name": original["instance_name"]},
                     headers=admin_headers)


# ── 3. The claim endpoint is throttled, with no off switch ───────────────────

# Both always-on counters are cleared between tests by conftest's autouse
# _reset_setup_throttle fixture - the session-scoped client fixture and other
# test files post to /api/auth/setup, so the store is never empty by the time
# these run.

def test_setup_attempts_are_capped(client, monkeypatch):
    monkeypatch.setattr(security, "SETUP_MAX_ATTEMPTS", 3)
    body = {"username": "claimant", "password": "ClaimPass1"}

    codes = [client.post("/api/auth/setup", json=body).status_code
             for _ in range(4)]

    assert codes[-1] == 429
    assert 429 not in codes[:3]


def test_throttle_is_not_gated_on_the_rate_limit_flag(client, monkeypatch):
    """THE POINT OF THE WHOLE CONTROL. ENABLE_RATE_LIMIT defaults to false, so a
    claim endpoint routed through check_rate_limit would read as guarded in the
    source and be absent in every default deployment - the exists-vs-active
    class. This one has no off switch."""
    monkeypatch.setattr(security, "ENABLE_RATE_LIMIT", False)
    monkeypatch.setattr(security, "SETUP_MAX_ATTEMPTS", 2)
    body = {"username": "claimant", "password": "ClaimPass1"}

    client.post("/api/auth/setup", json=body)
    client.post("/api/auth/setup", json=body)

    assert client.post("/api/auth/setup", json=body).status_code == 429


def test_closed_setup_is_throttled_too(client, monkeypatch):
    """The throttle runs BEFORE admin_exists(). An unthrottled 403 answers
    'has this instance been claimed yet' for free, and forever."""
    monkeypatch.setattr(security, "SETUP_MAX_ATTEMPTS", 2)
    body = {"username": "claimant", "password": "ClaimPass1"}

    # The session client already created an admin, so every one of these is a 403
    # until the throttle takes over.
    first = client.post("/api/auth/setup", json=body).status_code
    client.post("/api/auth/setup", json=body)
    third = client.post("/api/auth/setup", json=body).status_code

    assert first == 403
    assert third == 429


def test_the_setup_store_does_not_grow_without_bound():
    """Swept in full on every call: the dict is tiny by construction except
    under exactly the attack that makes pruning worth doing. _rate_store carried
    this defect for months before its amortized sweep landed."""
    security._setup_store["10.0.0.1"] = [0.0]          # long expired
    security._setup_store["10.0.0.2"] = []             # never populated

    security.check_setup_rate_limit("10.0.0.3")

    assert "10.0.0.1" not in security._setup_store
    assert "10.0.0.2" not in security._setup_store
    assert "10.0.0.3" in security._setup_store
