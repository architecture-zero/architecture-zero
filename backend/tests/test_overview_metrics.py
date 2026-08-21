"""Guards the Overview-dashboard latency aggregates.

The percentile math itself gets pinned: nearest-rank percentiles, None
(never zero) on an empty pool, and NULL durations excluded as honestly
unknown rather than counted as instant answers. Lane exclusion (which rows
count as model-served at all) is pinned separately in
test_answer_lane_ttft.py.
"""
import sys
from pathlib import Path

_APP = Path(__file__).resolve().parent.parent
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.audit import _percentile, latency_summary  # noqa: E402


def test_percentile_empty_is_none_never_zero():
    assert _percentile([], 50) is None
    s = latency_summary([])
    assert s == {"answers_timed": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}


def test_percentile_single_value_is_that_value():
    s = latency_summary([840])
    assert (s["p50_ms"], s["p95_ms"], s["p99_ms"]) == (840, 840, 840)
    assert s["answers_timed"] == 1


def test_percentiles_nearest_rank_on_known_pool():
    durs = list(range(100, 1100, 100))  # 100..1000, ten values
    s = latency_summary(durs)
    assert s["answers_timed"] == 10
    assert s["p50_ms"] == 500
    assert s["p95_ms"] == 1000
    assert s["p99_ms"] == 1000


def test_null_durations_are_excluded_not_counted_as_instant():
    # Audit rows predating the duration column carry NULL - unknown, not 0ms.
    # Counting them would flatter every percentile.
    s = latency_summary([None, None, 900, 1100])
    assert s["answers_timed"] == 2
    assert s["p50_ms"] == 900


def test_unsorted_input_is_handled():
    s = latency_summary([900, 100, 500])
    assert s["p50_ms"] == 500
