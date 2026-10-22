"""The test suite loads no reranker model (2026-10-21).

app.rerank._get_encoder imports fastembed's TextCrossEncoder when it is first
asked for one and builds it against FASTEMBED_CACHE - which conftest points at a
fresh, empty directory - so the first test to reach it would DOWNLOAD the model
from the Hugging Face hub. No test here reaches a real load today, which is
luck: on a sibling deployment the same download hung under pytest in the deploy
gate's production image on 2026-10-21 (three 25-minute runs, each at the same
test, through the eval run route's reranker preflight - rerank.status(); the
same download took 5 s outside pytest in that image, and the test passed in 2 s
with HF_HUB_OFFLINE=1). conftest now installs a fastembed.rerank.cross_encoder
whose constructor raises: _get_encoder records the load error, status() answers
'not loaded' at once, and retrieval falls back to retriever order - the fallback
the code already has for a failed load. A test that wants an encoder installs
its own (the provider tests set TextCrossEncoder on the module per test, and
win).

FIX cases fail on the conftest before the closure - the real constructor is
reached, a download on a box with the network, the hub's own offline error
without it, never this rule's words; CONTROL holds on both.
"""
import sys
import time
import types

import app.rerank as rr

MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
RULE = "the test suite loads no reranker model"


def test_the_suite_loads_no_reranker_model(monkeypatch):
    # FIX: a fresh ask for the default model records conftest's rule, not a load
    monkeypatch.setattr(rr, "_encoders", {})
    monkeypatch.setattr(rr, "_load_errors", {})
    assert rr._get_encoder(MODEL) is None
    assert RULE in rr._load_errors[MODEL]
    stub = sys.modules["fastembed.rerank.cross_encoder"]
    assert getattr(stub, "__file__", None) is None   # conftest's module, not the package's


def test_the_status_preflight_answers_at_once(monkeypatch):
    # FIX: the eval run route's preflight - the path that hung the gate - answers
    # 'not loaded' in the rule's words, within a bound no download fits
    monkeypatch.setattr(rr, "_encoders", {})
    monkeypatch.setattr(rr, "_load_errors", {})
    monkeypatch.setattr(rr, "rerank_enabled", lambda: True)
    monkeypatch.setattr(rr, "rerank_provider", lambda: "local")
    monkeypatch.setattr(rr, "rerank_model", lambda: MODEL)
    started = time.monotonic()
    info = rr.status()
    assert time.monotonic() - started < 5
    assert info["loaded"] is False
    assert RULE in info["error"]


def test_a_tests_own_stand_in_still_wins(monkeypatch):
    # CONTROL: the shape test_rerank_providers uses - the module a test installs
    # for itself is the one _get_encoder builds from
    class _Fake:
        def rerank(self, query, docs):
            return [1.0] * len(docs)
    mod = types.ModuleType("fastembed.rerank.cross_encoder")
    mod.TextCrossEncoder = lambda model_name, cache_dir=None: _Fake()
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", mod)
    monkeypatch.setattr(rr, "_encoders", {})
    monkeypatch.setattr(rr, "_load_errors", {})
    enc = rr._get_encoder("a-model-of-this-tests-own")
    assert enc is not None and enc.rerank("q", ["a", "b"]) == [1.0, 1.0]
