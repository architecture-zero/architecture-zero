"""The proxy's request-body limits, pinned as text.

No test read the proxy config, so either directive could be deleted and the
whole suite stay green. This reads the file itself. It cannot show that nginx
behaves as configured - that was measured in a container, by hand, and is in
the record - only that what was measured is still what ships.

The config sits outside backend/, so where only backend/ is present (the
production-image gate stages tests/ and nothing beside it) the file is not
visible and these skip; CI and a workstation see the whole repo.
"""
from pathlib import Path
import re

import pytest

CONF = Path(__file__).resolve().parents[2] / "frontend/nginx.conf"
SITES = {
    "_": [
        "/api/ingest/upload"
    ]
}


def _text():
    if not CONF.exists():
        pytest.skip(f"{CONF.name} is not visible in this environment")
    return re.sub(r"#[^\n]*", "", CONF.read_text(encoding="utf-8"))


def _block_at(text, at):
    """The braces block whose opener starts at `at`, nested blocks included."""
    depth = 0
    for i in range(text.index("{", at), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[at:i + 1]
    raise AssertionError("unclosed block")


def _server(text, site):
    """The server block that names this site AND proxies an API - a config
    may name a site twice (a redirect block, then the real one)."""
    found = [block for block in (_block_at(text, m.start())
                                 for m in re.finditer(r"^server\s*\{", text, flags=re.M))
             if re.search(r"\bserver_name\s+%s\s*;" % re.escape(site), block)
             and "location /api/ {" in block]
    assert len(found) == 1, "%s: %d server blocks" % (site, len(found))
    return found[0]


def _location(server, opener):
    return _block_at(server, server.index(opener))


def _directives(block):
    """The block's OWN directives: what sits in nested blocks is not counted."""
    inner = block[block.index("{") + 1:block.rindex("}")]
    depth, own = 0, []
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0:
            own.append(ch)
    text = re.sub(r"location[^;{}]*$", "", "".join(own), flags=re.M)
    return [" ".join(d.split()) for d in text.split(";") if d.strip()]


@pytest.mark.parametrize("site", sorted(SITES))
def test_the_json_routes_carry_the_same_ceiling_as_the_app(site):
    api = _location(_server(_text(), site), "location /api/ {")
    assert "client_max_body_size 2m" in _directives(api)


@pytest.mark.parametrize("site,door", sorted((s, d) for s in SITES for d in SITES[s]))
def test_a_document_door_is_wider_and_streamed(site, door):
    """64m so a file of MAX_UPLOAD_MB fits with its envelope; streamed so the
    app - which knows whether the caller presented a credential - answers
    before the proxy has taken the body in."""
    server = _server(_text(), site)
    got = _directives(_location(server, "location = %s {" % door))
    assert "client_max_body_size 64m" in got
    assert "proxy_request_buffering off" in got
    # a door that names its own upstream must name its PARENT's: a wrong one
    # sends one site's documents to another site's backend
    mine = [d for d in got if d.startswith("proxy_pass ")]
    parent = [d for d in _directives(_location(server, "location /api/ {"))
              if d.startswith("proxy_pass ")]
    if mine:
        assert mine == parent, (site, door)


def test_no_other_block_sets_a_body_size():
    """The figures above are the whole of it: another would be a door
    somebody widened without saying so."""
    text = _text()
    doors = sum(len(SITES[s]) for s in SITES)
    assert len(re.findall(r"\bclient_max_body_size\b", text)) == len(SITES) + doors
    assert len(re.findall(r"\bproxy_request_buffering\b", text)) == doors
