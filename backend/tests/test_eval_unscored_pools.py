"""A pool nothing scored is counted as unscored on a run's record, never as
the tier rule's local scoring (2026-11-17).

rerank()'s receipt names who served a pool: the provider in effect,
"local-fallback" when the fallback chain engaged, "none" when it ran out and
the pool kept retriever order. Only when the tier rule kept the pool off the
vendor does it also hold rerank_hosted_skipped - and it holds it whether or
not the local model then scored the pool.

scripts/eval_retrieval.py's cohort pass read rerank_hosted_skipped first, so
a pool the tier rule kept off the vendor whose local scoring then failed was
counted as "scored locally" - a pool nothing scored, on record as scored. And
a pool whose chain ran out with no tier reason (the provider failed, then the
local model) was counted nowhere, nor was a local-provider pool whose model
failed. A run whose reranker silently stopped ranking read like one that
ranked.

The real run_cohort runs; the corpus, the vendor and the local model are
stood in for, so no index, no request and no model load:
  FIX      under hosted-api, a pool the tier rule kept off the vendor whose
           local scoring failed is unscored, not the tier rule's.
  FIX      under hosted-api, a public pool the vendor and then the local
           model failed on is unscored.
  FIX      under local, a pool the model failed on is unscored.
  CONTROL  a vendor-served run counts nothing.
  CONTROL  a tier-rule skip the local model scored counts as the tier rule.
  CONTROL  a vendor failure the local model scored counts as the fallback.
"""
import os
import sys
import types

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
ALL_PUBLIC = {q: (PUBLIC, PUBLIC, PUBLIC) for q in (Q1, Q2, Q3)}
WITH_DEPT = {**ALL_PUBLIC, QR: (DEPT, DEPT, PUBLIC)}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Post:
    """Stands in for the vendor's requests.post: ranks last to first, and
    raises for the queries in `fail` - the call that times out mid-run."""
    def __init__(self, fail=()):
        self.fail = set(fail)

    def __call__(self, url, **kw):
        body = kw["json"]
        if body["query"] in self.fail:
            raise TimeoutError("the scoring call timed out")
        docs = body["documents"]
        return _Resp({"results": [{"index": i, "relevance_score": float(i)}
                                  for i in range(len(docs))]})


class _Len:
    """The local model, stood in for: it scores by length, and raises for the
    queries in `fail` - a model that errors mid-run."""
    def __init__(self, fail=()):
        self.fail = set(fail)

    def rerank(self, query, docs):
        if query in self.fail:
            raise RuntimeError("the local model failed")
        return [float(len(d)) for d in docs]


def _run(monkeypatch, capsys, pools: dict, provider: str, vendor_fail=(),
         local_fail=()):
    """One retrieval pass over `pools` on `provider`, the hosted arm opened as
    a deployer opens it. Returns the run's result and its report."""
    cfg = {"rerank_enabled": "true", "rerank_provider": provider}
    monkeypatch.setattr(rr, "_cfg", lambda k, d: cfg.get(k, d))
    monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", True)
    monkeypatch.setenv("COHERE_API_KEY", "test-key")
    monkeypatch.setattr(rr, "requests", type("Requests", (), {"post": _Post(vendor_fail)}))
    local = _Len(local_fail)
    monkeypatch.setattr(rr, "_get_encoder", lambda model=None: local)

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


def test_fix_a_tier_skip_the_local_model_failed_on_is_unscored(monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, WITH_DEPT, "hosted-api", local_fail=(QR,))
    assert out["tier_rule_pools"] == 4
    # The tier rule kept QR's pool off the vendor, and the local model then
    # failed on it: nothing scored it, so it is not "scored locally".
    assert out["tier_rule_local"] == 0, "a pool nothing scored was counted as scored locally"
    assert out["rerank_unscored"] == 1
    assert out["fallback_local"] == 0
    assert _tag() + "unscored: 1 of 4 pools kept in retriever order" in report
    assert "tier rule" not in report


def test_fix_a_public_pool_the_vendor_and_the_local_model_failed_on_is_unscored(
        monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, ALL_PUBLIC, "hosted-api",
                       vendor_fail=(Q1, Q2), local_fail=(Q1,))
    assert out["tier_rule_local"] == 0
    assert out["fallback_local"] == 1         # Q2: the local model scored it
    assert out["rerank_unscored"] == 1        # Q1: nothing scored it
    assert (_tag()
            + f"vendor fallback: 1 of 3 pools scored locally ({LOCAL_MODEL}); "
              f"unscored: 1 of 3 pools kept in retriever order"
            in report)


def test_fix_a_local_run_records_the_pools_its_model_failed_on(monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, ALL_PUBLIC, "local", local_fail=(Q2,))
    assert out["tier_rule_pools"] == 3
    assert out["rerank_unscored"] == 1
    assert out["fallback_local"] == 0 and out["tier_rule_local"] == 0
    assert _tag("local") + "unscored: 1 of 3 pools kept in retriever order" in report


def test_control_a_vendor_served_run_counts_nothing(monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, ALL_PUBLIC, "hosted-api")
    assert out["tier_rule_pools"] == 3
    assert out.get("rerank_unscored", 0) == 0
    assert out["fallback_local"] == 0 and out["tier_rule_local"] == 0
    assert "reranker:" not in report


def test_control_a_tier_skip_the_local_model_scored_counts_as_the_tier_rule(
        monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, WITH_DEPT, "hosted-api")
    assert out["tier_rule_local"] == 1 and out["tier_rule_pools"] == 4
    assert out["fallback_local"] == 0
    assert out.get("rerank_unscored", 0) == 0
    assert "kept in retriever order" not in report
    assert _tag() + f"tier rule: 1 of 4 pools scored locally ({LOCAL_MODEL})" in report


def test_control_a_vendor_failure_the_local_model_scored_counts_as_the_fallback(
        monkeypatch, capsys):
    out, report = _run(monkeypatch, capsys, ALL_PUBLIC, "hosted-api", vendor_fail=(Q3,))
    assert out["fallback_local"] == 1 and out["tier_rule_local"] == 0
    assert out.get("rerank_unscored", 0) == 0
    assert "kept in retriever order" not in report
    assert (_tag() + f"vendor fallback: 1 of 3 pools scored locally ({LOCAL_MODEL})"
            in report)
