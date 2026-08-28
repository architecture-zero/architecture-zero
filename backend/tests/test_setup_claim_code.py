"""The first-Owner CLAIM CODE - the control that closes the claim race.

WHAT THE RACE IS. /api/auth/setup is unauthenticated, sits in auth.py's
EXCLUDED_PATHS, and stays open until an Owner exists. A deployment that is
publicly reachable before its operator finishes setup goes to whoever asks
first. The 2026-08-27 throttle bounds ATTEMPTS and explicitly does not help
here: one request is all it takes, and one request is under every limit.

WHY IT MATTERS MORE IN THIS REPO THAN ANYWHERE. This is the template. "A fresh
deployment, publicly reachable, not yet claimed" is not an edge case here - it
is the normal first ten minutes of every deployment anyone ever makes from it,
and the operator inherits whatever posture ships rather than choosing one.

WHY THESE TESTS LOOK LIKE THIS. The session-scoped `client` fixture has already
claimed the deployment by the time anything here runs, so owner_exists()
short-circuits to 403 before the code is ever consulted. Every endpoint test
below reopens the claim window explicitly (monkeypatched owner_exists) and says
so. That is the only honest way to exercise a one-time gate from a suite that
has already used it up.
"""

import pytest

# owner_exists is read by the SETUP ROUTE, which lives in the auth router -
# main still binds its own for the boot banner, so patching main here would
# succeed and inject into a module the route no longer consults.
from app.routers import auth as auth_route_mod
from app import security


# -- Unit: the code itself ----------------------------------------------------

def test_generated_code_is_not_predictable():
    """The one property the code must have. A predictable claim code is not a
    control at all - it is a documented default, which is the class the JWT
    default-secret guard exists to refuse."""
    security._claim_code = None
    first = security.setup_claim_code()
    security._claim_code = None
    second = security.setup_claim_code()

    assert first != second
    assert len(first) >= 20


def test_code_is_stable_within_a_process():
    """Minted once and reused. If it re-rolled per read, the banner printed at
    boot would never match the code the endpoint checks."""
    security._claim_code = None
    assert security.setup_claim_code() == security.setup_claim_code()


def test_env_override_wins(monkeypatch):
    """SETUP_CLAIM_CODE is the multi-worker and provisioned-secret answer: each
    uvicorn worker mints its own value, so a multi-process deployment must pin
    one or only a single worker would accept the operator's code."""
    monkeypatch.setattr(security, "SETUP_CLAIM_CODE_ENV", "operator-supplied-code")
    monkeypatch.setattr(security, "_claim_code", None)

    assert security.setup_claim_code() == "operator-supplied-code"
    assert security.claim_code_source() == "env"


def test_wrong_code_is_refused():
    security._claim_code = None
    security._claim_code_burned = False
    security.setup_claim_code()

    with pytest.raises(Exception) as exc:
        security.verify_setup_claim_code("not-the-code")
    assert exc.value.status_code == 401


def test_absent_code_is_refused():
    """The field defaults to "" on the request model, so this is what an old
    client - or a claim-jumper who never saw the logs - actually sends."""
    security._claim_code = None
    security._claim_code_burned = False
    security.setup_claim_code()

    for missing in ("", None):
        with pytest.raises(Exception) as exc:
            security.verify_setup_claim_code(missing)
        assert exc.value.status_code == 401


def test_non_ascii_code_is_refused_not_crashed():
    """secrets.compare_digest raises TypeError on a non-ASCII str, and this
    input is unauthenticated. Compared on bytes precisely so one multi-byte
    character returns 401 instead of a 500 with a traceback."""
    security._claim_code = None
    security._claim_code_burned = False
    security.setup_claim_code()

    with pytest.raises(Exception) as exc:
        security.verify_setup_claim_code("cle-de-reclamation-éè")
    assert exc.value.status_code == 401


def test_burned_code_stops_working_even_if_it_is_still_correct():
    """Belt and braces beside owner_exists(). A leaked banner from the container
    logs must not be replayable, and this half does not wait on a database read
    to say so."""
    security._claim_code = None
    security._claim_code_burned = False
    code = security.setup_claim_code()
    security.verify_setup_claim_code(code)   # fine before the burn

    security.burn_setup_claim_code()
    with pytest.raises(Exception) as exc:
        security.verify_setup_claim_code(code)
    assert exc.value.status_code == 401

    security._claim_code_burned = False


# -- Endpoint: the gate in place ----------------------------------------------

@pytest.fixture
def unclaimed(monkeypatch):
    """Reopen the claim window the session fixture already closed.

    Yields the live code. Resets the burn flag on the way out, because a test
    that claims successfully would otherwise leave the gate shut for the next.
    """
    monkeypatch.setattr(auth_route_mod, "owner_exists", lambda: False)
    monkeypatch.setattr(security, "_claim_code", None)
    monkeypatch.setattr(security, "_claim_code_burned", False)
    yield security.setup_claim_code()
    security._claim_code_burned = False


def test_claim_without_the_code_is_refused(client, unclaimed):
    """THE POINT OF THE WHOLE CONTROL. This is the race: an unclaimed, publicly
    reachable deployment and a stranger who got there first. Before this commit
    the same request returned 200 and the deployment."""
    r = client.post("/api/auth/setup",
                    json={"username": "jumper", "password": "JumperPass1"})

    assert r.status_code == 401
    assert "claim code" in r.json()["detail"].lower()


def test_claim_with_a_wrong_code_is_refused(client, unclaimed):
    r = client.post("/api/auth/setup",
                    json={"username": "jumper", "password": "JumperPass1",
                          "claim_code": "guessed-wrong"})
    assert r.status_code == 401


def test_the_code_is_checked_before_the_password_policy(client, unclaimed):
    """Order matters. Password rules are free reconnaissance for an anonymous
    caller - which minimum length, which character classes - and there is no
    reason to answer that for someone who cannot claim the deployment anyway.
    A policy 400 here would mean the code check ran too late."""
    r = client.post("/api/auth/setup",
                    json={"username": "jumper", "password": "x",
                          "claim_code": "guessed-wrong"})

    assert r.status_code == 401


def test_a_claimed_deployment_still_answers_403_not_401(client):
    """The claimed-ness contract is UNCHANGED by this commit. The code check
    sits below the owner_exists() 403 on purpose: answering 401 on both paths
    would hide claimed-ness better, but that is a change to a control with its
    own tests and it belongs in its own decision."""
    r = client.post("/api/auth/setup",
                    json={"username": "other", "password": "OtherPass1",
                          "claim_code": "anything-at-all"})
    assert r.status_code == 403


def test_the_throttle_still_runs_first(client, unclaimed, monkeypatch):
    """The two controls compose rather than replace each other. Without the code
    the caller gets 401s - but not an unlimited supply of them, or the
    throttle's whole reason for existing (bounded blast radius on a
    bulk-reachable endpoint) would have been quietly undone here."""
    monkeypatch.setattr(security, "SETUP_MAX_ATTEMPTS", 3)
    body = {"username": "jumper", "password": "JumperPass1",
            "claim_code": "wrong"}

    codes = [client.post("/api/auth/setup", json=body).status_code
             for _ in range(4)]

    assert codes[:3] == [401, 401, 401]
    assert codes[-1] == 429


def test_the_right_code_claims_the_deployment_and_burns(client, unclaimed):
    """The happy path, and the receipt that the code is single-use: the second
    identical request fails on the burn, not on owner_exists (still patched
    False by the fixture), so this proves the process-local half specifically."""
    r = client.post("/api/auth/setup",
                    json={"username": "rightful", "password": "RightfulPass1",
                          "claim_code": unclaimed})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "owner created"

    again = client.post("/api/auth/setup",
                        json={"username": "second", "password": "SecondPass1",
                              "claim_code": unclaimed})
    assert again.status_code == 401


def test_a_failed_claim_does_not_burn_the_code(client, unclaimed):
    """An IntegrityError - the operator retyping a name that already exists -
    means the claim did NOT happen. Burning there would strand them with a dead
    code and no Owner, and the only way back would be restarting the container.
    """
    taken = {"username": "testadmin", "password": "AnotherPass1",
             "claim_code": unclaimed}

    first = client.post("/api/auth/setup", json=taken)
    assert first.status_code == 409

    r = client.post("/api/auth/setup",
                    json={"username": "recovered", "password": "RecoverPass1",
                          "claim_code": unclaimed})
    assert r.status_code == 200, r.text
