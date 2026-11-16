"""The PII email pattern costs linear time.

The email pattern began `\\b[A-Za-z0-9._%+\\-]+@`: at every word boundary in a
run of address characters it read to the end of the run looking for the @.
In a run like `a.a.a.a...` every letter starts a word, so a run of n
characters cost n * n steps - 0.74 s at 40,000 characters on one workstation,
about 3 s at 80,000, half an hour for a 2 MB ingest body - inside ONE regex
call, which holds the interpreter lock, so every other request on the
process waited. It runs wherever PII is read: scan_pii and redact_pii on an
ingest or an upload (PII_SCAN_MODE), and the answer filter's count at the end
of every answer (PII_OUTPUT_MODE).

The local part is now at most 64 characters, the limit RFC 5321 sets for an
address, so each start reads at most 64 characters: linear. The ONE
behavior change: a run of more than 64 address characters before an @ is
not an address in full - a run of one word (no boundary inside it) is not
matched at all, and a longer dotted run is matched from its last 64
characters, so its start is not masked. Neither is a deliverable address.

FIX: each shape below with no @ after it costs scan_pii and redact_pii under
CEILING_S, and so does the answer filter's count. (With an @ at the end the
old form was fast too - its first try matched the whole run - so those cases
are guards on the new form, not FIX cases.)
CONTROL: on every text whose address runs fit the limit, the pattern finds
and masks exactly what its old form did (the old form is here).
"""
import random
import re
import time

import pytest

from app import pii

OLD_EMAIL = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
N = 80_000

# Picked from one workstation: the old pattern took about 3 s on these shapes
# at 80,000 characters, the new one about 0.01 s. 0.5 s is far above the
# linear cost (room for a slow shared runner) and far below the old one.
CEILING_S = 0.5

SHAPES = {
    "dot run": "a.",
    "hyphen run": "a-",
    "plus run": "a+",
    "percent run": "a%",
    "dot run, digits": "1.",
    "mixed run": "ab.c-d+e%",
}


def _shape(unit: str, tail: str) -> str:
    body = (unit * (N // len(unit) + 1))[:N - len(tail)]
    return body + tail


@pytest.mark.parametrize("tail", ["", "@example.com"], ids=["no at", "an at at the end"])
@pytest.mark.parametrize("name", sorted(SHAPES))
def test_a_run_of_address_characters_costs_linear_time(name, tail):
    text = _shape(SHAPES[name], tail)
    t0 = time.perf_counter()
    pii.scan_pii(text)
    pii.redact_pii(text)
    assert time.perf_counter() - t0 < CEILING_S


def test_the_answer_filter_count_costs_linear_time():
    # The filter's flush reads the whole answer through scan_pii.
    f = pii.OutputFilter("warn")
    t0 = time.perf_counter()
    f.apply(_shape("a.", ""))
    assert time.perf_counter() - t0 < CEILING_S


_LOCAL = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-"
_WORDS = ["write", "to", "me", "at", "or", "reach", "the", "team", "via", "(", ")",
          ",", ";", ":", "<", ">", "\"", "'", "mailto:", "-", ".", "\n", "  "]


def _address(rng: random.Random) -> str:
    local = "".join(rng.choice(_LOCAL) for _ in range(rng.randint(1, 64)))
    labels = [
        "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789-") for _ in range(rng.randint(1, 12)))
        for _ in range(rng.randint(1, 3))
    ]
    tld = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(1, 6)))
    return local + "@" + ".".join(labels) + "." + tld


def test_realistic_text_matches_and_masks_exactly_what_the_old_form_did():
    rng = random.Random(20261115)
    for _ in range(4000):
        parts = []
        for _ in range(rng.randint(1, 12)):
            parts.append(_address(rng) if rng.random() < 0.4 else rng.choice(_WORDS))
        # Joined by a space: with nothing between them one address's domain
        # runs into the next one's local part, a run past 64 - the one change,
        # pinned by its own test below, not a realistic text.
        text = " ".join(parts)
        assert pii._PATTERNS["email"].findall(text) == OLD_EMAIL.findall(text), text
        assert pii._PATTERNS["email"].sub("X", text) == OLD_EMAIL.sub("X", text), text


def test_short_random_strings_match_exactly_what_the_old_form_did():
    # Under 66 characters no address run can pass the 64 limit, so the two
    # forms must agree on every string - the dense alphabet makes them try.
    rng = random.Random(7)
    alphabet = "a.@-x _+%1b\nc"
    for _ in range(30000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        assert pii._PATTERNS["email"].findall(text) == OLD_EMAIL.findall(text), repr(text)
        assert pii._PATTERNS["email"].sub("X", text) == OLD_EMAIL.sub("X", text), repr(text)


def test_the_one_change_a_local_part_over_64_characters():
    one_word = "x" + "a" * 70 + "@example.com"
    assert OLD_EMAIL.findall(one_word)                      # the old form took it
    assert not pii._PATTERNS["email"].findall(one_word)     # not an address now
    at_limit = "a" * 64 + "@example.com"
    assert pii._PATTERNS["email"].findall(at_limit) == [at_limit]
