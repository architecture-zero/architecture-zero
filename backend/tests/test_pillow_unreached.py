"""Pillow is never handed bytes by this app (2026-10-21).

pip-audit flags Pillow 12.3.0 (OSV lists 28 advisories for it as of 2026-10-21 -
TIFF, GIF, EPS and PDF decoders reading past their buffers, returning
uninitialized memory, recursing without bound, a double free in a transform; the
morning's runs showed eight, the evening's sixteen more), all fixed in Pillow
13.0.0 - which cannot be installed: fastembed (0.8.0 pinned here; 0.9.0 the same)
requires pillow<13, and pip refuses the pair. The acceptance in the scan
workflow's ignore-vulns block rests on one condition: no module of this app
imports Pillow, and none loads fastembed's image-embedding models (the only
code in the image that hands Pillow bytes), so no upload, connector or crawl
reaches a Pillow decoder. This test fails the build the day an app module does
either; re-triage then, or when fastembed lets Pillow 13 in (its PR #830) -
bump fastembed and drop the ids.
"""
import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PILLOW = re.compile(r"^\s*(from\s+PIL\b|import\s+PIL\b)", re.M)
IMAGE_MODELS = re.compile(r"fastembed\.image\b|\bImageEmbedding\b|LateInteractionMultimodalEmbedding")


def _sources():
    for folder in ("app", "scripts"):
        root = BACKEND / folder
        if root.is_dir():
            for path in sorted(root.rglob("*.py")):
                yield path.relative_to(BACKEND).as_posix(), path.read_text(encoding="utf-8")


def test_no_app_module_imports_pillow():
    offenders = [name for name, src in _sources() if PILLOW.search(src)]
    assert offenders == [], f"Pillow reached from app code - re-triage the acceptance: {offenders}"


def test_no_app_module_loads_an_image_embedding_model():
    offenders = [name for name, src in _sources() if IMAGE_MODELS.search(src)]
    assert offenders == [], f"an image-embedding model hands Pillow bytes - re-triage: {offenders}"
