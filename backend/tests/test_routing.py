"""Tests for app/routing.py - deterministic query-type routing.

History routing keeps the session log out of the default retrieval pool but
adds it back for past-work-shaped questions; status routing lifts generated
DB-truth chunks over stale narrative; the follow-up resolver expands bare
deictic messages ("current", "more") against the last substantive user turn.
All regex-based and recall-biased by design - see the module docstring.
"""
from app.routing import route_departments


def test_history_shaped_questions_route_to_history():
    questions = [
        # Eval-set-shaped history questions (regression guards for the
        # original routing fix)
        "What did we work on in the most recent work sessions?",
        "What was the root cause of the missed dispatch window at the Crestline hub in early March 2031?",
        "When we A/B tested two label printers, what made the first trial run invalid, and what was the corrected outcome?",
        "Which issues did the outside audit find during the March 2031 review, and how were they closed?",
        "During the warehouse safety audit, what gap was found at Harbor Point and how was it closed?",
        "What did the pick-error audit measure in early March 2031, and what did the misses have in common?",
        # Free-form phrasings a real user types
        "What happened last session?",
        "When did the reranker ship?",
        "What did you change on 2031-03-04?",
        "What have we shipped recently?",
        # Temporal-span phrasings (a live pressure-test miss: none of the
        # original patterns caught these, yet their answers live entirely in
        # the history pool)
        "What is our current measured retrieval recall, and how has that number changed since mid-June?",
        "How has retrieval quality evolved over time?",
        "What broke in the past week?",
        "Show me the recall trajectory.",
        "How have the eval numbers improved since March 2031 began?",
    ]
    for q in questions:
        assert route_departments(q) == ["history"], f"should route: {q!r}"


def test_current_fact_questions_do_not_route():
    questions = [
        "What is the expedited air freight surcharge?",
        "What are the specs of the Harbor Point label printers?",
        "What is the current main build arc, and what is the next build?",
        "Which shipping lane is excluded from the flat-rate program, and why?",
        "What is the default pallet position size for new clients?",
        "How much does the pro plan cost per year?",
        "What is the current price of Bitcoin right now?",
    ]
    for q in questions:
        assert route_departments(q) == [], f"should NOT route: {q!r}"


def test_route_departments_handles_empty():
    assert route_departments("") == []
    assert route_departments(None) == []


def test_history_source_maps_to_history_department():
    from app.main import _dept_for_source
    assert _dept_for_source("internal/session-log.md") == "history"
    # The whole internal/ subtree is restricted (Owner-only) - gate the
    # COPIES, not just the canonical file. Public KB content stays general.
    assert _dept_for_source("internal/PLAN.md") == "restricted"
    assert _dept_for_source("internal/audits/q3-review.md") == "restricted"
    assert _dept_for_source("faq.md") == "general"


def test_status_query_detection():
    from app.routing import is_status_query

    positives = [
        "What's next in my plans",                                   # pressure-test verbatim
        "What is the current main build arc, and what is the next build?",
        "What are we working on right now?",
        "Where do we stand?",
        "What's the status of the reranker port?",
        "What are my priorities?",
        "What's left in the current milestone?",
        "Any open loops?",
        "What should I work on next?",
    ]
    for q in positives:
        assert is_status_query(q), f"should be status-shaped: {q!r}"

    negatives = [
        "What is the expedited surcharge rate?",
        "What is the current price of Bitcoin right now?",           # guardrail trap
        "What did we work on in the most recent work sessions?",
        "What are the specs of the label printers?",
        "How much does the pro plan cost per year?",
        "",
    ]
    for q in negatives:
        assert not is_status_query(q), f"should NOT be status-shaped: {q!r}"


def test_status_query_prioritizes_generated_chunks(monkeypatch):
    """Source-authority grounding: on a status question, generated chunks lead
    the kept list; if the rerank cut dropped them all, one is swapped in over
    the last slot. Non-status queries keep the rerank order untouched."""
    import app.database as database
    import app.rerank as rerank_mod

    gen = {"text": "next_up: dock retrofit", "source": "plan-summary",
           "score": 0.5, "auto_generated": True}
    n1 = {"text": "narrative a", "source": "docs/old-notes.md",
          "score": 0.9, "auto_generated": False}
    n2 = {"text": "narrative b", "source": "internal/PLAN.md",
          "score": 0.8, "auto_generated": False}

    # rerank passthrough (encoder isn't loaded in tests anyway; make it explicit)
    monkeypatch.setattr(rerank_mod, "rerank",
                        lambda q, cands, top_k=None, stats=None: cands[:(top_k or 2)])

    # Generated chunk survives the cut -> it must LEAD the kept list.
    monkeypatch.setattr(database, "query_similar",
                        lambda *a, **k: [n1, gen, n2])
    kept = rerank_mod.retrieve("What's next in my plans", top_k=2)
    assert kept[0]["source"] == "plan-summary"

    # Generated chunk cut by rerank (rank 3 of top_k=2) -> swapped in over the
    # last slot; still leads.
    monkeypatch.setattr(database, "query_similar",
                        lambda *a, **k: [n1, n2, gen])
    kept = rerank_mod.retrieve("What's next in my plans", top_k=2)
    assert kept[0]["source"] == "plan-summary"
    assert len(kept) == 2 and kept[1]["source"] == n1["source"]

    # Non-status query: order untouched even with a generated chunk present.
    kept = rerank_mod.retrieve("What is the expedited surcharge rate?", top_k=2)
    assert [c["source"] for c in kept] == [n1["source"], n2["source"]]


def test_followup_detection():
    from app.routing import is_followup

    positives = ["current", "Current?", "now", "latest", "recently", "status",
                 "update", "updates", "next", "what's next",
                 "where are we", "where do we stand", "more", "continue",
                 "go on", "keep going", "and", "why", "how so", "tell me more",
                 "what about that", "what about it", "explain more"]
    for q in positives:
        assert is_followup(q), f"should be a follow-up: {q!r}"

    negatives = ["What is the expedited surcharge rate?", "current status of the port",
                 "why did the reranker ship?", "tell me more about QLoRA",
                 "give me a general review of my project", "next task please",
                 ""]
    for q in negatives:
        assert not is_followup(q), f"should NOT be a follow-up: {q!r}"


def test_resolve_followup_reattaches_topic_and_triggers_status():
    from app.routing import resolve_followup, is_status_query

    history = [
        {"role": "user", "content": "give me a general review of my project"},
        {"role": "assistant", "content": "Here's the review ..."},
    ]
    # Bare "current" -> carries the topic AND routes as a status query so the
    # LIVE SYSTEM RECORD chunks outrank narrative (a live spot-check miss).
    resolved = resolve_followup("current", history)
    assert "general review of my project" in resolved
    assert is_status_query(resolved), f"expansion must route as status: {resolved!r}"

    # Generic continuation -> re-attaches topic, no status frame needed.
    resolved = resolve_followup("more", history)
    assert "general review of my project" in resolved and "more" in resolved


def test_resolve_followup_walks_back_past_chained_followups():
    """Chained drill-down: history stores the user's REAL words (only the retrieval
    query is rewritten), so the previous turn can itself be a bare follow-up. The walk
    must skip those and land on the last SUBSTANTIVE turn - otherwise the retrieval
    query is two contentless deictic tokens ("current - more")."""
    from app.routing import resolve_followup

    history = [
        {"role": "user", "content": "give me a general review of my project"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "current"},          # itself a bare follow-up
        {"role": "assistant", "content": "..."},
    ]
    resolved = resolve_followup("more", history)
    assert "general review of my project" in resolved, resolved
    assert not resolved.startswith("current -"), resolved


def test_resolve_followup_picks_the_most_recent_substantive_turn():
    """Multi-turn history is the NORMAL case - the LAST substantive user turn wins, not
    the first (guards the reversed() walk)."""
    from app.routing import resolve_followup

    history = [
        {"role": "user", "content": "how does the reranker work"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "tell me about the vector-store migration plan"},
        {"role": "assistant", "content": "..."},
    ]
    resolved = resolve_followup("current", history)
    assert "vector-store migration plan" in resolved, resolved
    assert "reranker" not in resolved, resolved


def test_resolve_followup_skips_blank_turns():
    """A blank/whitespace latest user turn is skipped in favour of the real topic."""
    from app.routing import resolve_followup

    history = [
        {"role": "user", "content": "the vector-store migration plan"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "   "},
    ]
    assert "vector-store migration plan" in resolve_followup("current", history)


def test_status_frame_covers_every_status_token():
    """EVERY _STATUS_FOLLOWUP_RE token must expand into a status-shaped query on a
    status topic - that frame is what lifts the LIVE SYSTEM RECORD chunks over stale
    narrative. A non-status continuation must NOT get the frame."""
    from app.routing import resolve_followup, is_status_query

    history = [{"role": "user", "content": "give me a general review of my project"}]
    for token in ["current", "now", "latest", "recent", "recently", "status",
                  "update", "updates", "next", "what's next", "where are we",
                  "where do we stand"]:
        resolved = resolve_followup(token, history)
        assert is_status_query(resolved), f"{token!r} -> {resolved!r} must be status-shaped"

    for token in ["more", "why", "tell me more", "continue"]:
        resolved = resolve_followup(token, history)
        assert not resolved.endswith("current status"), f"{token!r} -> {resolved!r}"


def test_status_frame_is_gated_on_the_topic_shape():
    """The 'current status' frame is right when the topic IS the project/plan and wrong
    on a specific factual topic - forcing it there makes the grounding rule float
    generated DB chunks over the narrative the user actually asked about."""
    from app.routing import resolve_followup, is_status_query, is_status_topic

    # REGRESSION GUARD for a live spot-check miss: this topic is NOT itself a status
    # QUESTION (is_status_query False - _STATUS_RE is noun-anchored), yet its bare
    # "current" follow-up must still route as status. Any narrowing of the topic gate
    # to is_status_query alone would silently undo the shipped fix.
    project = "give me a general review of my project"
    assert not is_status_query(project)
    assert is_status_topic(project)
    assert is_status_query(resolve_followup("current", [{"role": "user", "content": project}]))

    # Factual/technical topic -> neutral form, topic terms stay dominant, no frame.
    tech = "how does the cross-encoder reranker work"
    assert not is_status_topic(tech)
    resolved = resolve_followup("latest", [{"role": "user", "content": tech}])
    assert resolved == f"{tech} - latest", resolved
    assert not is_status_query(resolved), resolved


def test_resolve_followup_is_fail_open():
    from app.routing import resolve_followup

    history = [{"role": "user", "content": "review my project"}]
    # Not a follow-up -> unchanged.
    q = "What is the expedited surcharge rate?"
    assert resolve_followup(q, history) == q
    # No prior user turn -> unchanged (nothing to resolve against).
    assert resolve_followup("current", []) == "current"
    assert resolve_followup("current", None) == "current"
    assert resolve_followup("current", [{"role": "assistant", "content": "hi"}]) == "current"

    # Pydantic-style Message objects (attribute access), not just dicts.
    class M:
        def __init__(self, role, content):
            self.role, self.content = role, content
    resolved = resolve_followup("current", [M("user", "the vector-store migration")])
    assert "vector-store migration" in resolved


def test_format_context_marks_generated_chunks():
    from app.rerank import format_context

    ctx = format_context([
        {"text": "next_up: dock retrofit", "source": "plan-summary", "auto_generated": True},
        {"text": "old prose", "source": "docs/old-notes.md"},
    ])
    assert "[LIVE SYSTEM RECORD" in ctx and "plan-summary" in ctx
    assert "[docs/old-notes.md]\nold prose" in ctx
    # generated marker must NOT leak onto narrative chunks
    assert ctx.index("LIVE SYSTEM RECORD") < ctx.index("docs/old-notes.md")


def test_retrieve_adds_routed_department(monkeypatch):
    """The router must apply INSIDE retrieve() (shared by chat + eval), and it must
    ADD to the caller's department, never replace it."""
    import app.database as database
    import app.rerank as rerank

    captured = {}

    def fake_query_similar(query, n_results=5, department=None):
        captured["department"] = department
        return []

    monkeypatch.setattr(database, "query_similar", fake_query_similar)

    # history is routed in; `restricted` is floored in for Owner (default level,
    # no user_level == Owner). The router ADDS, never replaces.
    rerank.retrieve("What happened last session?")
    assert captured["department"] == ["history", "restricted"]

    rerank.retrieve("What happened last session?", department="finance")
    assert captured["department"] == ["finance", "history", "restricted"]

    # A plain fact query routes nothing, but Owner still floors `restricted`
    # (the internal docs) into the pool - general/global is always queried by
    # query_similar itself, so the docs are the only always-on add here.
    rerank.retrieve("What is the expedited surcharge rate?")
    assert captured["department"] == ["restricted"]

    rerank.retrieve("What is the expedited surcharge rate?", department="finance")
    assert captured["department"] == ["finance", "restricted"]
