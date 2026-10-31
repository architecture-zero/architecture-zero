"""A deployment's own rule for watching Ollama, asked every pass (2026-10-31).

The timer decided whether to watch Ollama from two things only: a reader
was passed, and Ollama is on in Settings. A deployment that watches Ollama
only under a rule of its own - say, only while the saved URL is a server it
runs itself - had no way to say so, short of its own copy of this module.
So run_self_check and self_check_loop take an optional `ollama_watched`
callable, and ONE public decision, watching_ollama, asks all three every
pass. The loop reads it at boot off the event loop (the rule may read the
database) and hands the callable to every pass, so a URL saved after boot
holds from the next pass.

FIX: with a rule that says no, the pass does not probe and the health word
reads skipped; with one that says yes, it probes; the rule is asked every
pass, at boot off the event loop, and passed to every pass. CONTROL: with no
rule, the decision is exactly the reader and the toggle, as before.

Surface-neutral on purpose: the toggle is stood in for, never read, so the
file is the same wherever self_check.py is.
"""
import asyncio
import threading

import pytest

from app import self_check


class _Answer:
    status_code = 200

    def json(self):
        return {"models": []}


def _up(path, timeout):
    return _Answer()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """The timer's state goes back after each test; no alert leaves."""
    for name in ("_rag_wired", "_ollama_wired", "_redis_wired", "_started_at",
                 "_rag_last", "_ollama_last", "_redis_last", "_last_failing",
                 "_last_logged"):
        monkeypatch.setattr(self_check, name, getattr(self_check, name))
    monkeypatch.setattr(self_check, "fire_alert", lambda key, title, body: None)
    monkeypatch.setattr(self_check, "redis_enabled", lambda: False)


@pytest.fixture
def toggle(monkeypatch):
    """Stand in for the Settings toggle, whatever reads it on this surface."""
    state = {"on": True}
    monkeypatch.setattr(self_check, "ollama_enabled", lambda: state["on"])
    return state


# -- The decision ----------------------------------------------------------------

@pytest.mark.parametrize("reader, on", [(None, True), (_up, False), (_up, True)])
def test_with_no_rule_the_decision_is_the_reader_and_the_toggle(toggle, reader, on):
    """CONTROL: a deployment that passes no rule watches exactly as before."""
    toggle["on"] = on
    assert self_check.watching_ollama(reader) is (reader is not None and on)
    assert self_check.watching_ollama(reader, None) is (reader is not None and on)


@pytest.mark.parametrize("rule", [True, False])
def test_the_rule_decides_when_the_reader_and_the_toggle_allow(toggle, rule):
    assert self_check.watching_ollama(_up, lambda: rule) is rule


def test_the_rule_cannot_turn_on_what_the_toggle_turned_off(toggle):
    toggle["on"] = False
    asked = []
    assert self_check.watching_ollama(_up, lambda: asked.append(1) or True) is False
    assert self_check.watching_ollama(None, lambda: asked.append(1) or True) is False


# -- One pass -------------------------------------------------------------------

def test_a_pass_whose_rule_says_no_does_not_probe(toggle, tmp_path):
    out = self_check.run_self_check(str(tmp_path), _up, ollama_watched=lambda: False)
    assert "ollama" not in out


def test_a_pass_whose_rule_says_yes_probes(toggle, tmp_path):
    out = self_check.run_self_check(str(tmp_path), _up, ollama_watched=lambda: True)
    assert out["ollama"]["ok"] is True


def test_a_pass_with_no_rule_probes_as_before(toggle, tmp_path):
    out = self_check.run_self_check(str(tmp_path), _up)
    assert out["ollama"]["ok"] is True


def test_the_rule_is_asked_every_pass(toggle, tmp_path):
    """A rule whose answer changes between passes - a URL saved meanwhile -
    is followed from the next pass, never the boot's answer for good."""
    answers = iter([True, False, True])
    seen = [("ollama" in self_check.run_self_check(
        str(tmp_path), _up, ollama_watched=lambda: next(answers))) for _ in range(3)]
    assert seen == [True, False, True]


# -- The timer ------------------------------------------------------------------

def test_the_boot_reads_the_rule_off_the_event_loop(toggle, monkeypatch):
    monkeypatch.setattr(self_check, "SELF_CHECK_INTERVAL_SECONDS", 0)
    threads = []

    def rule():
        threads.append(threading.current_thread() is threading.main_thread())
        return False

    asyncio.run(self_check.self_check_loop(".", _up, None, ollama_watched=rule))
    assert threads == [False]                  # asked once, in a worker thread
    assert self_check._ollama_wired is False
    assert self_check.ollama_readiness() == "skipped"

    asyncio.run(self_check.self_check_loop(".", _up, None, ollama_watched=lambda: True))
    assert self_check._ollama_wired is True


def test_the_loop_hands_the_rule_to_every_pass(toggle, monkeypatch):
    def rule():
        return True

    runs = []
    monkeypatch.setattr(self_check, "SELF_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(self_check, "run_self_check", lambda *a: runs.append(a) or {})

    async def go():
        task = asyncio.create_task(
            self_check.self_check_loop(".", _up, None, ollama_watched=rule))
        for _ in range(1000):
            if len(runs) >= 2:
                break
            await asyncio.sleep(0.005)
        task.cancel()

    asyncio.run(go())
    assert len(runs) >= 2 and all(a[5] is rule for a in runs)


def test_a_pass_the_rule_turned_off_reads_skipped(toggle, monkeypatch):
    """The health routes' word follows what the pass decided: the boot and the
    first pass are told yes, the second no."""
    answers = iter([True, True, False])
    seen = []
    real_pass = self_check.run_self_check

    def _pass(*a):
        out = real_pass(*a)
        seen.append("ollama" in out)
        return out

    monkeypatch.setattr(self_check, "SELF_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(self_check, "run_self_check", _pass)
    monkeypatch.setattr(self_check, "probe_disk", lambda d: {"ok": True})

    async def go():
        task = asyncio.create_task(self_check.self_check_loop(
            ".", _up, None, ollama_watched=lambda: next(answers, False)))
        for _ in range(1000):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.002)
        task.cancel()

    asyncio.run(go())
    assert seen[:2] == [True, False], seen
    assert self_check.ollama_readiness() == "skipped"
