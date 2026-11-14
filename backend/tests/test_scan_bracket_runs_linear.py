"""The markdown rules read a run of brackets, query marks or placeholders in
linear time.

Four exfiltration rules read a markdown image or link: the image whose URL
carries a query, the image and the link whose URL carries a template
placeholder, and any external image. Each read the bracketed text as
[^\\]]* and the URL as [^)]* around its test, and three shapes cost n * n
steps: a run of [ or ![ with no ] after it (every bracket a start, each
reading to the end), and a URL holding a run of ?, & or placeholders with no
) after it (every one of them retried against the rest of the run). With the
opening repeated as well, the cost was n * n * n: an image opening and a ?
repeated to 80,000 characters took 215 s.

What these pin: the shapes that cost n * n cost the scan less than
CEILING_S. CONTROL: a run of ( with no ), an image URL with no ? (neither was
slow before), and what the four rules found, they still find. The rules are
read in one pass now; the old forms, the check that the rules match exactly
what they matched, and the opening repeated with no ) after it (which this
file left open) are in tests/test_scan_markdown_one_pass.py.
"""
import time

import pytest

from app import corpus_scan

N = 100000

# The same ceiling as the whitespace runs' (tests/test_scan_whitespace_runs_linear.py).
# On one workstation, before the change: the fastest shape below 1.2 s, the
# slowest 5.0 s; after it, the slowest 0.08 s (the repeated image opening).
# 0.5 s leaves six times that for a slow shared runner and is still under
# half the fastest case before.
CEILING_S = 0.5


# -- FIX ----------------------------------------------------------------------

def _rep(unit, n=N):
    return unit * (n // len(unit))


SHAPES = {
    "a run of [": _rep("["),
    "a run of ![": _rep("!["),
    "an image URL then a run of ?": "![a](http://x" + _rep("?"),
    "an image URL then a run of &": "![a](http://x" + _rep("&"),
    "an image URL then a run of {{": "![a](http://x" + _rep("{{"),
    "a link URL then a run of {{": "[a](http://x" + _rep("{{"),
    "a link URL then a run of ${": "[a](http://x" + _rep("${"),
    "a link URL then a run of %7b": "[a](http://x" + _rep("%7b", 2 * N),
    # n * n * n before, so kept short: 16,000 and 20,000 characters, where the
    # old forms took about 2 and 4 s.
    "an image opening with a ? repeated": _rep("![a](http://x?", 16000),
    "a link opening with a {{ repeated": _rep("[a](http://{{", 20000),
}


@pytest.mark.parametrize("name", list(SHAPES))
def test_a_bracket_shape_costs_the_scan_linear_time(name):
    text = SHAPES[name]
    t0 = time.perf_counter()
    corpus_scan.scan(text)
    assert time.perf_counter() - t0 < CEILING_S


# -- CONTROL ------------------------------------------------------------------

QUICK_BEFORE = {
    "a run of ( with no )": "![a](http://x" + _rep("("),
    "an image URL with no ?": "![a](http://" + _rep("x"),
    "an image URL with no ?, then )": "![a](http://" + _rep("x") + ")",
}


@pytest.mark.parametrize("name", list(QUICK_BEFORE))
def test_a_shape_that_was_not_slow_is_not_slow(name):
    text = QUICK_BEFORE[name]
    t0 = time.perf_counter()
    corpus_scan.scan(text)
    assert time.perf_counter() - t0 < CEILING_S


FOUND = {
    "an image whose URL carries a query":
        ("![logo](https://a.example.test/p.png?id=7)", ("exfiltration", "high")),
    "an image whose text holds a [":
        ("![a [b c](https://a.example.test/p.png?u=1)", ("exfiltration", "high")),
    "an image after another opening":
        ("![x![logo](https://a.example.test/p?q=1)", ("exfiltration", "high")),
    "an image with a placeholder":
        ("![a](https://a.example.test/{{user}})", ("exfiltration", "high")),
    "a link after another [":
        ("[click [here](https://a.example.test/%7Btoken%7D)", ("exfiltration", "high")),
    "a link with a dollar placeholder":
        ("[go](https://a.example.test/${key})", ("exfiltration", "high")),
    "any external image": ("![logo](https://a.example.test/p.png)", ("exfiltration", "medium")),
}


@pytest.mark.parametrize("name", list(FOUND))
def test_what_the_four_rules_found_they_still_find(name):
    text, finding = FOUND[name]
    assert finding in {(f["type"], f["severity"]) for f in corpus_scan.scan(text)}
