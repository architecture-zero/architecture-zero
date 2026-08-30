"""Pins the condition three unpatchable CVEs depend on.

CVE-2026-45830 / -45831 / -45833 affect chromadb 0.4.17 through 1.5.9
inclusive, which is every release - there is no version to upgrade to. All
three describe the chroma SERVER surface: cross-tenant read/write over the
HTTP /api/v2/tenants routes, an authorization provider that never checks which
tenant a permission covers, and code injection via a malicious model repo with
trust_remote_code on a collection-update endpoint.

None of them is reachable here, for one reason: this app runs chroma EMBEDDED.
database.py builds a chromadb.PersistentClient in-process - no HttpClient, no
chroma server, no chroma port, no tenants, and no authorization provider. The
routes those CVEs describe do not exist in this process.

That makes the embedded property the only thing standing between "not
applicable" and three unpatched findings, one of them remote code execution.
It is a deployment property, not a code property, which is exactly the kind
that erodes quietly - and this repository is a template, so it erodes in other
people's deployments, not just this one.

A comment cannot enforce a condition. This test can: the day someone reaches
for an HttpClient, stands up a chroma server, or points the app at one, the
build fails and the CVEs have to be re-triaged - which is exactly the day they
stop being theoretical. SECURITY.md states the same requirement for operators
who never run this suite. Delete this test only together with that section.
"""
import pathlib

import pytest

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Ways chroma stops being embedded. Each is a real client/server construction,
# not a mention - the docstrings and comments that discuss this are prose.
_SERVER_SHAPES = (
    "chromadb.HttpClient",
    "chromadb.AsyncHttpClient",
    "chromadb.Client(",          # the in-memory/server-ish client, not Persistent
    "chromadb.server",
    "chroma_server_host",
    "CHROMA_SERVER_HOST",
)


def _python_sources():
    return sorted(p for p in _APP.rglob("*.py") if "__pycache__" not in str(p))


def test_the_app_only_ever_constructs_an_embedded_client():
    offenders = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue          # prose about this policy is allowed
            for shape in _SERVER_SHAPES:
                if shape in line:
                    offenders.append(f"{path.name}:{line_no}: {shape}")
    assert not offenders, (
        "chroma is no longer embedded-only, which makes CVE-2026-45830 / "
        "-45831 / -45833 live findings against this deployment - no fix "
        "exists at any chromadb version. Re-triage them, and update "
        "SECURITY.md's 'Deploying this safely' section, before shipping "
        "this:\n  " + "\n  ".join(offenders))


def test_a_persistent_client_is_actually_what_is_used():
    """The positive half - the guard above passes trivially if chroma stops
    being used at all, so assert the embedded client is present."""
    db = _APP / "database.py"
    if not db.exists():
        pytest.skip("database.py not present in this layout")
    assert "PersistentClient" in db.read_text(encoding="utf-8", errors="ignore")
