"""The reranker's status names the model that scores (2026-11-17).

status() is what a run reads to say which reranker it measured: the eval
harness tags its rows with status()["model"], and the eval preflight refuses a
run whose reranker cannot score. Under hosted-api it named rerank_model(), the
LOCAL cross-encoder's configured name, while the vendor scored with the hosted
model (rerank_hosted_model, else the vendor's own default). The scores were
right and the label was wrong: a run the vendor scored went on record under a
model that never saw it. The live-system record carried the same name, and the
model is told to treat that record as current truth.

FIX cases fail on the code before this change (the label is the local
encoder's name under hosted-api). CONTROL cases hold on both: local loads
rerank_model() and remote-http sends it as the request's model, so that is
their label. No case reaches the network or loads a model: requests.post and
the encoder are stand-ins.
"""
import app.rag_config as rc
import app.rerank as rr
from app.config import set_config

VENDOR_DEFAULT = "rerank-v3.5"   # the cohere row of rerank._HOSTED


def _clear_cfg():
    for key in ("rerank_enabled", "rerank_provider", "rerank_remote_url",
                "rerank_hosted_vendor", "rerank_hosted_model", "rerank_model"):
        set_config(key, "")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Post:
    """Stands in for requests.post; records every call it receives."""
    def __init__(self, payload):
        self.calls = []
        self._payload = payload

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _Resp(self._payload)


class _Ordered:
    """A local encoder that ranks the self-test's relevant snippet first."""
    def rerank(self, query, docs):
        return [1.0, 0.0][:len(docs)]


def _stub(monkeypatch, payload):
    post = _Post(payload)
    monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
    monkeypatch.setattr(rr, "_get_encoder", lambda model=None: _Ordered())
    return post


def _hosted(monkeypatch, *, allowed=True):
    set_config("rerank_enabled", "true")
    set_config("rerank_provider", "hosted-api")
    monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", allowed, raising=True)
    monkeypatch.setenv("COHERE_API_KEY", "test-key")
    return _stub(monkeypatch, {"results": [{"index": 0, "relevance_score": 0.9},
                                           {"index": 1, "relevance_score": 0.1}]})


# -- FIX: hosted-api is labelled with the model the vendor is sent ------------

def test_fix_hosted_status_names_the_vendor_default_model(monkeypatch):
    try:
        post = _hosted(monkeypatch)
        info = rr.status()
        assert info["loaded"] is True and info["error"] is None
        assert info["model"] == VENDOR_DEFAULT
        assert info["model"] != rr.rerank_model()
        assert len(post.calls) == 1
    finally:
        _clear_cfg()


def test_fix_hosted_status_names_the_override(monkeypatch):
    try:
        post = _hosted(monkeypatch)
        set_config("rerank_hosted_model", "rerank-test-override")
        info = rr.status()
        assert info["loaded"] is True
        assert info["model"] == "rerank-test-override"
        assert post.calls[0]["json"]["model"] == "rerank-test-override"
    finally:
        _clear_cfg()


def test_fix_the_label_is_the_model_the_vendor_was_sent(monkeypatch):
    try:
        post = _hosted(monkeypatch)
        info = rr.status()
        sent = [c["json"]["model"] for c in post.calls]
        assert sent == [info["model"]]
        set_config("rerank_hosted_model", "rerank-test-override")
        post.calls.clear()
        info = rr.status()
        assert [c["json"]["model"] for c in post.calls] == [info["model"]]
    finally:
        _clear_cfg()


def test_fix_a_closed_latch_still_names_the_hosted_model_and_sends_nothing(monkeypatch):
    try:
        post = _hosted(monkeypatch, allowed=False)
        info = rr.status()
        assert info["loaded"] is False and "latched OFF" in info["error"]
        assert info["model"] == VENDOR_DEFAULT
        assert post.calls == []
    finally:
        _clear_cfg()


def test_fix_an_unknown_vendor_is_no_model_and_status_does_not_raise(monkeypatch):
    try:
        post = _hosted(monkeypatch)
        set_config("rerank_hosted_vendor", "acme")
        info = rr.status()
        assert info["loaded"] is False and "unknown rerank_hosted_vendor" in info["error"]
        assert info["model"] == ""
        assert post.calls == []
    finally:
        _clear_cfg()


def test_fix_the_live_system_record_names_the_hosted_model(client, monkeypatch):
    import app.system_records as sr
    try:
        _hosted(monkeypatch, allowed=False)
        snap = sr._snapshot()
        assert snap["rerank_provider"] == "hosted-api"
        assert snap["rerank_model"] == VENDOR_DEFAULT
        assert "(hosted-api, model " + VENDOR_DEFAULT + ")" in sr.build_posture(snap)
    finally:
        _clear_cfg()


# -- CONTROL: local and remote-http score with rerank_model() -----------------

def test_control_local_status_names_the_local_model(monkeypatch):
    try:
        set_config("rerank_enabled", "true")
        set_config("rerank_provider", "local")
        post = _stub(monkeypatch, {})
        info = rr.status()
        assert info["loaded"] is True
        assert info["model"] == rr.rerank_model()
        assert post.calls == []
    finally:
        _clear_cfg()


def test_control_remote_http_status_names_the_model_it_sends(monkeypatch):
    try:
        set_config("rerank_enabled", "true")
        set_config("rerank_provider", "remote-http")
        set_config("rerank_remote_url", "http://scorer.test:9000/rerank")
        post = _stub(monkeypatch, {"scores": [0.9, 0.1]})
        info = rr.status()
        assert info["loaded"] is True
        assert info["model"] == rr.rerank_model()
        assert [c["json"]["model"] for c in post.calls] == [info["model"]]
    finally:
        _clear_cfg()


def test_control_the_live_system_record_names_the_local_model(client, monkeypatch):
    import app.system_records as sr
    try:
        set_config("rerank_enabled", "true")
        set_config("rerank_provider", "local")
        _stub(monkeypatch, {})
        snap = sr._snapshot()
        assert snap["rerank_model"] == rr.rerank_model()
    finally:
        _clear_cfg()
