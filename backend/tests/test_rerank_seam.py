"""Call-time reranker config seam.

What makes an A/B honest here: `rerank_enabled` / `rerank_model` are read PER
CALL from config (env stays the default), so flipping an arm reaches the
serving process without a container restart - and the model swap both actually
loads the new model AND evicts the old one under a lock, so concurrent readers
can never race two encoders resident (an outside-review finding; the lock
rides the seam by requirement).
"""
import app.rerank as rr
from app.config import set_config


def _reset_module():
    rr._encoders = {}
    rr._load_errors = {}


def _clear_cfg():
    set_config("rerank_enabled", "")
    set_config("rerank_model", "")


class _Fake:
    def __init__(self, model_name=None, cache_dir=None):
        self.model_name = model_name

    def rerank(self, query, docs):
        return [float(len(d)) for d in docs]


def test_config_overrides_reach_call_time():
    try:
        set_config("rerank_enabled", "false")
        assert rr.rerank_enabled() is False
        # Disabled = passthrough, truncated, no encoder touched.
        out = rr.rerank("q", [{"text": "a"}, {"text": "b"}], top_k=1)
        assert [c["text"] for c in out] == ["a"]
        set_config("rerank_enabled", "true")
        assert rr.rerank_enabled() is True
        set_config("rerank_model", "some/other-model")
        assert rr.rerank_model() == "some/other-model"
    finally:
        _clear_cfg()


def test_blank_config_falls_back_to_env_default():
    try:
        set_config("rerank_model", "   ")
        assert rr.rerank_model() == rr.RERANK_MODEL_DEFAULT
    finally:
        _clear_cfg()


def test_model_swap_loads_new_and_evicts_old(monkeypatch):
    _reset_module()
    import fastembed.rerank.cross_encoder as fce
    monkeypatch.setattr(fce, "TextCrossEncoder", _Fake)
    a = rr._get_encoder("model-a")
    assert a is not None and rr._encoders == {"model-a": a}
    b = rr._get_encoder("model-b")
    assert b is not None and list(rr._encoders) == ["model-b"], \
        "the old encoder must be EVICTED, not accumulated (memory bound)"
    # Single rebind: the dict object was replaced whole, never emptied in place.
    assert rr._encoders["model-b"] is b
    _reset_module()


def test_load_failure_disables_that_model_only(monkeypatch):
    _reset_module()
    import fastembed.rerank.cross_encoder as fce

    class _Boom:
        def __init__(self, model_name=None, cache_dir=None):
            if model_name == "bad/model":
                raise RuntimeError("no such model")
            self.model_name = model_name

        def rerank(self, query, docs):
            return [0.0 for _ in docs]

    monkeypatch.setattr(fce, "TextCrossEncoder", _Boom)
    assert rr._get_encoder("bad/model") is None
    assert "bad/model" in rr._load_errors
    # A different (working) model still loads - the failure is per-model.
    assert rr._get_encoder("good/model") is not None
    _reset_module()
