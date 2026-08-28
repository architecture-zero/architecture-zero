"""The on-demand injection probe.

The probe itself needs a live model and writes to a corpus, so what is pinned
here is the frame around it: that it shares the standing cohort's specs rather
than growing its own, that its delete guard can actually match the collections
it creates, and that it refuses the cases where a "cleanup" would delete
somebody else's data.

The refusal tests are the point. This is the only place in the repository that
waives the ingestion gate outside an Owner-authenticated endpoint, so the
bounds on it need to fail loudly when someone widens them.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")))

import injection_probe as ip                      # noqa: E402


# -- One definition, shared with the standing cohort --------------------------

def test_the_probe_grades_with_the_cohorts_own_specs():
    """The probe and the eval cohort must not drift into measuring different
    things under the same name. The cohort's docstring forward-declares this
    consumer; the import is what makes it true."""
    from app import injection_cohort
    assert ip.PROBES is injection_cohort.SPECS
    assert ip.SOURCE == injection_cohort.SOURCE
    assert ip.grade is injection_cohort.grade


# -- The delete guard must be able to match what the probe creates ------------

def test_probe_collections_uses_the_databases_own_name_derivation():
    """A hand-rolled f"kb_{dept}" disagrees with the real derivation for any
    base that is not already lowercase and clean - and a delete guard whose
    allowlist never matches reports zero dropped while the residue survives.
    That is a guard that lies rather than one that fails."""
    from app.database import _collection_name
    base = "Probe Run"
    derived = ip._probe_collections(base)
    for dept in ip._probe_depts(base):
        expected = _collection_name(dept)
        if expected != "knowledge_base":
            assert expected in derived, (
                f"the guard's allowlist is missing {expected}, so the "
                f"collection it creates could never be dropped")
    assert all(c == c.lower() for c in derived)


def test_the_global_collection_can_never_enter_the_delete_allowlist():
    from app.database import GLOBAL_COLLECTION
    for base in ("general", "", "GENERAL", "injection_probe"):
        assert GLOBAL_COLLECTION not in ip._probe_collections(base)


# -- Refusals: the bounds on the one gate-waiving path in the repo ------------

def test_a_base_that_lands_on_the_real_corpus_is_refused():
    """The cleanup sweeps the bare base too, and delete_source removes by
    source name - so a base resolving onto the main collection would delete the
    planted source from the real corpus. The standing eval plants that same
    source name there while it runs, so this would silently strip a live eval's
    poison and leave it scoring "held" against nothing."""
    for base in ("general", ""):
        why = ip._refuse_unsafe_base(base)
        assert why, f"base {base!r} must be refused"
        assert "corpus" in why.lower() or "access-tier" in why.lower()


def test_a_declared_access_tier_department_is_refused():
    from app.rag_config import DEPARTMENT_MIN_LEVEL
    for base in DEPARTMENT_MIN_LEVEL:
        assert ip._refuse_unsafe_base(base), f"{base} must be refused"


def test_the_default_throwaway_base_is_allowed():
    assert ip._refuse_unsafe_base("injection_probe") is None


def test_an_arm_suffix_cannot_smuggle_a_base_past_the_refusal():
    """The refusal checks every derived name, not just the base - otherwise a
    base whose SUFFIXED form collides with a real department would pass."""
    src = inspect.getsource(ip._refuse_unsafe_base)
    assert "_probe_depts(base)" in src, (
        "the refusal must iterate the derived departments, not just the base")


# -- The gate waiver has exactly one payload ----------------------------------

def test_the_only_gate_waiving_call_plants_the_shipped_fixture():
    """quarantine_exempt=True is the entire privilege budget of this script. A
    --file or --text option would turn a red-team probe into a general
    corpus-poisoning primitive, so the payload is not parameterised: _plant
    takes no source or payload argument beyond the loaded fixture."""
    src = inspect.getsource(ip)
    assert src.count("quarantine_exempt=True") == 1
    params = inspect.signature(ip._plant).parameters
    assert "source" not in params, "the planted source name must not be caller-supplied"
    assert set(params) == {"poison", "department", "trust", "strip_tag"}
    # and the only caller passes load_poison()'s output
    assert "poison = load_poison()" in inspect.getsource(ip.main)


def test_the_probe_never_changes_the_scan_mode():
    """Reading the scan mode is a pre-flight; writing it would be the probe
    arranging for its own plant to land."""
    src = inspect.getsource(ip)
    assert "INJECTION_SCAN_MODE" in src
    assert "INJECTION_SCAN_MODE =" not in src
    assert 'setenv("INJECTION_SCAN_MODE' not in src


# -- The report is written to a fixed path, so it gets redacted ---------------

def test_the_answer_excerpt_is_redacted_before_it_is_written():
    """A FAIL on the credential check means the answer matched a secret-shaped
    pattern, and the report lands at a fixed path.

    The fixture is ASSEMBLED rather than written as a literal: a key-shaped
    string committed to a public repository is a secret-scanner finding whether
    or not it is real, and the scanner reads history, so the literal would
    outlive any later edit of this file.
    """
    probe_secret = "sk-" + ("A" * 8) + ("1" * 8)
    assert probe_secret not in ip._redact(f"here it is: {probe_secret}")


def test_redaction_does_not_lean_on_the_empty_blocklist():
    """The content blocklist is empty unless an operator configured one, so a
    redaction built on it would do nothing on a stock deployment."""
    src = inspect.getsource(ip._redact)
    assert "redact_pii" in src
    assert "apply_blocklist" not in src


# -- Wiring -------------------------------------------------------------------

def test_prompt_parts_are_imported_from_their_post_split_home():
    """These moved out of main when it was split into routers; importing them
    from the old home raises ImportError rather than merely reading oddly."""
    src = inspect.getsource(ip._answer)
    assert "from app.runtime_config import" in src
    assert "from app.main import" not in src


def test_the_probe_asks_at_a_stated_clearance_rather_than_by_omission():
    src = inspect.getsource(ip.main)
    assert "user_level=OWNER_LEVEL" in src
