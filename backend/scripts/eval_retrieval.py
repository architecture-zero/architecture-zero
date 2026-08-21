"""Retrieval AND answer-layer eval with a same-process visibility PRE-FLIGHT.

This is the codified valid runner for retrieval recall numbers outside the
app UI. The measurement lesson behind it: an eval running as a SECOND python
process against Chroma can see a different index view than the app process
that owns it - phantom misses that the live pipeline retrieves at rank 1
minutes later. Rule: eval numbers only count from runs with same-process
visibility verification (or the in-app path). The pre-flight below IS that
verification: this process confirms every expected source is visible to ITS
OWN Chroma handle, with chunk counts, BEFORE scoring. If any expected source
is invisible, it exits 2 and the numbers do not count.

Scoring is the app's own (_score_retrieval) over the app's own pipeline
(app.rerank.retrieve) with the app's own question set (EvalQuestion) -
like-for-like with the in-app run (top_k=5 kept after rerank).

Run inside the backend container (where the corpus lives):

  docker compose exec backend python scripts/eval_retrieval.py
  docker compose exec backend python scripts/eval_retrieval.py --misses-only
  docker compose exec backend python scripts/eval_retrieval.py --answers --repeat 3
  docker compose exec backend python scripts/eval_retrieval.py --ab --misses-only

Throttled like the in-app job (EVAL_QUESTION_PAUSE_SECONDS between
questions, default 2s) - an unthrottled run can freeze a shared VM. The
RETRIEVAL path is read-only (writes no EvalResult rows). ANSWER mode
(--answers) does persist EvalResult rows - it runs the same in-process job
the app uses, so its runs appear alongside in-app ones.

Operational notes:
- Budget generously on a contended box (embed + wide fetch + CPU
  cross-encoder + the pause, per question) - and stdout is pipe-buffered
  through docker exec, so silence until exit is normal.
- An interrupted ssh client does NOT stop the remote process - the
  container-side python keeps running as an orphan (parented by Docker, not
  the ssh session). The lock below refuses a second concurrent run (two runs
  double memory and halve each other's speed); `pgrep -f eval_retrieval`
  finds an orphan to kill.
- The full report is ALSO written to /tmp/eval_retrieval-last-report.txt
  inside the container, so a dead pipe (slept laptop, dropped ssh) cannot
  lose the numbers: `docker compose exec backend cat` it back.
- Importing app.main costs real memory (the model stack rides along). It
  must not start schedulers at import time - background jobs belong in
  startup events, which only fire under the real server.
"""
import argparse
import math
import os
import sys
import time

LOCK_PATH = "/tmp/eval_retrieval.lock"
REPORT_PATH = "/tmp/eval_retrieval-last-report.txt"


class _Tee:
    """Mirror stdout to REPORT_PATH so a broken pipe (dropped ssh / slept
    laptop) can't lose a completed run's numbers. The file write is the one
    that must survive; the console write is best-effort."""

    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.console = sys.stdout

    def write(self, s):
        self.f.write(s)
        self.f.flush()
        try:
            self.console.write(s)
            self.console.flush()
        except Exception:
            pass  # pipe gone; the file still has everything

    def flush(self):
        self.f.flush()


def _acquire_lock() -> bool:
    """Single-run guard: a concurrent eval doubles box load and halves both
    runs' speed. Stale locks (dead pid) are reclaimed."""
    if os.path.exists(LOCK_PATH):
        try:
            pid = int(open(LOCK_PATH).read().strip())
        except Exception:
            pid = None
        if pid and os.path.exists(f"/proc/{pid}"):
            print(f"Another eval_retrieval run is active (pid {pid}, lock "
                  f"{LOCK_PATH}). Refusing a concurrent run - kill it or wait.")
            return False
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    return True

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import _score_retrieval  # noqa: E402  (module init is idempotent)
from app.db import get_session          # noqa: E402
from app.models import EvalQuestion     # noqa: E402
from app.rerank import retrieve         # noqa: E402
from app.database import list_sources   # noqa: E402

PAUSE = float(os.getenv("EVAL_QUESTION_PAUSE_SECONDS", "2.0"))

# A/B ARMS. Each arm flips the LIVE config keys, which rerank.py reads per
# call (`_cfg`), so an arm is a config change rather than a container restart
# - and the process that serves retrieval is the one that sees it. The driver
# restores the prior values in a `finally`, so an interrupted run cannot
# leave the instance on an experimental configuration.
#
# Every arm sets rerank_provider EXPLICITLY (blank = the env default, local):
# arms run in sequence over live config, so an unlisted key would leak from a
# previous arm - the gpu-remote arm must never leave remote-http behind for a
# local arm to inherit. gpu-remote scores the SAME fp32 model off-box via
# rerank.py's remote-http provider (any endpoint speaking that contract) -
# the hardware is the variable, so accuracy should hold by construction and
# the run measures whether it actually does, plus the latency. The int8 arm
# is a rerank_model flip (rerank.py registers the quantized ONNX as a custom
# model); its speedup is architecture-dependent - measure, never assume.
AB_CONFIGS = [
    ("no-reranker", {"rerank_enabled": "false", "rerank_model": "",
                     "rerank_provider": ""}),
    ("ms-marco",    {"rerank_enabled": "true",
                     "rerank_model": "Xenova/ms-marco-MiniLM-L-6-v2",
                     "rerank_provider": ""}),
    ("int8-cpu",    {"rerank_enabled": "true",
                     "rerank_model": "Xenova/ms-marco-MiniLM-L-6-v2-int8",
                     "rerank_provider": ""}),
    ("gpu-remote",  {"rerank_enabled": "true",
                     "rerank_model": "Xenova/ms-marco-MiniLM-L-6-v2",
                     "rerank_provider": "remote-http"}),
]


def _relevance(needles: list[str], sources: list[str]) -> list[int]:
    """Binary relevance per returned chunk, using the same matcher as
    scoring."""
    return [1 if any(_matches(nd, s) for nd in needles) else 0 for s in sources]


def _dcg(rels: list[int]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def _ndcg(rels: list[int]) -> float:
    """nDCG over the returned list, normalised against the best ORDERING of
    the same chunks. This is the ordering metric a reranker is actually
    judged on - recall (hit/rank) is a set metric and cannot see the
    difference between the answer at rank 1 and the answer at rank 5.

    Ideal = these same relevance values sorted desc, so a run that retrieves
    one relevant chunk and puts it first scores 1.0. When NOTHING relevant
    was retrieved the ideal is empty and the ratio is undefined; that is
    reported as 0.0, not skipped - excluding misses would let a system that
    retrieves less score higher than one that retrieves more."""
    ideal = _dcg(sorted(rels, reverse=True))
    return round(_dcg(rels) / ideal, 4) if ideal else 0.0


def _reciprocal_rank(rels: list[int]) -> float:
    for i, r in enumerate(rels, start=1):
        if r:
            return 1.0 / i
    return 0.0


def _mean(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 4) if vals else None


def _needles(expected_source: str) -> list[str]:
    """Mirror _score_retrieval's needle parsing: '|' alternates, per-needle
    'local:' scheme strip, lowercase."""
    needles = [n.strip().split(":", 1)[-1].strip().lower()
               for n in (expected_source or "").lower().split("|")]
    return [n for n in needles if n]


def _matches(needle: str, source: str) -> bool:
    s = source.lower()
    return needle in s or s.rsplit("/", 1)[-1] == needle.rsplit("/", 1)[-1]


def fetch_questions():
    """Every eval question EXCEPT the mechanically-graded 'injection' cohort.
    That cohort runs ONLY via the in-app job, which supplies the hostile
    material it measures against: a transient poisoned fixture planted and
    then restored bit-for-bit. This script does not plant fixtures, so
    running those questions here would record VACUOUS holds (the payload
    never reached the model - the exact failure the cohort's own
    reached/rode count exists to expose), and the pre-flight would demand a
    fixture doc that only exists mid-run."""
    with get_session() as db:
        rows = db.query(EvalQuestion).filter(
            EvalQuestion.category.notin_(("injection",))).order_by(
            EvalQuestion.category, EvalQuestion.id).all()
        return [type("Q", (), {
            "id": r.id, "question": r.question, "category": r.category,
            "expected_source": r.expected_source, "notes": r.notes,
            "as_level": r.as_level, "holdout": r.holdout,
            "setup_turns": r.setup_turns})() for r in rows]


def preflight(questions) -> bool:
    """Verify every expected source is visible to THIS process's Chroma
    handle. Prints the marker-source chunk counts; returns False if anything
    expected is invisible (then the run's numbers must not be trusted)."""
    sources = list_sources()  # all collections, [{source, count, department}]
    print(f"\n=== PRE-FLIGHT: corpus visibility from THIS process "
          f"({len(sources)} sources) ===")
    missing = []
    seen_needles = set()
    for q in questions:
        for nd in _needles(q.expected_source or ""):
            if nd in seen_needles:
                continue
            seen_needles.add(nd)
            hits = [s for s in sources if _matches(nd, s["source"])]
            total = sum(s["count"] for s in hits)
            depts = ",".join(sorted({s["department"] for s in hits})) or "-"
            print(f"  {'OK ' if hits else 'MISSING'} {nd:<44} "
                  f"chunks={total:<5} depts={depts}")
            if not hits:
                missing.append(nd)
    if missing:
        print(f"\nPRE-FLIGHT FAILED: {len(missing)} expected source(s) invisible "
              f"to this process: {missing}")
        print("Numbers from this run would NOT count. Aborting.")
        return False
    print("pre-flight clean: every expected source is visible.")

    # KNN HEALTH PROBE. The visibility check above is col.get, which can stay
    # green for weeks while a collection's HNSW index is dead to search - knn
    # refuses even k=1, every layer fails open, the eval silently runs
    # WITHOUT that collection, and the score moves with nothing errored.
    # Visibility is not retrievability: probe every non-empty collection with
    # an actual knn query (using one of its own stored embeddings, so the
    # probe cannot fail on dimension) and refuse to measure if any index
    # cannot search.
    from app.database import client, collection_metadata
    print("=== PRE-FLIGHT 2: knn search health per collection ===")
    broken = []
    for col_info in client.list_collections():
        col = client.get_or_create_collection(name=col_info.name,
                                              metadata=collection_metadata())
        n = col.count()
        if not n:
            continue
        try:
            got = col.get(limit=1, include=["embeddings"])
            # NEVER `or` on this: chroma returns embeddings as a numpy array,
            # and bool(ndarray) raises - which would make THIS probe report
            # every healthy collection as BROKEN (tests mock chroma, so only
            # a live run catches it). Mirror the app's own `is not None`
            # handling (database.py rescue leg).
            embs = got.get("embeddings")
            emb = (list(embs[0]) if embs is not None and len(embs)
                   else [0.0] * 768)
            col.query(query_embeddings=[emb], n_results=min(5, n))
            print(f"  OK      {col_info.name:<24} ({n} chunks)")
        except Exception as e:
            print(f"  BROKEN  {col_info.name:<24} ({n} chunks): {e}")
            broken.append(col_info.name)
    if broken:
        print(f"\nPRE-FLIGHT FAILED: {len(broken)} collection(s) refuse knn search "
              f"({broken}). Retrieval would silently run WITHOUT them. Rebuild "
              f"the collection(s) before measuring.")
        return False
    print("knn probe clean: every non-empty collection can actually search.")
    return True


def _pass_metrics(rows: list[dict]) -> dict:
    """Aggregate one answer pass. Module-level so the math is unit-testable.

    Emits BOTH raw and error-ADJUSTED numbers. An errored answer (writer API
    failure) auto-fails the raw score - correct for product honesty, the user
    got nothing - but a "noise band" built on raw scores conflates model
    nondeterminism with API availability: repeated passes have been observed
    tracking writer-API error bursts in exact arithmetic proportion. The
    adjusted numbers exclude errored rows entirely (from numerator AND
    denominator), isolating the model term. Two numbers, never blended:
    raw = what a user experienced, adjusted = what the model knows.

    The honesty cohort carries its OWN aggregate, never the tuned headline -
    a surface cohort wearing the tuned label the hour its category exists is
    the failure this split prevents.
    """
    def pct(num, den):
        return round(100 * num / den, 1) if den else None

    def split(pred):
        sub = [r for r in rows if pred(r)]
        ok = [r for r in sub if not r["errored"]]
        return (pct(sum(1 for r in sub if r["score"] == 1),
                    sum(1 for r in sub if r["score"] is not None)),
                pct(sum(1 for r in ok if r["score"] == 1),
                    sum(1 for r in ok if r["score"] is not None)))

    tuned, tuned_adj = split(lambda r: not r["holdout"]
                             and r["cat"] != "honesty")
    hold, hold_adj = split(lambda r: r["holdout"])
    hon, hon_adj = split(lambda r: r["cat"] == "honesty")
    faith_rows = [r for r in rows if not r["holdout"]
                  and r["cat"] != "honesty"]
    out = {
        "rows": len(rows),
        "errored": sum(1 for r in rows if r["errored"]),
        "corpus": sorted({r["corpus"] for r in rows if r.get("corpus")}),
        "tuned": tuned, "tuned_adj": tuned_adj,
        "holdout": hold, "holdout_adj": hold_adj,
        "honesty": hon, "honesty_adj": hon_adj,
        "faith": pct(sum(1 for r in faith_rows if r["faith"] == 1),
                     sum(1 for r in faith_rows if r["faith"] is not None)),
        "fresh": pct(sum(1 for r in faith_rows if r["fresh"] == 1),
                     sum(1 for r in faith_rows if r["fresh"] is not None)),
    }
    for k in ("", "_adj"):
        t, h = out[f"tuned{k}"], out[f"holdout{k}"]
        out[f"gap{k}"] = round(t - h, 1) if t is not None and h is not None else None
    return out


def run_answers(questions, n_results, label=""):
    """A full ANSWER-mode pass, IN THIS PROCESS.

    Why this exists: measuring the answer layer only through the app's HTTP
    eval endpoints means every published answer-layer number is a SINGLE run
    with no idea of its own variance - and repeated identical-configuration
    runs can spread several correctness points. Any delta smaller than that
    spread is noise. This is the tool that makes a claim checkable.

    Deliberately not over HTTP: an access token expires mid-pass, and when
    the poller starts 401ing the driver loses track of which run finished -
    the way two runs end up overlapping and racing each other's
    configuration. In-process means no token, no poller, and the lock this
    script already holds is the only sequencing needed.
    """
    import datetime as _dt
    import uuid as _uuid

    from app.main import _run_eval_job, DEFAULT_MODEL, EVAL_JUDGE_MODEL_DEFAULT
    from app.config import get_config
    from app.providers import _provider_for_model
    from app.models import EvalResult

    def _cfg_or_blank(key: str) -> str:
        """A config row that EXISTS but is BLANK must not win the fallback
        chain: a blank `default_model` row otherwise resolves the writer to
        the empty string, every answer errors, and the run reports 0% as
        though that were a measurement. `get_config(key, default)` returns
        the stored empty string rather than the default, so the emptiness
        has to be checked here."""
        return (get_config(key, "") or "").strip()

    model = (_cfg_or_blank("eval_answer_model") or _cfg_or_blank("default_model")
             or DEFAULT_MODEL)
    judge = _cfg_or_blank("eval_judge_model") or EVAL_JUDGE_MODEL_DEFAULT
    if _provider_for_model(model) == _provider_for_model(judge):
        print(f"  ABORT: writer {model} and judge {judge} are the same provider family - "
              f"the scores would be self-graded.")
        return None

    run_id = str(_uuid.uuid4())
    run_at = _dt.datetime.utcnow().isoformat()
    payload = [{"id": q.id, "question": q.question, "category": q.category,
                "expected_source": q.expected_source, "notes": q.notes,
                "as_level": q.as_level, "holdout": q.holdout or 0,
                "setup_turns": getattr(q, "setup_turns", None)} for q in questions]
    print(f"\n=== ANSWER RUN{' ' + label if label else ''}: {len(payload)} questions | "
          f"writer {model} | judge {judge} | run_id {run_id}", flush=True)

    _run_eval_job(run_id, run_at, payload, model, True, n_results, False)

    with get_session() as db:
        rows = [{"score": r.score, "faith": r.faithfulness, "fresh": r.freshness,
                 "cat": r.category, "holdout": r.holdout,
                 "corpus": r.corpus_fingerprint,
                 "errored": (r.response or "").startswith("[ERROR:")}
                for r in db.query(EvalResult).filter(EvalResult.run_id == run_id).all()]

    out = {"label": label, "run_id": run_id, **_pass_metrics(rows)}
    print(f"  tuned {out['tuned']} | holdout {out['holdout']} | GAP {out['gap']} | "
          f"faith {out['faith']} | fresh {out['fresh']} | honesty {out['honesty']} | "
          f"errored {out['errored']}/{out['rows']}", flush=True)
    if out["errored"]:
        print(f"  ERROR-ADJUSTED (writer-API failures excluded): tuned {out['tuned_adj']} "
              f"| holdout {out['holdout_adj']} | GAP {out['gap_adj']} - the raw line "
              f"includes API availability; this one isolates the model.", flush=True)
    print(f"  corpus {out['corpus'] or ['UNSTAMPED']}", flush=True)
    if len(out["corpus"]) > 1:
        print("  WARNING: the corpus CHANGED mid-pass (the watcher re-ingested while "
              "this ran) - these rows did not all measure the same thing.", flush=True)
    return out


def run_cohort(questions, top_k, misses_only, label="", stamp=False):
    """One retrieval pass over the cohort at the CURRENT configuration.

    Factored out of main() so the A/B driver can run it once per arm. Adds
    PER-ARM TIMING (retrieve wall-clock, and the rerank slice of it) and
    ORDERING metrics (nDCG@k, MRR). Recall answers "did the answer come back
    at all"; a reranker is judged on where it PUT it, which recall cannot
    see.
    """
    import app.rerank as rr

    state = rr.status()
    tag = state["model"] if state["enabled"] else "no reranker"
    # MISLABELLED-ARM GUARD: if reranking is ON but the encoder did not load,
    # rerank() silently falls back to retriever order - so the arm would
    # measure the no-reranker system while wearing the reranker's label.
    # Refuse instead of publishing a mislabelled row.
    if state["enabled"] and not state["loaded"]:
        print(f"  ABORT: reranking is on but the model did not load "
              f"({state['error']}). A run now would silently measure the fallback.")
        return None

    print(f"\n=== RUN{' ' + label if label else ''}: {len(questions)} questions, "
          f"top_k={top_k}, retrieval={tag}, pause={PAUSE}s ===", flush=True)

    # Time the rerank slice without reimplementing anything: rerank() is a
    # module global that retrieve() looks up at call time, so wrapping it
    # here measures the real call and leaves behaviour alone.
    real_rerank = rr.rerank
    slice_ms: dict[str, int] = {}
    pool_sizes: list[int] = []

    def timed_rerank(query, candidates, top_k=None):
        pool_sizes.append(len(candidates))
        t0 = time.time()
        out = real_rerank(query, candidates, top_k)
        slice_ms["last"] = int((time.time() - t0) * 1000)
        return out

    rr.rerank = timed_rerank

    scored = hits = rank1 = unscored = 0
    per_cat: dict[str, list[int]] = {}
    misses = []
    ndcgs: list[float] = []
    rrs: list[float] = []
    retrieve_times: list[int] = []
    rerank_times: list[int] = []
    try:
        for q in questions:
            query = q.question
            if getattr(q, "setup_turns", None):
                import json as _json
                from app.routing import resolve_followup
                try:
                    setup = _json.loads(q.setup_turns) or []
                except Exception:
                    setup = []
                if setup:
                    query = resolve_followup(q.question, setup)

            slice_ms.pop("last", None)
            t0 = time.time()
            results = retrieve(query, top_k=top_k)
            retrieve_times.append(int((time.time() - t0) * 1000))
            if "last" in slice_ms:
                rerank_times.append(slice_ms["last"])

            sources = [r.get("source", "unknown") for r in results]
            hit, rank = _score_retrieval(q.expected_source, sources)
            if hit is None:
                unscored += 1
            else:
                scored += 1
                rels = _relevance(_needles(q.expected_source), sources)
                ndcgs.append(_ndcg(rels))
                rrs.append(_reciprocal_rank(rels))
                cat = per_cat.setdefault(q.category or "?", [0, 0])
                cat[1] += 1
                if hit:
                    hits += 1
                    cat[0] += 1
                    if rank == 1:
                        rank1 += 1
                else:
                    misses.append((q, sources, results))
            if not misses_only and hit is not None:
                mark = f"HIT @{rank}" if hit else "MISS"
                print(f"  [{mark:>7}] {retrieve_times[-1]:>6}ms ({q.category}) "
                      f"{q.question[:66]}", flush=True)
            time.sleep(PAUSE)
    finally:
        rr.rerank = real_rerank

    pct = round(100 * hits / scored, 1) if scored else 0.0
    rt = sorted(retrieve_times)
    kt = sorted(rerank_times)
    out = {
        "label": label or tag, "config": tag,
        "hits": hits, "scored": scored, "pct": pct, "rank1": rank1,
        "unscored": unscored,
        "ndcg": _mean(ndcgs), "mrr": _mean(rrs),
        "retrieve_p50": rt[len(rt) // 2] if rt else None,
        "retrieve_total_s": round(sum(rt) / 1000.0, 1),
        "rerank_p50": kt[len(kt) // 2] if kt else None,
        "pool_avg": round(sum(pool_sizes) / len(pool_sizes), 1) if pool_sizes else None,
        "misses": {q.id for q, _, _ in misses},
    }
    print(f"\n  recall {hits}/{scored} = {pct}%   rank-1 {rank1}/{hits}   "
          f"unscored {unscored}")
    print(f"  nDCG@{top_k} {out['ndcg']}   MRR {out['mrr']}")
    print(f"  retrieve p50 {out['retrieve_p50']}ms   rerank p50 {out['rerank_p50']}ms   "
          f"avg pool {out['pool_avg']}   cohort wall-clock {out['retrieve_total_s']}s")
    print("  per category:")
    for cat, (h, n) in sorted(per_cat.items()):
        print(f"    {cat:<24} {h}/{n}")
    if misses:
        print(f"\n  MISSES ({len(misses)}):")
        for q, sources, results in misses:
            print(f"    Q{q.id} ({q.category}): {q.question[:76]}")
            print(f"        expected: {q.expected_source}")
            for i, r in enumerate(results, 1):
                print(f"        {i}. {r.get('source', '?'):<34} "
                      f"rerank={r.get('rerank_score')}")

    # Stamp the panel ONLY for a plain single-configuration run. An A/B arm
    # is an experiment, and writing an arm's number into rag_metrics.json
    # would publish "the system's recall" as whatever the last arm happened
    # to be.
    if stamp and scored:
        import datetime as _dt
        from app.main import _persist_rag_metric
        _persist_rag_metric("script:eval_retrieval",
                            _dt.datetime.utcnow().isoformat(),
                            {"pct": pct, "hits": hits, "total": scored})
        print(f"  stamped data/rag_metrics.json (run_id script:eval_retrieval)")
    return out


def run_ab(questions, top_k, misses_only, arms: list[str] | None = None):
    """Every arm over the SAME cohort and corpus, retrieval the only
    variable. `arms` filters AB_CONFIGS by label (an adjudicated arm need not
    re-run); order and same-pass discipline are preserved for whatever
    remains."""
    import app.rerank as rr
    from app.config import get_config, set_config

    configs = AB_CONFIGS
    if arms:
        unknown = set(arms) - {label for label, _ in AB_CONFIGS}
        if unknown:
            print(f"ABORT: unknown arm label(s) {sorted(unknown)} - "
                  f"valid: {[label for label, _ in AB_CONFIGS]}")
            return None
        configs = [(label, cfg) for label, cfg in AB_CONFIGS if label in arms]

    # Snapshot the UNION of keys any arm flips - restoring a subset would
    # leave a later-added key stuck on the last arm's value after the run.
    all_keys = sorted({k for _, cfg in AB_CONFIGS for k in cfg})
    prior = {k: get_config(k, "") for k in all_keys}
    results = []
    try:
        for label, cfg in configs:
            for k, v in cfg.items():
                set_config(k, v)
            # Evict the cached encoder so a model change actually LOADS the
            # new model instead of serving the previous one under a new
            # label.
            rr._encoders.clear()
            rr._load_errors.clear()
            out = run_cohort(questions, top_k, misses_only, label=label)
            if out is None:
                return None
            results.append(out)
    finally:
        for k, v in prior.items():
            set_config(k, v)
        rr._encoders.clear()
        rr._load_errors.clear()
        print(f"\nconfig restored: {prior}")

    print("\n=== A/B RESULT (same cohort, same corpus, retrieval is the only variable) ===")
    print(f"  {'arm':<14} {'recall':>13} {'rank-1':>7} {'nDCG':>7} {'MRR':>7} "
          f"{'rerank p50':>11} {'cohort':>9}")
    for r in results:
        print(f"  {r['label']:<14} {r['hits']:>4}/{r['scored']:<3} {r['pct']:>5}% "
              f"{r['rank1']:>7} {str(r['ndcg']):>7} {str(r['mrr']):>7} "
              f"{str(r['rerank_p50']) + 'ms':>11} {str(r['retrieve_total_s']) + 's':>9}")
    # Pairwise deltas: every arm vs the first (the no-reranker floor), plus
    # each later reranker arm vs the arm before it (the decision pair -
    # rescued/broke between two reranker arms is the accuracy half of a
    # cheaper-arm call).
    def _delta(a, b):
        d_recall = round(b["pct"] - a["pct"], 1)
        d_ndcg = round((b["ndcg"] or 0) - (a["ndcg"] or 0), 4)
        d_time = round(b["retrieve_total_s"] - a["retrieve_total_s"], 1)
        print(f"\n  {b['label']} vs {a['label']}: recall {d_recall:+}pp, "
              f"nDCG {d_ndcg:+}, wall-clock {d_time:+}s over {b['scored']} questions")
        gained = a["misses"] - b["misses"]
        lost = b["misses"] - a["misses"]
        print(f"  questions {b['label']} RESCUED: {sorted(gained) or 'none'}")
        print(f"  questions {b['label']} BROKE:   {sorted(lost) or 'none'}")

    for later in results[1:]:
        _delta(results[0], later)
    for a, b in zip(results[1:], results[2:]):
        _delta(a, b)
    if len(results) > 1:
        print("\n  Retrieval is deterministic, so these deltas need no noise band - "
              "the same arm re-run returns the same set. TIMING is not deterministic; "
              "read the ms columns as this box's load today, not as constants.")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5,
                    help="kept after rerank (5 = the in-app default, baseline-comparable)")
    ap.add_argument("--misses-only", action="store_true",
                    help="only print the per-miss detail, skip per-question lines")
    ap.add_argument("--answers", action="store_true",
                    help="run the ANSWER layer in-process (writer + judges), not just "
                         "retrieval. Retrieval is deterministic; the answer layer is not, "
                         "which is what --repeat is for.")
    ap.add_argument("--ab", action="store_true",
                    help="run the cohort under EACH retrieval arm in AB_CONFIGS in turn, "
                         "flipping the live config keys and restoring them afterwards. "
                         "Retrieval-only; combine with nothing else - the answer layer "
                         "has its own driver.")
    ap.add_argument("--arms", default="",
                    help="comma-separated AB_CONFIGS labels to run (with --ab). An "
                         "adjudicated arm need not re-run; the pass stays same-corpus "
                         "for whatever is listed. Empty = all arms.")
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat the answer pass N times to establish this instance's own "
                         "NOISE BAND. Repeating retrieval is pointless (identical every "
                         "time); repeating the answer layer is the only way to know whether "
                         "a delta is real.")
    args = ap.parse_args()

    questions = fetch_questions()
    if not questions:
        print("No eval questions in the DB.")
        return 1

    if not preflight(questions):
        return 2

    if args.answers:
        passes = [p for p in (run_answers(questions, args.top_k,
                                          label=(f"pass {i + 1}/{args.repeat}"
                                                 if args.repeat > 1 else ""))
                              for i in range(max(1, args.repeat))) if p]
        if not passes:
            return 2
        if len(passes) > 1:
            print("\n=== NOISE BAND (identical configuration, repeated) ===")
            print("  A delta is only claimable if it clears the spread a configuration "
                  "shows against ITSELF.")
            # "Identical configuration" is a claim about the CORPUS too. If
            # the passes did not all see the same corpus, the spread below is
            # not a noise band - it is noise plus corpus drift, and it
            # understates neither honestly.
            corpora = sorted({c for p in passes for c in (p.get("corpus") or [])})
            if len(corpora) > 1:
                print(f"  WARNING: passes did NOT share one corpus ({len(corpora)} distinct "
                      f"fingerprints: {corpora}). The spread below is noise PLUS corpus "
                      f"drift and must not be quoted as this instance's noise band.")
            elif corpora:
                print(f"  corpus (all passes): {corpora[0]}")
            for key, name in (("tuned", "tuned"), ("holdout", "holdout"),
                              ("gap", "GAP"), ("honesty", "honesty")):
                vals = [p[key] for p in passes if p.get(key) is not None]
                if not vals:
                    continue
                spread = round(max(vals) - min(vals), 1)
                mean = round(sum(vals) / len(vals), 1)
                print(f"    {name:<8} mean {mean:>6}  range {min(vals)}-{max(vals)}  "
                      f"spread {spread} across {len(vals)} runs")
            # The raw band above conflates model nondeterminism with API
            # availability. When any pass carried errors, also report the
            # band with errored rows excluded - the model term alone.
            if any(p["errored"] for p in passes):
                print("  --- ERROR-ADJUSTED band (writer-API failures excluded) ---")
                print(f"    errored per pass: {[p['errored'] for p in passes]}")
                for key, name in (("tuned_adj", "tuned"), ("holdout_adj", "holdout"),
                                  ("gap_adj", "GAP")):
                    vals = [p[key] for p in passes if p.get(key) is not None]
                    if not vals:
                        continue
                    spread = round(max(vals) - min(vals), 1)
                    mean = round(sum(vals) / len(vals), 1)
                    print(f"    {name:<8} mean {mean:>6}  range {min(vals)}-{max(vals)}  "
                          f"spread {spread} across {len(vals)} runs")
        return 0

    if args.ab:
        arms = [a.strip() for a in args.arms.split(",") if a.strip()] or None
        return 0 if run_ab(questions, args.top_k, args.misses_only, arms=arms) else 2

    # Single-configuration run: this is the one whose number is panel-worthy,
    # so it stamps. An A/B arm never does (see run_cohort's stamp guard).
    out = run_cohort(questions, args.top_k, args.misses_only, stamp=True)
    return 0 if out else 2


if __name__ == "__main__":
    if not _acquire_lock():
        sys.exit(3)
    try:
        sys.stdout = _Tee(REPORT_PATH)
        rc = main()
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
    sys.exit(rc)
