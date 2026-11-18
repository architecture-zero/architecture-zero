"""The retrieval pass measures each question at its own clearance, and a run's
record says when a failed provider call left a pool to the local model
(2026-11-17).

Two gaps between what scripts/eval_retrieval.py's cohort pass measured and
what it said:

1. Clearance. A question can be asked AS a clearance level (as_level: Member
   1, Guest 0; None is Owner). The answer run retrieves at that level
   (eval_runner: retrieve(..., user_level=q.get("as_level"))). The retrieval
   pass called retrieve(query, top_k=top_k) with no level, so a row stamped
   as_level was retrieved at Owner clearance there and at its own clearance
   in the answer run: the two passes measured different systems for it.

2. The provider fallback. Under rerank_provider=hosted-api (or remote-http) a
   scoring call that fails (an error, a timeout, a missing key) falls back to
   the local model, and the turn's receipt says rerank_provider=
   "local-fallback" with no rerank_hosted_skipped (that key is the tier
   rule's alone). The run counted the tier rule's pools and not these, so a
   pool the local model scored because the vendor failed went on record
   under the vendor's model.

The real run_cohort runs; the corpus, the vendor, the remote scorer and the
local model are stood in for, so no index, no request and no model load:
  FIX      a row with as_level 0 or 1 reaches retrieve with that user_level.
  FIX      through the real retrieve, a Guest row's pool never asks the floor
           department, and an Owner row's does.
  CONTROL  a row with as_level None reaches retrieve with user_level None.
  FIX      under hosted-api, the pools the vendor failed on count as vendor
           fallback, printed beside the tag.
  FIX      a run with both: the tier rule and the fallback, each counted as
           itself.
  FIX      under remote-http, a scorer that fails mid-run counts as remote
           fallback.
  CONTROL  a vendor-served pool counts nothing.
  CONTROL  a pool the tier rule kept off the vendor counts as the tier rule,
           not as a fallback.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")))

import eval_retrieval as ev  # noqa: E402
import app.database as database  # noqa: E402
import app.rag_config as rc  # noqa: E402
import app.rerank as rr  # noqa: E402

PUBLIC = database.GLOBAL_COLLECTION      # the global collection: the public tier
DEPT = "kb_restricted"                   # a floor department: not public
HOSTED_MODEL = rr._HOSTED["cohere"]["default_model"]
LOCAL_MODEL = rr.RERANK_MODEL_DEFAULT

Q1 = "Where is the onboarding guide?"
Q2 = "Who approves a new department?"
Q3 = "What does the backup job cover?"
QR = "How is a restricted document kept apart?"
# One pool per question, by question text: three candidates, each from its
# own source so the per-source cap keeps every one.
ALL_PUBLIC = {q: (PUBLIC, PUBLIC, PUBLIC) for q in (Q1, Q2, Q3)}
WITH_DEPT = {**ALL_PUBLIC, QR: (DEPT, DEPT, PUBLIC)}


def _questions(levels, category="tier-isolation"):
    return [types.SimpleNamespace(id=i, question=f"Where is document {i}?",
                                  category=category, expected_source="docs/0.md",
                                  as_level=lvl, setup_turns=None)
            for i, lvl in enumerate(levels, start=1)]


def _no_reranker(monkeypatch):
    cfg = {"rerank_enabled": "false"}
    monkeypatch.setattr(rr, "_cfg", lambda k, d: cfg.get(k, d))
    monkeypatch.setattr("time.sleep", lambda s: None)


# -- 1. clearance ---------------------------------------------------------------------

def _captured(monkeypatch, capsys, levels):
    """run_cohort over one row per level, retrieve stood in for: returns the
    keyword arguments each row reached retrieve with."""
    _no_reranker(monkeypatch)
    seen: list[dict] = []

    def retrieve(query, **kw):
        seen.append(kw)
        return [{"source": "docs/0.md", "text": "x"}]

    monkeypatch.setattr(ev, "retrieve", retrieve)
    out = ev.run_cohort(_questions(levels), top_k=3, misses_only=True)
    capsys.readouterr()
    assert out is not None and out["scored"] == len(levels)
    return seen


@pytest.mark.parametrize("level", [0, 1], ids=["guest", "member"])
def test_fix_a_row_with_a_level_is_retrieved_at_that_level(monkeypatch, capsys, level):
    seen = _captured(monkeypatch, capsys, [level])
    assert "user_level" in seen[0], "the pass passed no clearance at all"
    assert seen[0]["user_level"] == level


def test_control_a_row_with_no_level_is_retrieved_at_owner(monkeypatch, capsys):
    seen = _captured(monkeypatch, capsys, [None])
    assert seen[0].get("user_level") is None


def test_fix_through_the_real_retrieve_a_guest_pool_skips_the_floor_department(
        monkeypatch, capsys):
    """The clearance reaches the access-tier gate: the floor department
    (restricted, Owner-only) is asked for the Owner row and never for the
    Guest row - the answer run's own pools for those rows."""
    _no_reranker(monkeypatch)
    asked: list = []

    def query_similar(query, n_results=5, department=None, **kw):
        asked.append(department)
        return [{"text": f"{query} chunk", "source": "docs/0.md",
                 "collection": PUBLIC, "score": 0.9}]

    monkeypatch.setattr(database, "query_similar", query_similar)
    out = ev.run_cohort(_questions([0, None]), top_k=3, misses_only=True)
    capsys.readouterr()
    assert out is not None and out["scored"] == 2
    guest, owner = asked
    assert "restricted" not in (guest or []), "a Guest row's pool asked an Owner-only department"
    assert "restricted" in (owner or [])


# -- 2. the provider fallback -----------------------------------------------------------

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Post:
    """Stands in for requests.post, the vendor's and the remote scorer's both:
    records each call and ranks last to first, and raises for the queries in
    `fail` - the call that times out mid-run."""
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, url, **kw):
        body = kw["json"]
        if body["query"] in self.fail:
            raise TimeoutError("the scoring call timed out")
        docs = body.get("documents", body.get("texts"))
        self.calls.append(docs)
        if "documents" in body:
            return _Resp({"results": [{"index": i, "relevance_score": float(i)}
                                      for i in range(len(docs))]})
        return _Resp({"scores": [float(i) for i in range(len(docs))]})


class _Len:
    """The local model, stood in for: it scores by length."""
    def rerank(self, query, docs):
        return [float(len(d)) for d in docs]


def _run(monkeypatch, capsys, pools: dict, provider: str, fail=()):
    """One retrieval pass over `pools` on `provider`, the hosted arm opened as
    a deployer opens it (the provider key, the host latch, the vendor's key)
    and the remote arm given its url. Returns the run's result and report."""
    cfg = {"rerank_enabled": "true", "rerank_provider": provider,
           "rerank_remote_url": "http://scorer.test/rerank"}
    monkeypatch.setattr(rr, "_cfg", lambda k, d: cfg.get(k, d))
    monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", True)
    monkeypatch.setenv("COHERE_API_KEY", "test-key")
    post = _Post(fail)
    monkeypatch.setattr(rr, "requests", type("Requests", (), {"post": post}))
    monkeypatch.setattr(rr, "_get_encoder", lambda model=None: _Len())

    def query_similar(query, n_results=5, department=None, **kw):
        return [{"text": f"{query} chunk {i} " + "x" * i, "source": f"docs/{i}.md",
                 "collection": col, "score": 0.9}
                for i, col in enumerate(pools[query])]

    monkeypatch.setattr(database, "query_similar", query_similar)
    monkeypatch.setattr("time.sleep", lambda s: None)
    questions = [types.SimpleNamespace(id=i, question=q, expected_source="docs/0.md",
                                       category="general", as_level=None,
                                       setup_turns=None)
                 for i, q in enumerate(pools, start=1)]
    real_rerank = rr.rerank
    out = ev.run_cohort(questions, top_k=3, misses_only=True)
    assert rr.rerank is real_rerank, "the script puts the reranker back"
    assert out is not None and out["scored"] == len(pools)
    return out, capsys.readouterr().out


def _tag(provider="hosted-api"):
    if provider == "hosted-api":
        return f"reranker: {HOSTED_MODEL} via hosted-api (cohere); "
    return f"reranker: {LOCAL_MODEL} via {provider}; "


def test_fix_a_hosted_run_records_the_pools_the_vendor_failed_on(monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, ALL_PUBLIC, "hosted-api", fail=(Q2, Q3))
    # Every pool was public, so the tier rule moved none ...
    assert out["tier_rule_local"] == 0 and out["tier_rule_pools"] == 3
    # ... but the vendor failed on two, and the local model scored them.
    assert out["fallback_local"] == 2
    assert (_tag() + f"vendor fallback: 2 of 3 pools scored locally ({LOCAL_MODEL})"
            in report)
    assert "tier rule" not in report


def test_fix_the_tier_rule_and_the_fallback_are_each_counted_as_themselves(
        monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, WITH_DEPT, "hosted-api", fail=(Q1,))
    assert out["tier_rule_pools"] == 4
    assert out["tier_rule_local"] == 1        # the pool that held a department chunk
    assert out["fallback_local"] == 1         # the public pool the vendor failed on
    assert (_tag()
            + f"tier rule: 1 of 4 pools scored locally ({LOCAL_MODEL}); "
              f"vendor fallback: 1 of 4 pools scored locally ({LOCAL_MODEL})"
            in report)


def test_fix_a_remote_run_records_the_pools_the_scorer_failed_on(monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, ALL_PUBLIC, "remote-http", fail=(Q1,))
    assert out["tier_rule_local"] == 0
    assert out["fallback_local"] == 1
    assert (_tag("remote-http")
            + f"remote fallback: 1 of 3 pools scored locally ({LOCAL_MODEL})"
            in report)


def test_control_a_vendor_served_run_counts_nothing(monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, ALL_PUBLIC, "hosted-api")
    assert out.get("fallback_local", 0) == 0
    assert out.get("tier_rule_local", 0) == 0
    assert "fallback:" not in report and "tier rule" not in report


def test_control_a_tier_rule_skip_counts_as_the_tier_rule_not_a_fallback(
        monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, WITH_DEPT, "hosted-api")
    assert out["tier_rule_local"] == 1 and out["tier_rule_pools"] == 4
    assert out.get("fallback_local", 0) == 0
    assert "fallback:" not in report
    assert (_tag() + f"tier rule: 1 of 4 pools scored locally ({LOCAL_MODEL})"
            in report)
