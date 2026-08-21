"""Instrument-gap guards from a live index-rot incident.

Two failure modes measured live, now pinned by tests:
  - A history collection's HNSW index refused knn at k=1 while col.get stayed
    green, so the eval pre-flight passed and the run silently measured a
    system missing that collection (the tuned score dropped several points
    with nothing errored).
  - Repeated identical-configuration passes scored in exact arithmetic
    proportion to writer-API error bursts - a raw noise band conflates model
    nondeterminism with API availability.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")))

import eval_retrieval as ev  # noqa: E402


# -- _pass_metrics: raw vs error-adjusted -------------------------------------

def _row(score, cat="general", holdout=0, errored=False, faith=None, fresh=None,
         corpus="src=1;chunks=1;sha=abc"):
    return {"score": score, "cat": cat, "holdout": holdout, "errored": errored,
            "faith": faith, "fresh": fresh, "corpus": corpus}


def test_pass_metrics_errored_rows_fail_raw_but_leave_adjusted():
    """The incident arithmetic as a fixture: errored answers drag the raw score
    (product honesty - the user got nothing) but must NOT drag the adjusted one
    (the model never got to answer)."""
    rows = ([_row(1)] * 8 + [_row(0, errored=True)] * 2      # tuned 8/10 raw
            + [_row(1, holdout=1)] * 3)                       # holdout clean
    m = ev._pass_metrics(rows)
    assert m["tuned"] == 80.0
    assert m["tuned_adj"] == 100.0
    assert m["holdout"] == m["holdout_adj"] == 100.0
    assert m["errored"] == 2
    assert m["gap"] == -20.0 and m["gap_adj"] == 0.0


def test_pass_metrics_clean_pass_adjusted_equals_raw():
    rows = [_row(1), _row(0), _row(1, holdout=1), _row(1, cat="honesty")]
    m = ev._pass_metrics(rows)
    assert m["tuned"] == m["tuned_adj"] == 50.0
    assert m["holdout"] == m["holdout_adj"] == 100.0
    assert m["honesty"] == m["honesty_adj"] == 100.0
    assert m["errored"] == 0


def test_pass_metrics_mid_run_corpus_change_is_visible():
    """Two distinct stamps in one pass = the corpus moved mid-run; the metrics
    must surface both, not collapse them."""
    rows = [_row(1, corpus="src=1;chunks=1;sha=aaa"),
            _row(1, corpus="src=1;chunks=2;sha=bbb")]
    m = ev._pass_metrics(rows)
    assert len(m["corpus"]) == 2


# -- pre-flight knn probe: visibility is not retrievability -------------------

class _FakeCol:
    def __init__(self, name, n, knn_ok, embeddings="numpy"):
        self.name = name
        self._n = n
        self._knn_ok = knn_ok
        self._embeddings = embeddings

    def count(self):
        return self._n

    def get(self, limit=None, include=None):
        # Chroma returns embeddings as a NUMPY ARRAY, not a list. An earlier
        # fake returned a plain list - truthy, so the probe's `or` fallback was
        # never exercised and a fully green mocked suite sat on top of a probe
        # that failed on first live contact (every healthy collection reported
        # BROKEN, "truth value of an array ... is ambiguous"). The default here
        # is now the real return type; the other modes cover the
        # genuinely-absent cases the fallback exists for.
        if self._embeddings == "numpy":
            import numpy as np
            return {"embeddings": np.zeros((1, 4))}
        if self._embeddings == "none":
            return {"embeddings": None}
        if self._embeddings == "empty":
            return {"embeddings": []}
        return {"embeddings": [[0.0] * 4]}

    def query(self, query_embeddings=None, n_results=None):
        if not self._knn_ok:
            raise RuntimeError(
                "Cannot return the results in a contigious 2D array")
        return {"ids": [["x"]]}


class _FakeClient:
    def __init__(self, cols):
        self._cols = {c.name: c for c in cols}

    def list_collections(self):
        return list(self._cols.values())

    def get_or_create_collection(self, name, metadata=None):
        return self._cols[name]


def _preflight_with(monkeypatch, cols, questions):
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "client", _FakeClient(cols))
    monkeypatch.setattr(ev, "list_sources", lambda department=None: [
        {"source": "doc.md", "department": "general", "count": 1}])
    return ev.preflight(questions)


def _q(expected="local:doc.md"):
    return type("Q", (), {"expected_source": expected})()


def test_preflight_fails_when_a_nonempty_collection_refuses_knn(monkeypatch):
    """The exact rot mode measured live: count/get fine, knn dead. Pre-flight
    must refuse to measure - a green pre-flight over a searchless collection is
    how points vanish from the score with every check passing."""
    cols = [_FakeCol("knowledge_base", 10, knn_ok=True),
            _FakeCol("kb_history", 512, knn_ok=False)]
    assert _preflight_with(monkeypatch, cols, [_q()]) is False


def test_preflight_passes_healthy_and_skips_empty_collections(monkeypatch):
    cols = [_FakeCol("knowledge_base", 10, knn_ok=True),
            _FakeCol("kb_empty", 0, knn_ok=False)]  # empty: never probed
    assert _preflight_with(monkeypatch, cols, [_q()]) is True


def test_preflight_does_not_false_alarm_on_numpy_embeddings(monkeypatch):
    """The probe's OWN first live run reported every healthy collection BROKEN:
    `got.get("embeddings") or [...]` calls bool() on a numpy array, which
    raises. A tripwire that fails toward false alarm still fails - and a
    mocked-chroma suite could not see it, so this pins the real type."""
    cols = [_FakeCol("knowledge_base", 10, knn_ok=True, embeddings="numpy")]
    assert _preflight_with(monkeypatch, cols, [_q()]) is True


def test_preflight_probes_with_a_zero_vector_when_embeddings_are_absent(monkeypatch):
    """The case the fallback actually exists for: no stored embedding to borrow.
    The probe must still run a knn query (and still catch a dead index), not
    skip the collection or crash."""
    for mode in ("none", "empty"):
        healthy = [_FakeCol("knowledge_base", 10, knn_ok=True, embeddings=mode)]
        assert _preflight_with(monkeypatch, healthy, [_q()]) is True, mode
        dead = [_FakeCol("knowledge_base", 10, knn_ok=False, embeddings=mode)]
        assert _preflight_with(monkeypatch, dead, [_q()]) is False, mode


# -- ordering metrics: nDCG@k and MRR (added for the reranker A/B) ------------
#
# Recall is a SET metric: it cannot tell the answer at rank 1 from the answer at
# rank 5, which is exactly the difference a reranker is bought for. These pin the
# ordering metrics that decide that A/B, including the tie-break case where two
# arms have identical recall and only the ordering differs.

def test_ndcg_rewards_putting_the_answer_first():
    perfect = ev._ndcg([1, 0, 0, 0, 0])
    last = ev._ndcg([0, 0, 0, 0, 1])
    assert perfect == 1.0
    assert 0 < last < perfect


def test_ndcg_is_zero_when_nothing_relevant_came_back():
    """Reported as 0.0, never skipped: excluding misses would let an arm that
    retrieves LESS outscore one that retrieves more."""
    assert ev._ndcg([0, 0, 0, 0, 0]) == 0.0


def test_ndcg_is_one_when_all_relevant_chunks_lead():
    """Normalised against the best ORDERING of the same chunks, so two relevant
    chunks in the top two slots is a perfect score, not a partial one."""
    assert ev._ndcg([1, 1, 0, 0, 0]) == 1.0
    assert ev._ndcg([0, 1, 1, 0, 0]) < 1.0


def test_reciprocal_rank_tracks_the_first_hit():
    assert ev._reciprocal_rank([1, 0, 0]) == 1.0
    assert ev._reciprocal_rank([0, 1, 0]) == 0.5
    assert ev._reciprocal_rank([0, 0, 1]) == 1 / 3
    assert ev._reciprocal_rank([0, 0, 0]) == 0.0


def test_relevance_uses_the_same_matcher_as_scoring():
    """Basename-aware, '|' alternates - if this drifted from _score_retrieval the
    A/B's ordering metrics would disagree with its own recall column."""
    needles = ev._needles("local:handbook/onboarding.md|team-profile.md")
    assert ev._relevance(needles, ["onboarding.md", "index.md", "team-profile.md"]) == [1, 0, 1]
    assert ev._relevance(needles, ["unrelated.md"]) == [0]


def test_ab_configs_are_the_four_arms_and_flip_the_live_keys():
    """An arm must be a CONFIG flip (rerank.py reads these per call), not a
    restart - and the no-reranker arm must actually disable, not just swap.
    int8-cpu is the quantized custom model rerank.py registers; gpu-remote is
    the same fp32 model over the remote-http provider - hardware is the
    variable. Every LOCAL reranker arm must name a DISTINCT model or two rows
    measure the same system under different labels; every arm must set
    rerank_provider EXPLICITLY or a remote arm's provider would leak into the
    local arms that run after it."""
    labels = [label for label, _ in ev.AB_CONFIGS]
    assert labels == ["no-reranker", "ms-marco", "int8-cpu", "gpu-remote"]
    cfgs = dict(ev.AB_CONFIGS)
    assert all("rerank_provider" in c for c in cfgs.values()), \
        "every arm must pin the provider - unlisted keys leak between arms"
    assert cfgs["no-reranker"]["rerank_enabled"] == "false"
    assert cfgs["ms-marco"]["rerank_enabled"] == "true"
    assert cfgs["ms-marco"]["rerank_model"]
    assert cfgs["int8-cpu"]["rerank_enabled"] == "true"
    assert cfgs["int8-cpu"]["rerank_model"]
    local_models = [c["rerank_model"] for label, c in ev.AB_CONFIGS
                    if c.get("rerank_enabled") == "true" and not c.get("rerank_provider")]
    assert len(set(local_models)) == len(local_models)
    # gpu-remote: same fp32 model as ms-marco ON PURPOSE (isolates hardware),
    # distinguished by provider, never by a fake model name.
    assert cfgs["gpu-remote"]["rerank_provider"] == "remote-http"
    assert cfgs["gpu-remote"]["rerank_model"] == cfgs["ms-marco"]["rerank_model"]
    # The int8 arm's model must be one rerank.py can actually construct - a
    # registered custom model, not a bare unknown name (the mislabelled-arm
    # class: a load failure would abort, but a typo'd registered name is worse).
    import app.rerank as rr
    assert cfgs["int8-cpu"]["rerank_model"] in rr._CUSTOM_MODELS


def test_run_ab_arm_filter_rejects_unknown_labels():
    assert ev.run_ab([], 5, False, arms=["no-such-arm"]) is None
