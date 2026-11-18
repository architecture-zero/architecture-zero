"""A run's record says when the tier rule scored its pools locally (2026-11-17).

Under rerank_provider=hosted-api a pool that is not all public is never sent
to the vendor: the local model scores it (the tier rule, at
rerank._score_hosted), and that turn's receipt says so (the stats key
rerank_hosted_skipped). The retrieval script tags its run with
rerank.status()["model"], the model the provider in effect scores with: the
hosted model here. So a run whose pools reached past the global collection
(an Owner-level pool holds the floor departments) was scored partly or wholly
by the local model under the hosted model's name. Each turn's receipt was
honest; the run's tag was not. The script now counts the receipts and prints
the count beside the tag.

The real retrieve and rerank run; the corpus, the vendor and the local model
are stood in for, so no index, no request and no model load:
  FIX      under hosted-api, a run whose pools hold a department's chunks
           records how many pools the tier rule scored locally, and its
           report says so beside the tag.
  CONTROL  under hosted-api, a run whose pools are all public adds nothing:
           every pool went to the vendor, and the tag was true.
  CONTROL  a run on the local provider adds nothing.
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

# One pool per question, by question text: three candidates, each from its own
# source so the per-source cap keeps every one.
MIXED = {"Where is the onboarding guide?": (PUBLIC, DEPT, PUBLIC),
         "Who approves a new department?": (PUBLIC, PUBLIC, PUBLIC),
         "How is a restricted document kept apart?": (DEPT, DEPT, PUBLIC)}
ALL_PUBLIC = {q: (PUBLIC, PUBLIC, PUBLIC) for q in MIXED}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Post:
    """Stands in for requests.post: records each call, ranks last to first."""
    def __init__(self):
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(kw["json"]["documents"])
        n = len(kw["json"]["documents"])
        return _Resp({"results": [{"index": i, "relevance_score": float(i)}
                                  for i in range(n)]})


class _Len:
    """The local model, stood in for: it scores by length."""
    def rerank(self, query, docs):
        return [float(len(d)) for d in docs]


def _run(monkeypatch, capsys, pools: dict, provider: str):
    """One retrieval pass of the script over `pools`, on `provider`, with the
    hosted arm opened as a deployer opens it (the provider key, the host
    latch and the vendor's key). Returns the run's result, its report and the
    documents the vendor was sent."""
    cfg = {"rerank_enabled": "true", "rerank_provider": provider}
    monkeypatch.setattr(rr, "_cfg", lambda k, d: cfg.get(k, d))
    monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", True)
    monkeypatch.setenv("COHERE_API_KEY", "test-key")
    post = _Post()
    monkeypatch.setattr(rr, "requests", type("Requests", (), {"post": post}))
    monkeypatch.setattr(rr, "_get_encoder", lambda model=None: _Len())

    def query_similar(query, n_results=5, department=None, **kw):
        return [{"text": f"{query} chunk {i} " + "x" * i, "source": f"docs/{i}.md",
                 "collection": col, "score": 0.9}
                for i, col in enumerate(pools[query])]

    monkeypatch.setattr(database, "query_similar", query_similar)
    monkeypatch.setattr("time.sleep", lambda s: None)
    questions = [types.SimpleNamespace(id=i, question=q, expected_source="docs/0.md",
                                       category="general", setup_turns=None)
                 for i, q in enumerate(pools, start=1)]
    real_rerank = rr.rerank
    out = ev.run_cohort(questions, top_k=3, misses_only=True)
    assert rr.rerank is real_rerank, "the script puts the reranker back"
    return out, capsys.readouterr().out, post.calls


def _header(report: str) -> str:
    return next(line for line in report.splitlines() if line.startswith("=== RUN"))


def test_fix_a_hosted_run_records_the_pools_the_tier_rule_scored_locally(monkeypatch, capsys):
    out, report, sent = _run(monkeypatch, capsys, MIXED, "hosted-api")
    assert out is not None and out["scored"] == 3
    # The tag still names the model the provider in effect scores with ...
    assert f"retrieval={HOSTED_MODEL}" in _header(report)
    # ... and beside it the record now says two of the three pools never
    # reached that model: the local one scored them.
    assert out["tier_rule_local"] == 2 and out["tier_rule_pools"] == 3
    local = rr.RERANK_MODEL_DEFAULT
    line = (f"reranker: {HOSTED_MODEL} via hosted-api (cohere); "
            f"tier rule: 2 of 3 pools scored locally ({local})")
    assert line in report
    # The rule itself is untouched: the self-test and the one all-public pool
    # reached the vendor, and nothing from a pool that held a department's
    # chunk did.
    assert len(sent) == 2
    kept_off = [q for q, tiers in MIXED.items() if DEPT in tiers]
    assert not any(doc.startswith(q) for docs in sent for doc in docs for q in kept_off)


def test_control_an_all_public_hosted_run_adds_nothing(monkeypatch, capsys):
    out, report, sent = _run(monkeypatch, capsys, ALL_PUBLIC, "hosted-api")
    assert out is not None and out["scored"] == 3
    assert f"retrieval={HOSTED_MODEL}" in _header(report)
    # Every pool went to the vendor (plus the self-test): the tag was true.
    assert len(sent) == 1 + len(ALL_PUBLIC)
    assert out.get("tier_rule_local", 0) == 0
    assert "tier rule" not in report


def test_control_a_local_run_adds_nothing(monkeypatch, capsys):
    out, report, sent = _run(monkeypatch, capsys, MIXED, "local")
    assert out is not None and out["scored"] == 3
    assert f"retrieval={rr.RERANK_MODEL_DEFAULT}" in _header(report)
    assert sent == []
    assert out.get("tier_rule_local", 0) == 0
    assert "tier rule" not in report
