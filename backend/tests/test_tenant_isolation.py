"""Tenant isolation.

Conversation history: one owner's chat history must be invisible and
undeletable to another - even if they know the session id. Verified at the
_scope function level (the security primitive) and at the /api/history
endpoints.

KB retrieval (access tiers): a lower tier must not retrieve higher-tier KB
content into an answer. The failure class pinned here: query-ROUTED history
injection (the Owner-only session log pulled back into the pool for
history-shaped questions) once bypassed department scoping for EVERY caller.
Verified at the retrieve() function level - which departments a given
clearance level may ask the DB for - since the test harness mocks Chroma,
that seam IS the security primitive (mirrors how _scope is proved).
"""
import pytest

import app.history as h


def test_scope_isolates_history_by_owner():
    sess = "iso-fn-sess"
    # Same session string, three owners: user 9001, user 9002, and a guest (NULL).
    h.save_message(sess, "user", "alice-secret", user_id=9001)
    h.save_message(sess, "user", "bob-secret", user_id=9002)
    h.save_message(sess, "user", "guest-note", user_id=None)

    # Each owner reads ONLY their own rows.
    assert [m["content"] for m in h.load_history(sess, 9001)] == ["alice-secret"]
    assert [m["content"] for m in h.load_history(sess, 9002)] == ["bob-secret"]
    assert [m["content"] for m in h.load_history(sess, None)] == ["guest-note"]

    # Session listing is owner-scoped: a user with no rows here doesn't see it;
    # the operator override (all_users) does.
    assert not any(s["session"] == sess for s in h.list_sessions(user_id=9003))
    assert any(s["session"] == sess for s in h.list_sessions(all_users=True))

    # Clear is owner-scoped: clearing alice leaves bob + guest intact.
    h.clear_session(sess, 9001)
    assert h.load_history(sess, 9001) == []
    assert [m["content"] for m in h.load_history(sess, 9002)] == ["bob-secret"]
    assert [m["content"] for m in h.load_history(sess, None)] == ["guest-note"]

    # delete_tail is owner-scoped: deleting bob's tail never touches the guest row.
    h.save_message(sess, "user", "bob-2", user_id=9002)
    h.delete_tail_messages(sess, 5, user_id=9002)
    assert h.load_history(sess, 9002) == []
    assert [m["content"] for m in h.load_history(sess, None)] == ["guest-note"]


def test_history_endpoints_require_auth(client):
    # Private per-user data: no token, no history - route-level guard, so it
    # holds even with the auth middleware disabled.
    assert client.get("/api/history/whatever").status_code == 401
    assert client.delete("/api/history/whatever").status_code == 401
    assert client.delete("/api/history/whatever/tail").status_code == 401


def test_history_endpoint_is_owner_scoped(client, admin_headers):
    admin_id = client.get("/api/auth/me", headers=admin_headers).json()["id"]
    sess = "iso-ep-sess"
    h.save_message(sess, "user", "admin-only-secret", user_id=admin_id)

    # A second, distinct user...
    client.post("/api/users",
                json={"username": "bob_iso", "password": "BobPass1", "role": "member"},
                headers=admin_headers)
    bob = client.post("/api/auth/login",
                      json={"username": "bob_iso", "password": "BobPass1"})
    assert bob.status_code == 200, bob.text
    bob_headers = {"Authorization": f"Bearer {bob.json()['access_token']}"}

    # ...cannot READ the admin's session even knowing its id.
    admin_view = client.get(f"/api/history/{sess}", headers=admin_headers).json()
    bob_view = client.get(f"/api/history/{sess}", headers=bob_headers).json()
    assert any(m["content"] == "admin-only-secret" for m in admin_view["messages"])
    assert bob_view["messages"] == []

    # ...and cannot DELETE it (admin's rows survive bob's delete attempt).
    client.delete(f"/api/history/{sess}", headers=bob_headers)
    still = client.get(f"/api/history/{sess}", headers=admin_headers).json()
    assert any(m["content"] == "admin-only-secret" for m in still["messages"])


# -- Access-tier retrieval isolation ------------------------------------------
import app.rerank as rerank
from app.permissions import (
    effective_level, GUEST_LEVEL, MEMBER_LEVEL, ADMIN_LEVEL, OWNER_LEVEL,
)
from app.rag_config import department_min_level


def test_effective_level_ladder():
    """The clearance ladder Owner > Admin > Member > Guest, with fail-closed
    defaults for anything off the ladder."""
    assert effective_level(None) == GUEST_LEVEL                 # no account
    assert effective_level({"role": "guest"}) == GUEST_LEVEL
    assert effective_level({"role": "member"}) == MEMBER_LEVEL
    assert effective_level({"role": "admin"}) == ADMIN_LEVEL    # Admin is NOT superuser
    assert effective_level({"role": "owner"}) == OWNER_LEVEL
    # An unrecognized present role resolves to the LOWEST authenticated rung,
    # never higher - unknown input fails closed toward less privilege.
    assert effective_level({"role": "superuser"}) == MEMBER_LEVEL
    assert effective_level({"role": "who_dis"}) == MEMBER_LEVEL
    # An authed record with the role field missing defaults to Member too.
    assert effective_level({"username": "x"}) == MEMBER_LEVEL
    # The ladder is strictly ordered - the whole point of "higher sees lower".
    assert GUEST_LEVEL < MEMBER_LEVEL < ADMIN_LEVEL < OWNER_LEVEL


def test_department_min_level_fail_closed():
    assert department_min_level(None) == 0        # shared floor
    assert department_min_level("general") == 0
    assert department_min_level("history") == OWNER_LEVEL     # Owner-only session log
    assert department_min_level("restricted") == OWNER_LEVEL  # Owner-only internal docs
    # A brand-new, unlisted department is PRIVATE by default (Owner-only) -
    # sharing is opt-in, not opt-out.
    assert department_min_level("finance_secret") == OWNER_LEVEL


def _spy_departments(monkeypatch) -> dict:
    """Patch query_similar (the DB seam retrieve() calls) to capture exactly which
    departments a given clearance level asks for, and return no rows."""
    captured = {"departments": []}
    def spy(query, n_results=5, department=None):
        captured["departments"] = list(department or [])
        return []
    monkeypatch.setattr("app.database.query_similar", spy)
    return captured


def test_retrieve_gate_blocks_higher_tier_departments(monkeypatch):
    """The security primitive: retrieve() never asks the DB for a department above
    the caller's level - the caller's OWN department or a query-ROUTED one."""
    seen = _spy_departments(monkeypatch)

    # Owner asking a history-shaped question -> history IS routed in (recall kept).
    rerank.retrieve("what did we do last session?", user_level=OWNER_LEVEL)
    assert "history" in seen["departments"]

    # Member asking the SAME question -> history is dropped at the gate.
    rerank.retrieve("what did we do last session?", user_level=MEMBER_LEVEL)
    assert "history" not in seen["departments"]

    # A Member whose OWN department is a private (unlisted) one can't reach it;
    # an Owner can.
    rerank.retrieve("anything", department="finance_secret", user_level=MEMBER_LEVEL)
    assert "finance_secret" not in seen["departments"]
    rerank.retrieve("anything", department="finance_secret", user_level=OWNER_LEVEL)
    assert "finance_secret" in seen["departments"]


def test_retrieve_history_injection_isolation_measured(monkeypatch):
    """The measured isolation number. Across every history-routed probe: a
    Member-level retrieve leaks the Owner-only history collection ZERO times; an
    Owner retrieves it every time (recall preserved). This is the canary for the
    routing-injection bypass class - query-routed history once reached every
    caller because routing ran outside the department gate."""
    seen = _spy_departments(monkeypatch)
    probes = [
        "what did we do last week?",
        "what happened in the 2025-06-01 session?",
        "recent build history",
        "what changed since mid-June?",
        "root cause of the last outage",
    ]
    member_leaks = owner_hits = 0
    for p in probes:
        rerank.retrieve(p, user_level=MEMBER_LEVEL)
        member_leaks += "history" in seen["departments"]
        rerank.retrieve(p, user_level=OWNER_LEVEL)
        owner_hits += "history" in seen["departments"]

    n = len(probes)
    assert member_leaks == 0, f"Member retrieval leaked history on {member_leaks}/{n} probes"
    assert owner_hits == n, f"Owner lost history recall on {n - owner_hits}/{n} probes"


def test_default_user_level_is_full_access_for_internal_callers(monkeypatch):
    """Trusted callers (eval, offline scripts) omit user_level and must get full
    access - otherwise the eval would silently stop measuring the history pool.
    Absence of a level == Owner, by design."""
    seen = _spy_departments(monkeypatch)
    rerank.retrieve("what did we do last session?")   # no user_level
    assert "history" in seen["departments"]


def test_chat_endpoint_passes_caller_level_to_retrieve(client, admin_headers, monkeypatch):
    """Wiring proof (positive signal): the /api/chat path must hand retrieve() the
    caller's REAL clearance level - a gate that's never called is no gate. We spy
    at the retrieve seam and stop before the LLM runs."""
    captured = {}
    class _Stop(Exception):
        pass
    def spy(query, department=None, top_k=None, user_level=None, stats=None):
        captured["user_level"] = user_level
        raise _Stop()
    monkeypatch.setattr("app.rerank.retrieve", spy)

    # A Member-level account.
    client.post("/api/users",
                json={"username": "member_iso", "password": "MemberP1", "role": "member"},
                headers=admin_headers)
    tok = client.post("/api/auth/login",
                      json={"username": "member_iso", "password": "MemberP1"}).json()["access_token"]
    member_headers = {"Authorization": f"Bearer {tok}"}

    try:
        client.post("/api/chat",
                    json={"prompt": "what did we do last week?", "use_rag": True},
                    headers=member_headers)
    except _Stop:
        pass  # expected: we intentionally abort inside the spy, before any LLM call
    assert captured.get("user_level") == MEMBER_LEVEL


# -- Tiered eval cohort + tool-path isolation ----------------------------------
import app.agent as agent


def test_tool_path_gate_blocks_history_for_low_tier(tmp_path, monkeypatch):
    """The file tools are a SECOND retrieval surface: read_file could hand a lower
    tier the Owner-only session log straight past retrieve()'s department gate.
    They must enforce the SAME department clearance retrieval does
    (dept_for_source -> department_min_level). None == Owner, so trusted internal
    callers are unchanged."""
    ws = tmp_path
    (ws / "internal").mkdir()
    (ws / "internal" / "session-log.md").write_text("OWNER-ONLY SECRET LOG", encoding="utf-8")
    (ws / "README.md").write_text("public readme", encoding="utf-8")
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", ws.resolve())

    log = {"path": "internal/session-log.md"}

    # Member (L1): the Owner-only log is DENIED before its content is ever read...
    denied = agent.execute_tool("read_file", log, user_level=MEMBER_LEVEL)
    assert denied.startswith("Permission denied")
    assert "SECRET" not in denied
    # ...but a general (level-0) file is fine.
    assert agent.execute_tool("read_file", {"path": "README.md"},
                              user_level=MEMBER_LEVEL) == "public readme"

    # Owner (L3) and the internal default (None == Owner) get the log.
    assert agent.execute_tool("read_file", log, user_level=OWNER_LEVEL) == "OWNER-ONLY SECRET LOG"
    assert agent.execute_tool("read_file", log) == "OWNER-ONLY SECRET LOG"

    # Guest (L0) is denied too.
    assert agent.execute_tool("read_file", log, user_level=GUEST_LEVEL).startswith("Permission denied")

    # write_file to an Owner-only path is denied for a Member (no tampering either).
    assert agent.execute_tool("write_file", {"path": "internal/session-log.md", "content": "x"},
                              user_level=MEMBER_LEVEL).startswith("Permission denied")
    assert (ws / "internal" / "session-log.md").read_text(encoding="utf-8") == "OWNER-ONLY SECRET LOG"


def test_tool_path_search_and_list_hide_higher_tier(tmp_path, monkeypatch):
    """A Member's search/list must not surface Owner-only files by NAME. The
    internal/ subtree is restricted (and the session log inside it is history) -
    a general (root) file stays visible."""
    ws = tmp_path
    (ws / "internal").mkdir()
    (ws / "internal" / "session-log.md").write_text("secret", encoding="utf-8")
    (ws / "internal" / "runbook.md").write_text("internal runbook", encoding="utf-8")
    (ws / "faq.md").write_text("public", encoding="utf-8")   # general (root) file
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", ws.resolve())

    member_hits = agent.execute_tool("search_files", {"pattern": "**/*.md"}, user_level=MEMBER_LEVEL)
    assert "session-log.md" not in member_hits      # history, Owner-only
    assert "runbook.md" not in member_hits          # internal/* restricted
    assert "faq.md" in member_hits                  # general stays visible
    owner_hits = agent.execute_tool("search_files", {"pattern": "**/*.md"}, user_level=OWNER_LEVEL)
    assert "session-log.md" in owner_hits and "runbook.md" in owner_hits

    # list_directory of internal/: a Member sees nothing (all Owner-only);
    # Owner sees both, with the "[file] " prefix the tool renders.
    member_ls = agent.execute_tool("list_directory", {"path": "internal"}, user_level=MEMBER_LEVEL)
    assert "session-log.md" not in member_ls and "runbook.md" not in member_ls
    owner_ls = agent.execute_tool("list_directory", {"path": "internal"}, user_level=OWNER_LEVEL)
    assert "[file] session-log.md" in owner_ls and "[file] runbook.md" in owner_ls


def test_eval_job_runs_question_at_its_as_level(monkeypatch):
    """Wiring proof: _run_eval_job hands retrieve() the QUESTION's as_level, so a
    tier-isolation question is actually measured at Member/Guest clearance (a gate
    the low level never reaches is no gate). Spy at the retrieve seam."""
    import app.main as main_mod
    captured = []

    def spy(query, top_k=None, user_level=None):
        captured.append(user_level)
        return []

    monkeypatch.setattr("app.rerank.retrieve", spy)
    monkeypatch.setattr("time.sleep", lambda s: None)

    questions = [
        {"id": 1, "question": "member q", "category": "tier-isolation",
         "expected_source": None, "notes": "n", "as_level": MEMBER_LEVEL},
        {"id": 2, "question": "guest q", "category": "tier-isolation",
         "expected_source": None, "notes": "n", "as_level": GUEST_LEVEL},
        {"id": 3, "question": "normal q", "category": "general",
         "expected_source": None, "notes": "n", "as_level": None},
    ]
    main_mod._run_eval_job("tier-run", "2026-01-01", questions, model="m",
                           use_rag=True, n_results=5, retrieval_only=True)

    # Each question retrieved at its OWN clearance; the normal one at Owner (None).
    assert captured == [MEMBER_LEVEL, GUEST_LEVEL, None]


# -- Internal-docs classification + floor --------------------------------------
# The lesson behind `restricted`: the operational story DUPLICATES out of the
# session log into other internal docs (plans, runbooks, audits). Gating only
# the log leaves the copies world-readable - so the whole internal/ subtree is
# `restricted` (Owner-only), floored into the pool for Owner so recall is
# unchanged, gated for lower tiers.
from app.rag_config import dept_for_source


def test_internal_docs_classified_restricted():
    # The session log itself -> history (routing-only, Owner-only).
    assert dept_for_source("internal/session-log.md") == "history"
    # Everything else under internal/ -> restricted (the whole subtree, so a
    # copy of operational content is gated wherever it lands in the tree).
    assert dept_for_source("internal/runbook.md") == "restricted"
    assert dept_for_source("internal/audits/scan-results.md") == "restricted"
    assert dept_for_source("internal/plans/roadmap.md") == "restricted"
    # The public floor stays general.
    assert dept_for_source("faq.md") == "general"
    assert dept_for_source("onboarding.md") == "general"
    assert dept_for_source("docs/overview.md") == "general"
    # A sibling path that merely STARTS with the word "internal" is not inside
    # the subtree (prefix match is path-segment-shaped, "internal/").
    assert dept_for_source("internal-notes-public.md") == "general"
    # restricted is Owner-only.
    assert department_min_level("restricted") == OWNER_LEVEL
    assert department_min_level("restricted") > MEMBER_LEVEL


def test_eval_applies_non_owner_answer_gate_by_as_level(monkeypatch):
    """Answer-layer gate: a below-Owner as_level gets the non-owner rules in its
    system prompt; an Owner (as_level None) does not. Retrieval alone can't close
    the leak (operational history bleeds into general docs), so the answer layer
    refuses the behavior for low tiers."""
    import app.main as main_mod
    import app.eval_judge as judge_mod
    calls: list[dict] = []

    def _cap(msgs, model, tools=None, system_prompt="", max_tokens=1024):
        # The gate rides the system-role MESSAGE (what every provider serves);
        # the system_prompt kwarg is the tier-independent prompt-cache core
        # and must NOT carry it.
        sys_msg = next((m["content"] for m in msgs if m["role"] == "system"), "")
        calls.append({"sys_msg": sys_msg, "core": system_prompt})
        yield "ok"

    def _judge(msgs, model, tools=None, system_prompt="", max_tokens=1024):
        yield '{"pass": true, "rationale": "x"}'

    monkeypatch.setattr("app.rerank.retrieve", lambda q, top_k=None, user_level=None: [])
    monkeypatch.setattr(main_mod, "stream_chat", _cap)
    monkeypatch.setattr(main_mod, "supports_tools", lambda m="": False)
    monkeypatch.setattr(judge_mod, "stream_chat", _judge)
    monkeypatch.setattr("time.sleep", lambda s: None)

    qs = [
        {"id": 1, "question": "member q", "category": "tier-isolation",
         "expected_source": None, "notes": "n", "as_level": MEMBER_LEVEL},
        {"id": 2, "question": "owner q", "category": "general",
         "expected_source": None, "notes": "n", "as_level": None},
    ]
    main_mod._run_eval_job("gate-run", "2026-01-02", qs, model="m",
                           use_rag=True, n_results=5, retrieval_only=False)
    # One answer stream_chat call per question, in order.
    assert len(calls) == 2
    assert "ACCESS TIER: NON-OWNER" in calls[0]["sys_msg"], "Member did not get the non-owner gate"
    assert "ACCESS TIER: NON-OWNER" not in calls[1]["sys_msg"], "Owner wrongly got the non-owner gate"
    # Prompt-cache contract: both tiers share ONE cached core; the tier suffix
    # only ever appends after it (providers splits it into the uncached tail).
    assert calls[0]["core"] == calls[1]["core"]
    assert "ACCESS TIER: NON-OWNER" not in calls[0]["core"]
    assert calls[0]["sys_msg"].startswith(calls[0]["core"])


def test_restricted_floored_for_owner_gated_for_lower(monkeypatch):
    """restricted (internal docs) is in the pool for Owner on ANY query shape
    (floored, not routing-dependent) and NEVER for Member/Guest."""
    seen = _spy_departments(monkeypatch)

    # A plain fact-shaped query - nothing routed - still floors restricted for Owner.
    rerank.retrieve("what is the reranker model", user_level=OWNER_LEVEL)
    assert "restricted" in seen["departments"]
    rerank.retrieve("what is the reranker model", user_level=MEMBER_LEVEL)
    assert "restricted" not in seen["departments"]
    rerank.retrieve("what is the reranker model", user_level=GUEST_LEVEL)
    assert "restricted" not in seen["departments"]

    # Internal/trusted callers (no level) == Owner, full recall.
    rerank.retrieve("what is the reranker model")
    assert "restricted" in seen["departments"]


def test_restricted_measured_no_leak_across_probes(monkeypatch):
    """Measured floor guarantee across a spread of query shapes: a Member never
    gets restricted in the pool; an Owner always does."""
    seen = _spy_departments(monkeypatch)
    probes = [
        "what did we work on recently",
        "recap the operational progress notes",
        "what does this assistant do and what changed last session",
        "give me an overview of the project and its status",
        "what did we decide about the dock retrofit",
    ]
    member_leaks = owner_hits = 0
    for p in probes:
        rerank.retrieve(p, user_level=MEMBER_LEVEL)
        member_leaks += "restricted" in seen["departments"]
        rerank.retrieve(p, user_level=OWNER_LEVEL)
        owner_hits += "restricted" in seen["departments"]
    n = len(probes)
    assert member_leaks == 0, f"Member pool leaked restricted on {member_leaks}/{n}"
    assert owner_hits == n, f"Owner lost restricted recall on {n - owner_hits}/{n}"
