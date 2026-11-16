"""Shape pins on scripts/judge_calibration.py, the planted-case suite for
app/eval_judge.py (whose calibration method says to re-run it whenever a
prompt changes).

The real run costs one judge call per case and is never made here: the
judge's model call (app.eval_judge.stream_chat, where
test_honesty_judge_persona.py stubs it) is a stub, and the script's
judge-model lookup is a stub, so nothing reaches a network, a provider or a
model. What is pinned: the runner imports; every rubric eval_judge.py defines
runs with at least one planted PASS and one planted FAIL case; ids are
unique; every case binds to its judge and carries the fields that judge
guards on; and a run makes one judge call per case and reports by verdict.
"""
import datetime
import importlib.util
import inspect
import json
import pathlib
import re

import pytest

import app.eval_judge as judge_mod


def _calibration():
    """Load the script as a module without putting scripts/ on sys.path."""
    p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "judge_calibration.py"
    spec = importlib.util.spec_from_file_location("judge_calibration_shape_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cal():
    return _calibration()


def _judge_functions():
    return {name for name, fn in vars(judge_mod).items()
            if name.startswith("judge_") and inspect.isfunction(fn)
            and fn.__module__ == judge_mod.__name__}


def _all_cases(cal):
    return [(fn.__name__, c, e, kw) for _, cases, fn in cal.RUBRICS
            for c, e, kw in cases]


def test_the_runner_covers_every_rubric_the_judge_defines(cal):
    """FIX: the runner's rubric list is every judge_* function eval_judge.py
    defines, each the module's own - a new rubric cannot land without
    planted cases, and a dropped one fails here."""
    names = [fn.__name__ for _, _, fn in cal.RUBRICS]
    assert sorted(names) == sorted(_judge_functions())
    for _, _, fn in cal.RUBRICS:
        assert fn is getattr(judge_mod, fn.__name__)


def test_every_rubric_has_a_planted_pass_and_a_planted_fail(cal):
    """FIX: both verdicts on every rubric - a judge that passed everything,
    or failed everything, cannot read clean on any of them."""
    for name, cases, _ in cal.RUBRICS:
        assert {e for _, e, _ in cases} == {0, 1}, name


def test_case_ids_are_unique_across_every_rubric(cal):
    """CONTROL: an id names one case - a duplicate would make a MISSED line
    ambiguous."""
    ids = [c for _, c, _, _ in _all_cases(cal)]
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


_REQUIRED = {"judge_answer": ("question", "notes", "answer"),
             "judge_faithfulness": ("question", "grounding", "answer"),
             "judge_freshness": ("question", "grounding", "notes"),
             "judge_honesty": ("question", "notes", "answer"),
             "judge_retired": ("question", "notes", "answer")}


def test_every_case_binds_to_its_judge_and_carries_what_it_needs(cal):
    """CONTROL: every case's arguments bind to the judge it runs through (a
    mismatch is a TypeError mid-run), and every field that judge returns
    unscored without is a non-empty string. Honesty and retired take empty
    grounding by design, so only the guarded fields are pinned."""
    assert set(_REQUIRED) == _judge_functions()
    for fn_name, c, e, kw in _all_cases(cal):
        inspect.signature(getattr(judge_mod, fn_name)).bind(model="m", **kw)
        assert e in (0, 1), c
        for field in _REQUIRED[fn_name]:
            v = kw.get(field)
            assert isinstance(v, str) and v.strip(), (c, field)


def test_the_negotiation_dossier_case_pins_this_rubrics_wording(cal):
    """The comp rule's negotiation-context clause names its examples in this
    repo's own words: outside assessments agreeing on a level, a figure
    judged reasonable, engagement or pricing structures, a justification for
    a premium. The planted FAIL carries each, so it pins the rule as written
    here and not a differently worded copy."""
    rubric = judge_mod._JUDGE_SYSTEM
    for phrase in ("outside assessments agree on a level",
                   "a figure was judged reasonable",
                   "engagement or pricing structures",
                   "justification arguments for a premium"):
        assert phrase in rubric, phrase
    case = {c: (e, kw) for c, e, kw in cal.CORRECTNESS_CASES}[
        "c-fail-guardrail-negotiation-dossier"]
    assert case[0] == 0
    answer = case[1]["answer"]
    for phrase in ("outside assessments agree on a consistent level",
                   "judged your own counter-figure reasonable",
                   "engagement structure is a fixed-price pilot",
                   "justification for a premium", "without figures"):
        assert phrase in answer, phrase
    assert "$" not in answer


def test_the_freshness_tool_pair_differs_only_in_the_state(cal):
    """The stale tool result's PASS twin shares the question, the key and the
    block's label line - only the state inside the block differs."""
    by_id = {c: (e, kw) for c, e, kw in cal.FRESHNESS_CASES}
    fail_e, fail = by_id["fr-fail-stale-tool-result"]
    pass_e, twin = by_id["fr-pass-current-tool-result"]
    assert (fail_e, pass_e) == (0, 1)
    assert fail["question"] == twin["question"]
    assert fail["notes"] == twin["notes"]
    assert fail["grounding"].splitlines()[0] == twin["grounding"].splitlines()[0]
    assert fail["grounding"] != twin["grounding"]


def test_the_hybrid_cases_share_one_synthetic_persona(cal):
    """The honesty and faithfulness hybrid cases answer under one persona and
    one grounding, so the two rubrics are calibrated against one fixture."""
    personas = {kw["persona"] for cases in (cal.FAITHFULNESS_CASES, cal.HONESTY_CASES)
                for _, _, kw in cases if "persona" in kw}
    assert personas == {cal._PERSONA_HYBRID, cal._PERSONA_PLATFORM}
    faith = {c: kw for c, _, kw in cal.FAITHFULNESS_CASES}
    assert faith["f-pass-persona-identity"]["grounding"] == cal._GROUNDING_SERVICES


def test_the_judge_model_is_read_the_way_the_eval_runner_reads_it(cal, monkeypatch):
    """CONTROL: the config pin (a blank row not beating runtime_config's
    default), resolved by the model catalog - the instrument real rows are
    graded with."""
    from app.runtime_config import EVAL_JUDGE_MODEL_DEFAULT
    monkeypatch.setattr(cal.model_catalog, "resolve", lambda m: f"resolved:{m}")
    monkeypatch.setattr(cal, "_config_or_default", lambda key, default: default)
    assert cal._judge_model() == f"resolved:{EVAL_JUDGE_MODEL_DEFAULT}"
    monkeypatch.setattr(cal, "_config_or_default", lambda key, default: "pinned-judge")
    assert cal._judge_model() == "resolved:pinned-judge"


def _stub_judge(monkeypatch, cal, verdict):
    """Every judge call answers `verdict` (True/False), or raises when it is
    None. Returns the list of calls the stub saw."""
    calls = []

    def _stream(msgs, model, tools=None, system_prompt="", max_tokens=1024):
        calls.append((model, msgs[0]["content"], msgs[1]["content"]))
        if verdict is None:
            raise RuntimeError("stub judge unavailable")
        yield json.dumps({"pass": verdict, "rationale": "stub verdict"})

    monkeypatch.setattr(judge_mod, "stream_chat", _stream)
    monkeypatch.setattr(cal, "PAUSE", 0)
    monkeypatch.setattr(cal, "_judge_model", lambda: "stub-judge")
    return calls


def _marked(out, mark):
    return {m.group(1) for m in re.finditer(
        r"^\s*\[\s*" + mark + r"\]\s+(\S+)", out, re.M)}


@pytest.mark.parametrize("verdict", [True, False])
def test_a_run_makes_one_judge_call_per_case_and_grades_by_verdict(
        cal, monkeypatch, capsys, verdict):
    """FIX: running the suite needs the judge - one model call per planted
    case, through each rubric's own judge function, on the pinned model -
    and a judge that answers the same way every time misses exactly the
    other side's cases, so the run exits 1 naming them."""
    calls = _stub_judge(monkeypatch, cal, verdict)
    code = cal.main()
    out = capsys.readouterr().out
    cases = _all_cases(cal)
    assert len(calls) == len(cases)
    assert {m for m, _, _ in calls} == {"stub-judge"}
    today = datetime.date.today().isoformat()
    assert all(u.startswith(f"Today's date: {today}") for _, _, u in calls)
    # Each case's own question reached the judge.
    for _, c, _, kw in cases:
        block = f"<<<BEGIN QUESTION>>>\n{kw['question']}\n<<<END QUESTION>>>"
        assert any(block in u for _, _, u in calls), c
    expected_missed = {c for _, c, e, _ in cases if e != int(verdict)}
    assert code == 1
    assert _marked(out, "MISSED") == expected_missed
    assert f"{len(cases) - len(expected_missed)}/{len(cases)} verdicts correct" in out


def test_a_judge_that_errors_leaves_every_case_unscored(cal, monkeypatch, capsys):
    """CONTROL: a judge failure is unscored (exit 2), never a verdict."""
    calls = _stub_judge(monkeypatch, cal, None)
    code = cal.main()
    out = capsys.readouterr().out
    cases = _all_cases(cal)
    assert code == 2
    assert len(calls) == len(cases)
    assert _marked(out, "UNSCORED") == {c for _, c, _, _ in cases}
