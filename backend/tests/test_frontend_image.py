"""The frontend's runtime image is a supported nginx, pinned by its stable branch.

The frontend's last build stage serves the static bundle with nginx. It was
FROM nginx:alpine, a floating tag: a build uses whatever image of that name
the build host already holds, and a host that never pulls keeps building on
the image it pulled first. nginx numbers its branches by parity: an even minor
is a stable branch, patched until the next stable is cut; an odd minor is the
mainline, which ends the day it becomes that next stable.

FIX: the runtime stage is a stable branch (an even minor) no older than 1.30,
an alpine build, named by its branch - so a rebuild takes patch releases and
never crosses to a branch nobody read the changes for. A floor, not a watch:
a stable branch ends when the next one is cut (about once a year).
CONTROL: the build stage's image is as it was, and the runtime stage still
copies the built bundle and the frontend's own nginx.conf.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILES = [p for p in (REPO / "frontend" / "Dockerfile", REPO / "mobile" / "Dockerfile.web")
               if p.exists()]

pytestmark = pytest.mark.skipif(not DOCKERFILES,
                                reason="the repo's frontend Dockerfile is not beside this suite")

# The stable branch current when this was written. A floor, not a ceiling.
OLDEST_ACCEPTED = (1, 30)


def _lines() -> list[str]:
    assert len(DOCKERFILES) == 1, DOCKERFILES
    return DOCKERFILES[0].read_text(encoding="utf-8").splitlines()


def _nginx_branch() -> tuple[int, int]:
    froms = [line.split()[1] for line in _lines() if re.match(r"\s*FROM\s+nginx", line)]
    assert len(froms) == 1, froms
    named = re.fullmatch(r"nginx:(\d+)\.(\d+)-alpine", froms[0])
    assert named, f"the runtime stage is pinned by branch as nginx:<major>.<minor>-alpine, not {froms[0]!r}"
    return int(named.group(1)), int(named.group(2))


def test_the_runtime_stage_is_a_stable_nginx_branch():
    major, minor = _nginx_branch()
    assert minor % 2 == 0, (
        f"nginx {major}.{minor} is a mainline branch (odd minor): it ends the day "
        "the next stable is cut")


def test_the_runtime_stage_is_no_older_than_the_branch_this_was_read_against():
    assert _nginx_branch() >= OLDEST_ACCEPTED


def test_control_the_build_stage_and_the_copies_are_as_they_were():
    lines = [line.strip() for line in _lines()]
    assert any(re.fullmatch(r"FROM node:24-alpine AS \w+", line) for line in lines)
    assert any(line.startswith("COPY --from=") and line.endswith("/usr/share/nginx/html") for line in lines)
    assert "COPY nginx.conf /etc/nginx/conf.d/default.conf" in lines
