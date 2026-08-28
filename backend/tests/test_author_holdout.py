"""Holdout authoring machinery.

The authoring call costs real API money and its CONTENT is deliberately not
ours to shape - so what these tests pin is the mechanical frame the cohort's
independence depends on: parse tolerance, expected_source set by the script
(never trusted from the model), the holdout flag stamped on every item, and
drops happening BY RULE (duplicates, missing fields, over-production) instead
of rewrites.

No test here reaches the network. The author call is monkeypatched on the
script's own namespace, and conftest additionally rebinds requests.post
process-wide to raise - so a future refactor that stopped honouring the patch
would fail loudly rather than start billing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")))

import author_holdout as ah                         # noqa: E402


def test_parse_items_tolerates_model_formatting():
    assert ah._parse_items('[{"question": "q", "notes": "n", "category": "c"}]') == \
        [{"question": "q", "notes": "n", "category": "c"}]
    # code fence + surrounding prose still parse
    out = ah._parse_items('Here you go!\n```json\n[{"question": "q", "notes": "n"}]\n```')
    assert out == [{"question": "q", "notes": "n"}]
    # garbage / wrong shape -> None (dropped by rule), never a guessed batch
    assert ah._parse_items("I could not find suitable questions.") is None
    assert ah._parse_items('{"question": "not a list"}') is None
    assert ah._parse_items("") is None


def _corpus(tmp_path, body_files=(("company/sample.md", 30),)):
    root = tmp_path / "knowledge"
    for rel, reps in body_files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Sample\n" + ("A durable fact about the system. " * reps),
                     encoding="utf-8")
    return root


def _argv(corpus, out_path, count, per_file):
    return ["author_holdout.py", "--count", str(count),
            "--per-file", str(per_file), "--corpus", str(corpus),
            "--out", str(out_path), "--pause", "0"]


def test_authoring_is_mechanical_only(tmp_path, monkeypatch, capsys):
    """End-to-end over a tmp corpus with a mocked author model: valid items are
    taken AS-IS with the script (not the model) setting expected_source and the
    holdout flag; duplicate and incomplete items DROP by rule.

    The dedupe floor is mocked rather than seeded as a real EvalQuestion row -
    the suite shares one SQLite file and another test asserts the exact list of
    database-only questions, which a stray row here would break.
    """
    monkeypatch.setattr(ah, "_existing_question_texts",
                        lambda seed_path: {"Already banked question?"})

    corpus = _corpus(tmp_path)
    batch = [
        # valid - must land verbatim, with script-set source + flag
        {"question": "What durable fact does the sample state?",
         "notes": "Must state the durable fact.", "category": "Support extra words",
         "expected_source": "WRONG-model-claimed.md"},
        # exact duplicate of a banked question - drops by rule
        {"question": "Already banked question?", "notes": "n", "category": "support"},
        # missing notes - drops by rule
        {"question": "No grading key here?", "category": "support"},
    ]

    def _fake_stream(msgs, model, tools=None, system_prompt="", max_tokens=1024):
        yield json.dumps(batch)

    monkeypatch.setattr(ah, "stream_chat", _fake_stream)
    out_path = tmp_path / "holdout.json"
    monkeypatch.setattr(sys, "argv", _argv(corpus, out_path, count=3, per_file=3))

    rc = ah.main()
    assert rc == 2  # fewer than --count survived - usable, loudly reported

    items = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(items) == 1
    item = items[0]
    assert item["question"] == "What durable fact does the sample state?"
    assert item["notes"] == "Must state the durable fact."
    assert item["category"] == "support"         # mechanical normalize only
    assert item["holdout"] == 1                  # stamped by the script

    # expected_source comes from the file the model was SHOWN, never its claim,
    # and it is the BARE relative path this platform's seed file uses. The
    # scorer strips unknown prefixes per needle, so a prefixed form would still
    # score - which is exactly why the convention needs a test rather than a
    # comment. This assertion is the only place that format is pinned.
    assert item["expected_source"] == "company/sample.md"

    report = capsys.readouterr().out
    assert "duplicate question text" in report
    assert "missing question/notes" in report


def test_over_production_is_truncated_by_rule(tmp_path, monkeypatch, capsys):
    """A model that returns more than the requested count does not get to
    weight the exam toward one document. The truncation is a drop by rule and
    keeps the FIRST N, so the outcome does not depend on the model's ordering
    preferences either."""
    monkeypatch.setattr(ah, "_existing_question_texts", lambda seed_path: set())

    corpus = _corpus(tmp_path)
    batch = [{"question": f"Durable question number {n}?",
              "notes": f"Key {n}.", "category": "support"} for n in range(1, 5)]

    def _fake_stream(msgs, model, tools=None, system_prompt="", max_tokens=1024):
        yield json.dumps(batch)

    monkeypatch.setattr(ah, "stream_chat", _fake_stream)
    out_path = tmp_path / "holdout.json"
    monkeypatch.setattr(sys, "argv", _argv(corpus, out_path, count=2, per_file=2))

    ah.main()
    items = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(items) == 2, "a 4-item batch against --per-file 2 must yield 2"
    assert [i["question"] for i in items] == ["Durable question number 1?",
                                              "Durable question number 2?"]
    report = capsys.readouterr().out
    assert "returned 4 items for a 2-question request" in report
    assert "BY RULE" in report


def test_the_dedupe_floor_reads_the_seed_file(tmp_path, capsys):
    """The seed file is this platform's source of truth for the question set,
    and it is readable with no app environment at all - so it is the primary
    floor rather than the database."""
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([
        {"question": " Banked with padding ", "notes": "n"},
        {"question": "Second banked", "notes": "n"},
    ]), encoding="utf-8")

    texts = ah._existing_question_texts(str(seed))
    assert "Banked with padding" in texts, "must match on the STRIPPED text"
    assert "Second banked" in texts


def test_a_missing_seed_file_still_returns_a_usable_floor(tmp_path, capsys):
    """A missing seed must not crash the run - it degrades to whatever the
    database leg provides, and says so."""
    texts = ah._existing_question_texts(str(tmp_path / "does-not-exist.json"))
    assert isinstance(texts, set)
    assert "could not read the seed file" in capsys.readouterr().out


def test_an_author_from_the_judges_family_warns(monkeypatch, capsys):
    """Nothing else in the platform checks the author leg of the independence
    claim. A warning, not a refusal - the script drafts and a human merges."""
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("DEFAULT_MODEL", "qwen3:8b")
    ah._warn_if_same_family("claude-opus-4-1")
    out = capsys.readouterr().out
    assert "same family as the judge" in out

    ah._warn_if_same_family("gemini-3.6-flash")
    assert "[warn]" not in capsys.readouterr().out
