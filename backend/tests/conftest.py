import os
import tempfile

# -- Env vars BEFORE any app import ------------------------------------------
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["JWT_SECRET_KEY"] = "test-secret-key"
os.environ["ENABLE_AUTH"] = "false"
# Password policy pinned to permissive values so fixture passwords stay
# short; the shipped hardened defaults (12 chars, uppercase + special) are
# exercised explicitly in test_security_brick.py instead.
os.environ["MIN_PASSWORD_LENGTH"] = "8"
os.environ["REQUIRE_SPECIAL_CHARS"] = "false"
os.environ["REQUIRE_UPPERCASE"] = "false"
# Guest chat is double-gated (env AND admin config). The env half is opened
# here so guest-path tests can exercise it; the config half stays off unless
# a test patches it.
os.environ["ALLOW_GUEST_MODE"] = "true"
os.environ["ENABLE_AUDIT_LOG"] = "false"
os.environ["CORS_ORIGIN"] = "*"
# Isolate every data-path derivation (ingest-state file) from the real
# backend/data dir - the chroma CLIENT is mocked below, but path-based code
# would otherwise touch real files.
os.environ["CHROMA_PATH"] = tempfile.mkdtemp(prefix="test-chroma-")

from unittest.mock import MagicMock, patch
import pytest

# -- Mock chromadb before app.database imports it -----------------------------
_mock_col = MagicMock()
_mock_col.count.return_value = 0
_mock_col.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
_mock_col.get.return_value = {"ids": [], "metadatas": []}
_mock_col.upsert.return_value = None
_mock_col.delete.return_value = None

_mock_chroma_instance = MagicMock()
_mock_chroma_instance.get_or_create_collection.return_value = _mock_col
_mock_chroma_instance.list_collections.return_value = []

patch("chromadb.PersistentClient", return_value=_mock_chroma_instance).start()
patch("chromadb.Settings", return_value=MagicMock()).start()

from fastapi.testclient import TestClient
from app.main import app  # triggers all module-level init (DB schema, config seed)

# -- Mock _embed after app.database is imported --------------------------------
patch("app.database._embed", return_value=[0.0] * 768).start()

# -- The BATCH leg's socket, closed (F12, ported from az-personal 2026-08-27) --
# The mock above covers `_embed` (the per-text leg) and leaves `_embed_batch`
# open, so every batched write opened a REAL HTTP connection before falling back
# to the mocked path. What that cost depended entirely on what was listening on
# the developer's machine, which is what makes it a bug rather than a slow test:
# in CI nothing listens, so it is an instant refusal and invisible; on a
# workstation running Ollama this repo's suite stalled ~230s, and on az-personal
# the equivalent files WEDGED for 35 minutes. Green either way, which is how it
# survived. Found as F12 of the 2026-08-27 batch review.
#
# Not mocking `_embed_batch` itself: real tests exercise it, and blanket-mocking
# would delete that coverage instead of isolating it. The SOCKET closes instead -
# requests.post inside app.database raises instantly, the batch leg fails the way
# it already knows how to fail, and its existing `[_embed(t) for t in texts]`
# fallback runs the mocked per-text path. The batch code path still executes.
#
# Safe against the other requests.post in this module: it belongs to `_embed`,
# which is mocked at the function level above and never reaches the socket.
def _no_outbound_embed(*args, **kwargs):
    import requests as _rq
    url = args[0] if args else kwargs.get("url", "?")
    raise _rq.exceptions.ConnectionError(
        f"conftest: outbound embed calls are blocked in tests ({url})")


patch("app.database.requests.post", side_effect=_no_outbound_embed).start()

_ADMIN = {"username": "testadmin", "password": "AdminPass1"}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        # The claim code is REQUIRED since 2026-08-27 - /api/auth/setup hands
        # out ownership of the deployment, so it now takes a secret minted at
        # boot and printed to the container logs. Read here rather than
        # hard-coded: the generated value is per-process by design, and a
        # fixture pinning a literal would be asserting the code is predictable,
        # which is the one property it must not have.
        from app import security
        c.post("/api/auth/setup",
               json={**_ADMIN, "claim_code": security.setup_claim_code()})
        yield c


@pytest.fixture
def admin_headers(client):
    r = client.post("/api/auth/login", json=_ADMIN)
    assert r.status_code == 200, f"Admin login failed: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(autouse=True)
def _reset_setup_throttle():
    """Clear the always-on auth-abuse counters between tests (2026-08-27).

    Two stores: the first-owner claim throttle, and the MFA challenge guard.
    Both are process-global by design, and several test files post to
    /api/auth/setup - without this reset the later ones start colliding with the
    limit as the suite grows. Isolation, not a weakened control.
    """
    from app import security
    security._setup_store.clear()
    security._mfa_challenges.clear()
    yield
    security._setup_store.clear()
    security._mfa_challenges.clear()
