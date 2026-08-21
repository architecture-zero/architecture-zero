"""Provider seam tests: scoring dispatches per call on the rerank_provider
config key (local | remote-http | hosted-api), every provider failure degrades
to retriever order (rerank never breaks chat), and the hosted arm's egress is
LATCHED behind the RERANK_HOSTED_ALLOWED host env - a DB config flip alone
must never start shipping candidate chunks to a third party (an instance whose
candidate pool can contain private content simply never sets the latch).
"""
import app.rag_config as rc
import app.rerank as rr
from app.config import set_config

CANDS = [{"text": "short"}, {"text": "the longest text of them all"}, {"text": "mid text"}]


def _clear_cfg():
    for key in ("rerank_enabled", "rerank_provider", "rerank_remote_url",
                "rerank_hosted_vendor", "rerank_hosted_model", "rerank_model"):
        set_config(key, "")


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _PostRecorder:
    """Stands in for requests.post; records every call it receives."""
    def __init__(self, payload=None, status=200, exc=None):
        self.calls = []
        self._payload = payload
        self._status = status
        self._exc = exc

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._exc:
            raise self._exc
        return _Resp(self._payload, self._status)


# Local-chain helpers: the fallback chain sends failed non-local providers to
# the LOCAL encoder before retriever order - so provider-failure tests fake
# the local encoder and assert ITS ordering (len-scored: longest first),
# proving the chain engaged.
LOCAL_ORDER = ["the longest text of them all", "mid text", "short"]


class _FakeLen:
    def __init__(self, model_name=None, cache_dir=None):
        self.model_name = model_name

    @classmethod
    def add_custom_model(cls, **kw):
        pass

    def rerank(self, query, docs):
        return [float(len(d)) for d in docs]


class _FakeBoom:
    def __init__(self, model_name=None, cache_dir=None):
        raise RuntimeError("local encoder unavailable")


def _fake_local(monkeypatch, cls=_FakeLen):
    import fastembed.rerank.cross_encoder as fce
    rr._encoders = {}
    rr._load_errors = {}
    monkeypatch.setattr(fce, "TextCrossEncoder", cls)


# -- provider selection -------------------------------------------------------

def test_default_provider_is_local():
    try:
        assert rr.rerank_provider() == "local"
    finally:
        _clear_cfg()


def test_unknown_provider_degrades_loudly_to_local():
    try:
        set_config("rerank_provider", "gpu-magic")
        assert rr.rerank_provider() == "local"
    finally:
        _clear_cfg()


def test_provider_flip_reaches_call_time():
    try:
        set_config("rerank_provider", "remote-http")
        assert rr.rerank_provider() == "remote-http"
        set_config("rerank_provider", "")
        assert rr.rerank_provider() == "local"
    finally:
        _clear_cfg()


# -- remote-http (the GPU-box arm) --------------------------------------------

def test_remote_http_scores_and_ranks(monkeypatch):
    try:
        set_config("rerank_provider", "remote-http")
        set_config("rerank_remote_url", "http://gpu-box:9000/rerank")
        post = _PostRecorder(payload={"scores": [0.1, 0.9, 0.5]})
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        out = rr.rerank("q", CANDS, top_k=2)
        assert [c["text"] for c in out] == ["the longest text of them all", "mid text"]
        assert out[0]["rerank_score"] == 0.9
        # The request carried the contract: query + texts + model, to the config url.
        call = post.calls[0]
        assert call["url"] == "http://gpu-box:9000/rerank"
        assert call["json"]["query"] == "q"
        assert call["json"]["texts"] == [c["text"] for c in CANDS]
        assert "model" in call["json"]
    finally:
        _clear_cfg()


def test_remote_http_without_url_falls_back_no_http(monkeypatch):
    try:
        set_config("rerank_provider", "remote-http")
        post = _PostRecorder(payload={"scores": []})
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        monkeypatch.setattr(rr, "RERANK_REMOTE_URL_DEFAULT", "", raising=True)
        _fake_local(monkeypatch)
        out = rr.rerank("q", CANDS, top_k=2)
        assert [c["text"] for c in out] == LOCAL_ORDER[:2], \
            "the chain must land on the LOCAL encoder, not raw retriever order"
        assert post.calls == [], "no url configured must mean no HTTP attempt"
    finally:
        _clear_cfg()


def test_remote_http_wrong_score_count_falls_back(monkeypatch):
    try:
        set_config("rerank_provider", "remote-http")
        set_config("rerank_remote_url", "http://gpu-box:9000/rerank")
        post = _PostRecorder(payload={"scores": [0.9]})  # 1 score for 3 texts
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        _fake_local(monkeypatch)
        out = rr.rerank("q", CANDS, top_k=3)
        assert [c["text"] for c in out] == LOCAL_ORDER, \
            "a partial remote score set must never misrank - the chain rescores LOCALLY"
    finally:
        _clear_cfg()


def test_remote_http_error_falls_back(monkeypatch):
    try:
        set_config("rerank_provider", "remote-http")
        set_config("rerank_remote_url", "http://gpu-box:9000/rerank")
        post = _PostRecorder(exc=ConnectionError("scoring box is asleep"))
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        _fake_local(monkeypatch)
        out = rr.rerank("q", CANDS, top_k=2)
        assert [c["text"] for c in out] == LOCAL_ORDER[:2], \
            "a napping GPU box must cost latency (local rerank), never recall"
    finally:
        _clear_cfg()


def test_remote_status_reports_missing_url(monkeypatch):
    try:
        set_config("rerank_provider", "remote-http")
        monkeypatch.setattr(rr, "RERANK_REMOTE_URL_DEFAULT", "", raising=True)
        info = rr.status()
        assert info["provider"] == "remote-http"
        assert info["loaded"] is False
        assert "RERANK_REMOTE_URL" in info["error"]
    finally:
        _clear_cfg()


# -- hosted-api (the buy arm) -------------------------------------------------

def test_hosted_latched_off_makes_no_http_call(monkeypatch):
    """THE PRIVACY LATCH: provider flipped + key present, but the host env is
    not set -> zero HTTP, clean fallback. This is the test that matters."""
    try:
        set_config("rerank_provider", "hosted-api")
        monkeypatch.setenv("COHERE_API_KEY", "test-key")
        monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", False, raising=True)
        post = _PostRecorder(payload={"results": []})
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        _fake_local(monkeypatch)
        out = rr.rerank("q", CANDS, top_k=2)
        assert [c["text"] for c in out] == LOCAL_ORDER[:2]
        assert post.calls == [], "latched off must mean NO chunk ever leaves the box"
        info = rr.status()
        assert info["loaded"] is False and "latched OFF" in info["error"]
    finally:
        _clear_cfg()


def test_hosted_allowed_but_keyless_falls_back_no_http(monkeypatch):
    try:
        set_config("rerank_provider", "hosted-api")
        monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", True, raising=True)
        monkeypatch.delenv("COHERE_API_KEY", raising=False)
        post = _PostRecorder(payload={"results": []})
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        _fake_local(monkeypatch)
        out = rr.rerank("q", CANDS, top_k=1)
        assert [c["text"] for c in out] == LOCAL_ORDER[:1]
        assert post.calls == []
        info = rr.status()
        assert "COHERE_API_KEY" in info["error"]
        assert "test-key" not in str(info), "status must name the env, never a value"
    finally:
        _clear_cfg()


def test_hosted_cohere_scores_and_ranks(monkeypatch):
    try:
        set_config("rerank_provider", "hosted-api")
        monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", True, raising=True)
        monkeypatch.setenv("COHERE_API_KEY", "test-key")
        post = _PostRecorder(payload={"results": [
            {"index": 0, "relevance_score": 0.2},
            {"index": 1, "relevance_score": 0.95},
            {"index": 2, "relevance_score": 0.4},
        ]})
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        out = rr.rerank("q", CANDS, top_k=2)
        assert [c["text"] for c in out] == ["the longest text of them all", "mid text"]
        call = post.calls[0]
        assert call["url"] == "https://api.cohere.com/v2/rerank"
        assert call["json"]["documents"] == [c["text"] for c in CANDS]
        assert call["json"]["model"] == "rerank-v3.5"  # vendor default when unset
        assert call["headers"]["Authorization"] == "Bearer test-key"
    finally:
        _clear_cfg()


def test_hosted_voyage_shape_and_model_override(monkeypatch):
    try:
        set_config("rerank_provider", "hosted-api")
        set_config("rerank_hosted_vendor", "voyage")
        set_config("rerank_hosted_model", "rerank-2.5-lite")
        monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", True, raising=True)
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key-v")
        post = _PostRecorder(payload={"data": [
            {"index": 0, "relevance_score": 0.7},
            {"index": 1, "relevance_score": 0.1},
            {"index": 2, "relevance_score": 0.4},
        ]})
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        out = rr.rerank("q", CANDS, top_k=3)
        assert [c["text"] for c in out] == ["short", "mid text", "the longest text of them all"]
        call = post.calls[0]
        assert call["url"] == "https://api.voyageai.com/v1/rerank"
        assert call["json"]["model"] == "rerank-2.5-lite"
    finally:
        _clear_cfg()


def test_hosted_partial_results_fall_back(monkeypatch):
    try:
        set_config("rerank_provider", "hosted-api")
        monkeypatch.setattr(rc, "RERANK_HOSTED_ALLOWED", True, raising=True)
        monkeypatch.setenv("COHERE_API_KEY", "test-key")
        post = _PostRecorder(payload={"results": [{"index": 1, "relevance_score": 0.9}]})
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        _fake_local(monkeypatch)
        out = rr.rerank("q", CANDS, top_k=3)
        assert [c["text"] for c in out] == LOCAL_ORDER, \
            "a partial hosted score set must never misrank - the chain rescores LOCALLY"
    finally:
        _clear_cfg()


# -- INT8 custom model (the cheap local arm) ----------------------------------

def test_int8_custom_model_registers_once(monkeypatch):
    """Loading the int8 name registers the quantized ONNX with fastembed ONCE
    (fastembed refuses duplicates), pointing at the same repo's quantized file."""
    import fastembed.rerank.cross_encoder as fce
    calls = []

    class _FakeWithRegistry:
        def __init__(self, model_name=None, cache_dir=None):
            self.model_name = model_name

        @classmethod
        def add_custom_model(cls, **kw):
            calls.append(kw)

        def rerank(self, query, docs):
            return [0.0 for _ in docs]

    rr._encoders = {}
    rr._load_errors = {}
    rr._registered_custom = set()
    monkeypatch.setattr(fce, "TextCrossEncoder", _FakeWithRegistry)
    name = "Xenova/ms-marco-MiniLM-L-6-v2-int8"
    assert rr._get_encoder(name) is not None
    assert len(calls) == 1
    assert calls[0]["model"] == name
    assert calls[0]["model_file"] == "onnx/model_quantized.onnx"
    # Second load (post-eviction) must NOT re-register.
    rr._encoders = {}
    assert rr._get_encoder(name) is not None
    assert len(calls) == 1
    rr._encoders = {}
    rr._load_errors = {}
    rr._registered_custom = set()


def test_non_custom_model_skips_registration(monkeypatch):
    import fastembed.rerank.cross_encoder as fce
    calls = []

    class _FakeWithRegistry:
        def __init__(self, model_name=None, cache_dir=None):
            self.model_name = model_name

        @classmethod
        def add_custom_model(cls, **kw):
            calls.append(kw)

        def rerank(self, query, docs):
            return [0.0 for _ in docs]

    rr._encoders = {}
    rr._registered_custom = set()
    monkeypatch.setattr(fce, "TextCrossEncoder", _FakeWithRegistry)
    assert rr._get_encoder("Xenova/ms-marco-MiniLM-L-6-v2") is not None
    assert calls == []
    rr._encoders = {}


def test_custom_model_uses_its_own_cache_subdir(monkeypatch):
    """Same-repo custom models must NOT share the preset's cache dir: the fp32
    snapshot already there makes fastembed skip the download and the load dies
    NoSuchFile (a real deploy-build failure, reproduced locally)."""
    import os
    import fastembed.rerank.cross_encoder as fce
    seen = {}

    class _FakeCacheSpy:
        def __init__(self, model_name=None, cache_dir=None):
            seen[model_name] = cache_dir

        @classmethod
        def add_custom_model(cls, **kw):
            pass

        def rerank(self, query, docs):
            return [0.0 for _ in docs]

    rr._encoders = {}
    rr._load_errors = {}
    rr._registered_custom = set()
    monkeypatch.setattr(fce, "TextCrossEncoder", _FakeCacheSpy)
    int8 = "Xenova/ms-marco-MiniLM-L-6-v2-int8"
    rr._get_encoder(int8)
    rr._encoders = {}
    rr._get_encoder("Xenova/ms-marco-MiniLM-L-6-v2")
    assert seen[int8] == os.path.join(rr._CACHE_DIR, "custom")
    assert seen["Xenova/ms-marco-MiniLM-L-6-v2"] == rr._CACHE_DIR
    rr._encoders = {}
    rr._load_errors = {}
    rr._registered_custom = set()


def test_remote_http_carries_cf_access_headers(monkeypatch):
    """A remote scorer can sit behind the same CF Access app as a tunneled
    Ollama base (path route) - the remote call must carry the service token
    when the env has one, and no auth header at all when it does not (local
    dev)."""
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "test-id.access")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "test-cf-secret")
    import app.providers as pv
    monkeypatch.setattr(pv, "CF_ACCESS_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(pv, "CF_ACCESS_CLIENT_SECRET", "", raising=False)
    try:
        set_config("rerank_provider", "remote-http")
        set_config("rerank_remote_url", "http://gpu-box:9000/rerank")
        post = _PostRecorder(payload={"scores": [0.1, 0.9, 0.5]})
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        rr.rerank("q", CANDS, top_k=1)
        h = post.calls[0]["headers"]
        assert h["CF-Access-Client-Id"] == "test-id.access"
        assert h["CF-Access-Client-Secret"] == "test-cf-secret"
    finally:
        _clear_cfg()


def test_chain_end_remote_and_local_both_fail(monkeypatch):
    """The chain floor: remote down AND local unloadable -> retriever order,
    truncated - rerank never breaks chat."""
    try:
        set_config("rerank_provider", "remote-http")
        set_config("rerank_remote_url", "http://gpu-box:9000/rerank")
        post = _PostRecorder(exc=ConnectionError("scoring box is asleep"))
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        _fake_local(monkeypatch, cls=_FakeBoom)
        out = rr.rerank("q", CANDS, top_k=2)
        assert [c["text"] for c in out] == ["short", "the longest text of them all"]
    finally:
        _clear_cfg()
        rr._encoders = {}
        rr._load_errors = {}
