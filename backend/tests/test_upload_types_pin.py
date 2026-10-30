"""The KB upload picker's types are the extractor's types (2026-10-29).

The admin upload picker (frontend/src/admin/KBManage.tsx) carried its own
copy of the accepted types twice - the hint under the picker and the file
input's accept list - pinned by nothing, and the hint had already dropped two
types the extractor takes (.yaml, .yml). Both now derive from one constant,
UPLOAD_TYPES, and this pins that constant to text_extract.SUPPORTED_EXTS: a
type added to the extractor and not the picker, or the reverse, fails here.

The component is read as source text. The backend image carries no frontend/,
so there these skip; the CI test job runs them on a full checkout.
"""
import re
from pathlib import Path

import pytest

from app.text_extract import SUPPORTED_EXTS

COMPONENT = (Path(__file__).resolve().parents[2]
             / "frontend" / "src" / "admin" / "KBManage.tsx")

pytestmark = pytest.mark.skipif(not COMPONENT.exists(),
                                reason="frontend/ is not in this tree (the backend image)")

_DECL = re.compile(r"const\s+UPLOAD_TYPES\s*=\s*\[([^\]]*)\]")


def _source() -> str:
    return COMPONENT.read_text(encoding="utf-8")


def test_the_picker_declares_its_types_once_and_they_are_the_extractors():
    found = _DECL.search(_source())
    assert found, ("KBManage.tsx declares no UPLOAD_TYPES constant - the picker's "
                   "types are a hand copy pinned to nothing")
    declared = re.findall(r"""['"]([^'"]+)['"]""", found.group(1))
    assert len(declared) == len(set(declared)), declared
    assert set(declared) == {"." + e for e in SUPPORTED_EXTS}, (
        "the picker's UPLOAD_TYPES and text_extract.SUPPORTED_EXTS differ: "
        f"picker only {sorted(set(declared) - {'.' + e for e in SUPPORTED_EXTS})}, "
        f"extractor only {sorted({'.' + e for e in SUPPORTED_EXTS} - set(declared))}")


def test_the_hint_and_the_accept_list_both_read_the_constant():
    src = _source()
    assert "{UPLOAD_TYPES.join(' ')}" in src, (
        "the hint under the picker is not UPLOAD_TYPES.join(' ') - it can name "
        "fewer types than the picker accepts")
    assert "accept={UPLOAD_TYPES.join(',')}" in src, (
        "the file input's accept is not UPLOAD_TYPES.join(',')")
