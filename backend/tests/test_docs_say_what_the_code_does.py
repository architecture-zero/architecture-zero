"""Two things the docs said that the code does not do (2026-11-10).

The embedder outage. With nothing answering at EMBED_BASE a question asked
with retrieval on cannot be embedded. The README's first screen and the
runbook's quick start both said that question "returns a 500". It does not:
the chat route catches the outage and sends a finished answer that says the
knowledge base could not be reached, with a reference id the log carries
(tests/test_retrieval_degraded.py pins the route). And the troubleshooting
guide, which the README describes as keyed to the exact strings the code
emits, had no entry for that sentence.

The sign-in note. The workflow's acceptance of the python-jose advisory said
every decode pins HS256 with the server's own key "or RS256 with a sign-in
provider's fetched key". This code has no sign-in provider and no RS256
decode; the note twenty lines above it in the same file already said every
token here is HS256.

FIX: the three documents quote the sentence the chat route sends and none
calls the outage a 500; the troubleshooting guide has the entry; the workflow
names RS256 only if the app decodes with it.
CONTROL: the sentence beside each fix still stands - readiness is the check
that sees a missing embedder, and the advisory is still pinned by its test.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = ("README.md", "docs/runbook.md", "knowledge/troubleshooting.md")
WORKFLOW = ".github/workflows/ci.yml"

pytestmark = pytest.mark.skipif(
    not all((REPO / name).exists() for name in DOCS + (WORKFLOW,)),
    reason="the repo's documents are not beside this suite")

# What the chat route says on an outage turn, up to where a doc may trail off.
OPENING = "I could not reach my knowledge base for this question"
# What an instance asked for its documents as a peer answers in the same state.
PEER_DETAIL = "This instance's knowledge base is unavailable right now."


def _text(name: str) -> str:
    """A document as one line, so a sentence wrapped across lines still reads."""
    return " ".join((REPO / name).read_text(encoding="utf-8").split())


def _app_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted((REPO / "backend" / "app").rglob("*.py")))


# -- the embedder outage -------------------------------------------------------

def test_the_sentence_is_the_one_the_code_sends(chat_route_src):
    assert OPENING in " ".join(chat_route_src.replace('"', " ").split())
    assert PEER_DETAIL in _app_source()


@pytest.mark.parametrize("name", DOCS)
def test_each_document_quotes_the_outage_sentence(name):
    assert OPENING in _text(name)


@pytest.mark.parametrize("name", DOCS)
def test_no_document_calls_the_outage_a_500(name):
    text = _text(name)
    assert "500 rather than" not in text
    assert "returns a 500" not in text


def test_the_troubleshooting_guide_has_the_entry():
    guide = (REPO / "knowledge/troubleshooting.md").read_text(encoding="utf-8")
    assert f'## "{OPENING}"' in guide.splitlines()
    assert PEER_DETAIL in _text("knowledge/troubleshooting.md")


@pytest.mark.parametrize("name", ("README.md", "docs/runbook.md"))
def test_control_readiness_is_still_named_as_the_check_that_sees_it(name):
    assert "`/api/health/ready` is the check that sees it" in _text(name)


# -- the sign-in note ----------------------------------------------------------

def test_the_workflow_names_rs256_only_if_the_app_decodes_with_it():
    if "RS256" in _text(WORKFLOW):
        assert "RS256" in _app_source(), (
            "the workflow describes an RS256 decode this code does not have")


def test_control_the_advisory_is_still_accepted_and_pinned_by_its_test():
    workflow = _text(WORKFLOW)
    assert "CVE-2026-85394" in workflow
    assert "backend/tests/test_jwt_algorithms_pinned.py" in workflow
    assert (REPO / "backend/tests/test_jwt_algorithms_pinned.py").exists()
