"""The proxy's access lines carry the path alone, never the query string.

The nginx image's own format (`main`) logs $request - the whole request line,
query string included - so whatever a client put in a URL (a question asked
over GET, an OAuth callback's code, a token in a link) was kept in the
container's logs. Since 2026-11-16 frontend/nginx.conf maps the request URI
and the Referer to what comes before the first ?, logs them in a format that
is `main` field for field with only those two swapped, and names that format
in every server block: one set in a server block replaces the image's, where
one set at the http level would add a second line, query string and all. Both
were measured on nginx:1.30-alpine that day, with canary requests against this
file and against the one before it.

The error log takes no format and quotes the whole request line, so a
location that reads files from disk with no try_files fallback - one a client
can ask for a file that is not there - carries log_not_found off (also
2026-11-16): the miss stays out of the error log, and the access line still
records the path and the 404. Each such location is named below; every other
location that reads files is an exact match serving one fixed file, which a
client cannot make miss.

This reads the file as text. It cannot show that nginx behaves as configured -
that was measured in a container, by hand - only that what was measured is
still what ships. The maps' patterns are run here with Python's re on the same
kind of canary: they are plain enough that PCRE and re agree on them.

The config sits outside backend/, so where only backend/ is present the file
is not visible and these skip; CI and a workstation see the whole repo.
"""
from pathlib import Path
import re

import pytest

REPO = Path(__file__).resolve().parents[2]
CONFS = ["frontend/nginx.conf"]

FORMAT = "no_query"
LOG_LINE = "access_log /var/log/nginx/access.log %s" % FORMAT

# The image's own format, as nginx:1.30-alpine declares it in
# /etc/nginx/nginx.conf (read 2026-11-16). Ours is this with two fields cut.
MAIN = ('$remote_addr - $remote_user [$time_local] "$request" '
        '$status $body_bytes_sent "$http_referer" '
        '"$http_user_agent" "$http_x_forwarded_for"')

CANARY = "CANARY207"

# Fields that can hold a query string: the request line, the raw URI, the
# arguments (whole, or one by name) and the raw Referer.
CARRIES_A_QUERY = (r"\$\{?request\b", r"\$\{?request_uri\b", r"\$\{?args\b",
                   r"\$\{?arg_\w", r"\$\{?query_string\b", r"\$\{?is_args\b",
                   r"\$\{?http_referer\b")


def _text(rel):
    conf = REPO / rel
    if not conf.exists():
        pytest.skip(f"{rel} is not visible in this environment")
    return _strip_comments(conf.read_text(encoding="utf-8"))


def _strip_comments(text):
    """Comments out, quoted strings kept whole: a # inside quotes is text."""
    return re.sub(r'("[^"\n]*"|\'[^\'\n]*\')|#[^\n]*',
                  lambda m: m.group(1) or "", text)


def _scan(text):
    """Walk `text` once. Returns its own statements (each as one
    space-joined string, nested blocks skipped) and its top-level blocks as
    (head words, start, end, body). Quoted strings are skipped whole, so a
    brace or a semicolon inside one is text."""
    statements, blocks = [], []
    depth, start, opened, i = 0, 0, 0, 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = text.index(ch, i + 1) + 1
            continue
        if ch == "{":
            if depth == 0:
                head, opened = text[start:i].split(), i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blocks.append((head, start, i + 1, text[opened + 1:i]))
                start = i + 1
        elif ch == ";" and depth == 0:
            if text[start:i].strip():
                statements.append(" ".join(text[start:i].split()))
            start = i + 1
        i += 1
    assert depth == 0, "unbalanced braces"
    return statements, blocks


def _statements(text):
    return _scan(text)[0]


def _servers(text):
    """(start, end, body) of each top-level server block, in file order."""
    return [(a, b, body) for head, a, b, body in _scan(text)[1] if head == ["server"]]


def _words(statement):
    return [w.strip("\"'") for w in re.findall(r'"[^"]*"|\'[^\']*\'|\S+', statement)]


def _label(body):
    """A server block by its name and port, as a failure should name it."""
    own = {s.split()[0]: s.split()[1] for s in _statements(body)
           if s.split()[0] in ("server_name", "listen")}
    return "%s:%s" % (own.get("server_name", ""), own.get("listen", ""))


def _unlogged(text):
    """The server blocks whose own access_log is not exactly the no-query
    line, by label."""
    return [_label(body) for _, _, body in _servers(text)
            if [s for s in _statements(body) if s.split()[0] == "access_log"] != [LOG_LINE]]


def _map(text, source):
    """The one top-level map that reads `source`: (the variable it sets, its
    entries as (key, value))."""
    found = [(head, body) for head, _, _, body in _scan(text)[1]
             if head[:2] == ["map", source]]
    assert len(found) == 1, "%d maps read %s" % (len(found), source)
    head, body = found[0]
    entries = [_words(s) for s in _statements(body)]
    assert all(len(e) == 2 for e in entries), entries
    return head[2], entries


def _apply(entries, value):
    """What nginx's map gives for `value`: an exact key first, then the regex
    keys in order (unanchored, like PCRE's search), else the default."""
    default, exact, regexes = "", {}, []
    for key, out in entries:
        if key == "default":
            default = out
        elif key.startswith("~*"):
            regexes.append((re.compile(key[2:], re.I), out))
        elif key.startswith("~"):
            regexes.append((re.compile(key[1:]), out))
        else:
            exact[key] = out
    if value in exact:
        return exact[value]
    for pattern, out in regexes:
        m = pattern.search(value)
        if m:
            return re.sub(r"\$\{?(\d)\}?", lambda g: m.group(int(g.group(1))) or "", out)
    return default


def _format(text):
    """The no-query log_format's fields, its quoted pieces joined."""
    found = [s for s in _statements(text) if s.split()[:2] == ["log_format", FORMAT]]
    assert len(found) == 1, "%d log_format %s lines" % (len(found), FORMAT)
    pieces = re.findall(r"'([^']*)'|\"([^\"]*)\"", found[0].split(None, 2)[2])
    return " ".join("".join(a or b for a, b in pieces).split())


@pytest.mark.parametrize("rel", CONFS)
def test_every_server_block_logs_in_the_no_query_format(rel):
    """FIX: each server block names the format in its own access_log, once.
    Set there it replaces the image's line, so the block writes one line per
    request and that line has no query string."""
    assert _unlogged(_text(rel)) == []


@pytest.mark.parametrize("rel", CONFS)
def test_nothing_else_in_the_file_logs_another_way(rel):
    """FIX: the file's access_log lines are the server blocks' own and no
    others. One in a location would log that location some other way; one at
    the top of the file would add the image's line back beside ours."""
    text = _text(rel)
    lines = [" ".join(m.split()) for m in re.findall(r"(?<![\w$])(access_log\s[^;]*);", text)]
    assert lines == [LOG_LINE] * len(_servers(text))


@pytest.mark.parametrize("rel", CONFS)
def test_the_format_is_mains_with_the_query_cut(rel):
    """FIX: the format is the image's `main` field for field, the request line
    rebuilt from the method, the mapped path and the protocol, the Referer
    mapped too - so whatever parses these lines still parses them."""
    text = _text(rel)
    path, _ = _map(text, "$request_uri")
    referer, _ = _map(text, "$http_referer")
    want = (MAIN.replace('"$request"', '"$request_method %s $server_protocol"' % path)
                .replace('"$http_referer"', '"%s"' % referer))
    assert _format(text) == " ".join(want.split())


@pytest.mark.parametrize("rel", CONFS)
def test_the_format_names_no_field_that_carries_a_query(rel):
    """FIX: not the request line, the raw URI, the arguments or the raw
    Referer - any of them would put the query string back in the line."""
    fmt = _format(_text(rel))
    assert [p for p in CARRIES_A_QUERY if re.search(p, fmt)] == []


@pytest.mark.parametrize("rel", CONFS)
def test_the_maps_cut_the_uri_and_the_referer_at_the_first_question_mark(rel):
    """FIX: run on canary values, the URI's map gives the path alone and the
    Referer's the URL up to its query; an empty value (no Referer, a request
    line nginx could not parse) logs as "-", the shape `main` gave it."""
    text = _text(rel)
    _, uri = _map(text, "$request_uri")
    _, referer = _map(text, "$http_referer")
    assert _apply(uri, "/api/x?q=" + CANARY) == "/api/x"
    assert _apply(uri, "/?code=" + CANARY) == "/"
    assert _apply(uri, "/assets/app.js") == "/assets/app.js"
    assert _apply(uri, "") == "-"
    assert _apply(referer, "https://example.test/page?r=" + CANARY) == "https://example.test/page"
    assert _apply(referer, "?r=" + CANARY) == "-"
    assert _apply(referer, "") == "-"


@pytest.mark.parametrize("rel", CONFS)
def test_a_server_block_without_its_line_is_caught(rel):
    """FIX: the probe bites on a single block. In a copy with the line taken
    out of any one server block, exactly that block is reported."""
    text = _text(rel)
    for a, b, body in _servers(text):
        cut, n = re.subn(r"(?<![\w$])access_log\s+/var/log/nginx/access\.log\s+%s\s*;" % FORMAT,
                         "", text[a:b], count=1)
        assert n == 1, "%s has no no-query access_log to take out" % _label(body)
        assert _unlogged(text[:a] + cut + text[b:]) == [_label(body)]


@pytest.mark.parametrize("rel", CONFS)
def test_control_the_probe_sees_every_server_block(rel):
    """CONTROL: the probe's server blocks are the file's - as many as the file
    has listen lines, each holding one - so a block the parser skipped could
    not pass the checks above unseen. Passes on the file before 2026-11-16 too."""
    text = _text(rel)
    servers = _servers(text)
    assert servers
    assert len(servers) == len(re.findall(r"(?<![\w$])listen\s", text))
    assert all(sum(s.split()[0] == "listen" for s in _statements(body)) == 1
               for _, _, body in servers)

# Locations that read files from disk with no try_files fallback, where a
# client can name a file that is not there: each carries log_not_found off,
# set per location and never at the server level, where it would also quiet a
# location nobody read for it. Exact-match locations serving one fixed file
# are listed apart: a client cannot make them miss, and if that file is gone
# (a broken build or mount) the error line is the operator's signal.
QUIET_MISSES = {"frontend/nginx.conf": ["/assets/"]}
ONE_FIXED_FILE = {"frontend/nginx.conf": ["= /index.html"]}

# A location with one of these is something other than a file read: a proxy,
# a fixed answer, the shared proxy include, or try_files (which tests for the
# file itself and never logs a miss).
NOT_A_FILE_READ = ("proxy_pass", "return", "include", "try_files")

QUIET = [(rel, loc) for rel in CONFS for loc in QUIET_MISSES.get(rel, [])]


def _locations(text, offset=0):
    """Every location block, nested ones included, in file order, as
    (label, start, end, body) with start and end as offsets into `text`."""
    out = []
    for head, a, b, body in _scan(text)[1]:
        inner = text.index("{", a) + 1
        if head[:1] == ["location"]:
            out.append((" ".join(head[1:]), offset + a, offset + b, body))
        out.extend(_locations(body, offset + inner))
    return out


def _reads_files(body):
    return not any(s.split()[0] in NOT_A_FILE_READ for s in _statements(body))


def _loud_misses(text, rel):
    """The listed locations whose own directives lack log_not_found off."""
    want = QUIET_MISSES.get(rel, [])
    return [label for label, _, _, body in _locations(text)
            if label in want and "log_not_found off" not in _statements(body)]


@pytest.mark.parametrize("rel,loc", QUIET)
def test_a_missing_file_stays_out_of_the_error_log(rel, loc):
    """FIX: the location reads files with no try_files fallback, so a client
    can ask it for one that is not there. log_not_found off keeps that miss -
    and the request line the error log would quote with it, query string and
    all - out of the error log; the access line still has the path and 404."""
    found = [body for label, _, _, body in _locations(_text(rel)) if label == loc]
    assert len(found) == 1, "%d locations %s" % (len(found), loc)
    assert "log_not_found off" in _statements(found[0])


@pytest.mark.parametrize("rel", sorted({rel for rel, _ in QUIET}))
def test_log_not_found_is_set_per_location_only(rel):
    """FIX: the file's log_not_found lines are the listed locations' and no
    others - none at the top of the file or a server block's own level."""
    text = _text(rel)
    assert re.findall(r"(?<![\w$])log_not_found\s+(\w+)\s*;", text) == (
        ["off"] * len(QUIET_MISSES[rel]))
    assert not [s for s in _statements(text) if s.split()[0] == "log_not_found"]
    for _, _, body in _servers(text):
        assert not [s for s in _statements(body) if s.split()[0] == "log_not_found"]


@pytest.mark.parametrize("rel,loc", QUIET)
def test_a_location_without_log_not_found_is_caught(rel, loc):
    """FIX: the probe bites on a single location. In a copy with the line taken
    out of this one, exactly this one is reported."""
    text = _text(rel)
    spans = [(a, b) for label, a, b, _ in _locations(text) if label == loc]
    assert len(spans) == 1, spans
    a, b = spans[0]
    cut, n = re.subn(r"(?<![\w$])log_not_found\s+off\s*;", "", text[a:b], count=1)
    assert n == 1, "%s has no log_not_found off to take out" % loc
    assert _loud_misses(text[:a] + cut + text[b:], rel) == [loc]


@pytest.mark.parametrize("rel", CONFS)
def test_control_every_location_that_reads_files_is_accounted_for(rel):
    """CONTROL: the locations that read files from disk are the listed ones
    and the exact matches serving one fixed file, nothing else - so a new
    static location cannot arrive without someone deciding about it. Adding
    log_not_found changes no location's kind, so this passes both ways."""
    text = _text(rel)
    reads = sorted(label for label, _, _, body in _locations(text) if _reads_files(body))
    assert reads == sorted(QUIET_MISSES.get(rel, []) + ONE_FIXED_FILE.get(rel, []))
    assert all(label.startswith("= ") for label in ONE_FIXED_FILE.get(rel, []))
