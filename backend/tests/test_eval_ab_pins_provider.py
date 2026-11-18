"""The --ab arms run under the reranker they name (2026-11-17).

run_ab flips each arm's keys in the live config, runs the cohort, and
restores what it found in a `finally`. The local-model arms set
rerank_provider blank, read as "the env default, local" - but blank means
whatever RERANK_PROVIDER says, so on a deployment whose env default is
hosted-api every arm labeled with a local model was scored by the hosted
vendor. The A/B compared nothing it claimed to.

The config is a dict here (no DB) and run_cohort is a stand-in that records
what each arm ran under - the provider, and the model a run's tag names
(scoring_model, which status() reports as "model") - so no retrieval, no
model load and no request to any scorer:
  FIX      a local-model arm runs on local and its tag names its own model,
           on a host that says hosted-api in its config row and its env
           default, and on one that says it only in the env default.
  CONTROL  gpu-remote still runs on remote-http.
  CONTROL  the run leaves the config as it found it - after a clean run and
           after an arm raises mid-run - and a provider with no row reads
           its env default afterwards, as it did before. run_ab's snapshot
           already covered every key an arm writes; these keep it so.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")))

import eval_retrieval as ev  # noqa: E402
import app.rerank as rr  # noqa: E402

LOCAL_ARMS = ("no-reranker", "ms-marco", "int8-cpu")
OPERATOR_ROWS = {"rerank_enabled": "true", "rerank_model": "operator/model",
                 "rerank_provider": "hosted-api"}


def _host(monkeypatch, rows: dict, env_provider: str) -> dict:
    """The live config as a dict behind app.config's two calls (rerank.py's
    _cfg imports get_config per call, run_ab imports both per run), and the
    env default rerank_provider falls back to."""
    monkeypatch.setattr("app.config.get_config",
                        lambda k, d="": rows[k] if k in rows else d)
    monkeypatch.setattr("app.config.set_config",
                        lambda k, v: rows.__setitem__(k, v))
    monkeypatch.setattr(rr, "RERANK_PROVIDER_DEFAULT", env_provider)
    return rows


def _recorder(seen: dict, raise_on: str | None = None):
    def run_cohort(questions, top_k, misses_only, label="", stamp=False):
        seen[label] = {"provider": rr.rerank_provider(),
                       "enabled": rr.rerank_enabled(),
                       "tag_model": rr.scoring_model()}
        if label == raise_on:
            raise RuntimeError(f"arm {label} failed mid-run")
        return {"label": label, "hits": 0, "scored": 0, "pct": 0.0, "rank1": 0,
                "ndcg": None, "mrr": None, "rerank_p50": None,
                "retrieve_total_s": 0.0, "misses": set()}
    return run_cohort


@pytest.mark.parametrize("rows", [dict(OPERATOR_ROWS), {}],
                         ids=["row-and-env-say-hosted", "env-only-says-hosted"])
def test_a_local_model_arm_runs_on_local_when_the_host_says_hosted(monkeypatch, rows):
    _host(monkeypatch, rows, "hosted-api")
    seen: dict = {}
    monkeypatch.setattr(ev, "run_cohort", _recorder(seen))
    assert ev.run_ab([], 5, False)
    cfgs = dict(ev.AB_CONFIGS)
    wrong = {label: seen[label] for label in LOCAL_ARMS
             if seen[label]["provider"] != "local"}
    assert not wrong, wrong
    for label in ("ms-marco", "int8-cpu"):
        assert seen[label]["enabled"] is True
        # The run's tag (status()["model"]) names the local model the arm
        # loads, not the hosted model the vendor would have scored with.
        assert seen[label]["tag_model"] == cfgs[label]["rerank_model"], (label, seen[label])
    assert seen["no-reranker"]["enabled"] is False


def test_the_gpu_remote_arm_still_runs_on_remote_http(monkeypatch):
    _host(monkeypatch, dict(OPERATOR_ROWS), "hosted-api")
    seen: dict = {}
    monkeypatch.setattr(ev, "run_cohort", _recorder(seen))
    assert ev.run_ab([], 5, False)
    assert seen["gpu-remote"]["provider"] == "remote-http"
    assert seen["gpu-remote"]["tag_model"] == dict(ev.AB_CONFIGS)["gpu-remote"]["rerank_model"]


def test_the_run_leaves_the_config_it_found(monkeypatch):
    rows = _host(monkeypatch, dict(OPERATOR_ROWS), "local")
    monkeypatch.setattr(ev, "run_cohort", _recorder({}))
    assert ev.run_ab([], 5, False)
    for k, v in OPERATOR_ROWS.items():
        assert rows[k] == v, (k, rows[k])
    assert rr.rerank_provider() == "hosted-api"


def test_an_arm_that_raises_still_leaves_the_config_it_found(monkeypatch):
    rows = _host(monkeypatch, dict(OPERATOR_ROWS), "local")
    seen: dict = {}
    monkeypatch.setattr(ev, "run_cohort", _recorder(seen, raise_on="int8-cpu"))
    with pytest.raises(RuntimeError):
        ev.run_ab([], 5, False)
    assert "int8-cpu" in seen
    for k, v in OPERATOR_ROWS.items():
        assert rows[k] == v, (k, rows[k])
    assert rr.rerank_provider() == "hosted-api"


def test_a_provider_with_no_row_reads_as_it_did(monkeypatch):
    """No row: the snapshot reads "" and the restore writes a blank row, which
    _cfg reads as unset - the env default, exactly what the absent row read."""
    rows = _host(monkeypatch, {}, "hosted-api")
    monkeypatch.setattr(ev, "run_cohort", _recorder({}))
    before = rr.rerank_provider()
    assert ev.run_ab([], 5, False)
    assert rows.get("rerank_provider", "") == ""
    assert rr.rerank_provider() == before == "hosted-api"
