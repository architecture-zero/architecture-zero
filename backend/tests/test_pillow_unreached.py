"""Pillow is never handed bytes by this app (2026-10-21), and since 2026-11-10 the
dependency scan accepts no Pillow advisory.

The acceptance this file was written for is CLOSED. pip-audit flagged Pillow
12.3.0 - 28 advisories on OSV on 2026-10-21, 39 the next day, 41 by 2026-10-23:
TIFF, GIF, EPS, PDF, BMP, PNG and FlashPix decoders reading past their buffers,
returning uninitialized memory, recursing without bound, slipping the
decompression-bomb limit; a double free in a transform. Every one is fixed in
Pillow 13.0.0, which could not be installed: fastembed (0.8.0, and 0.9.0 the
same) required pillow<13, and pip refuses the pair. The scan workflow's
ignore-vulns block carried the ids on one condition, pinned here: no module of
this app imports Pillow, and none loads fastembed's image-embedding models, so
no upload, connector or crawl reaches a Pillow decoder.

The acceptance named its own exit - the day fastembed lets Pillow 13 in - and
it came: fastembed 0.9.1 (2026-10-24) requires pillow<14. Closed 2026-11-10:
requirements.txt pins fastembed 0.9.1, Pillow resolves to 13.0.0, and the 41
ids left the ignore-vulns block, so a Pillow advisory fails the scan again.

What this file pins now:
- the original condition, unchanged. It is the ground a future Pillow advisory
  would be triaged on. Pillow stays in the image for two parents: fastembed
  imports it, and qrcode[pil] draws the MFA setup QR code with it - an encode of
  this app's own string, never a decode of someone else's bytes;
- the fastembed pin does not drop below 0.9.1, the first release that lets the
  fixed Pillow in;
- none of the 41 closed advisories returns to an ignore list, under its CVE,
  GHSA or PYSEC id. With 13.0.0 installed such a line hides nothing today, and
  would hide a rollback to 12.3.0 tomorrow.
"""
import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = BACKEND.parent / ".github" / "workflows"
PILLOW = re.compile(r"^\s*(from\s+PIL\b|import\s+PIL\b)", re.M)
IMAGE_MODELS = re.compile(r"fastembed\.image\b|\bImageEmbedding\b|LateInteractionMultimodalEmbedding")
FASTEMBED_FLOOR = (0, 9, 1)

# The 41 advisories the acceptance covered when it closed - OSV's whole set for
# Pillow 12.3.0, every one fixed in 13.0.0. One advisory per line, under its CVE,
# GHSA and PYSEC ids: the scanner's ignore list matches an id OR an alias, so a
# line under any of the three forms would quietly bring the acceptance back.
CLOSED_ADVISORIES = """
    CVE-2026-112390 GHSA-22hg-r758-rrw9
    CVE-2026-112391 GHSA-254f-3936-288h
    CVE-2026-112392 GHSA-27h8-gf7j-v5hc PYSEC-2026-4326
    CVE-2026-112393 GHSA-332p-7hrc-92wf
    CVE-2026-112394 GHSA-4558-3h8r-v8xp PYSEC-2026-4327
    CVE-2026-112395 GHSA-462x-pgg6-55fp
    CVE-2026-112396 GHSA-4rr5-89j8-mwwm PYSEC-2026-4328
    CVE-2026-112397 GHSA-58r4-6rv3-gfh8 PYSEC-2026-4329
    CVE-2026-112398 GHSA-5x8w-8jj5-q52h PYSEC-2026-4330
    CVE-2026-112399 GHSA-5xm2-6676-qcp9
    CVE-2026-112400 GHSA-65qm-jvmh-6pqg
    CVE-2026-112401 GHSA-74r2-7ff9-mjj2
    CVE-2026-112402 GHSA-7mwm-ghpf-9vrm PYSEC-2026-4331
    CVE-2026-112403 GHSA-8g5g-8vmr-9782 PYSEC-2026-4332
    CVE-2026-112404 GHSA-c6hw-5grf-ff96
    CVE-2026-112405 GHSA-cp9v-662q-vvx9 PYSEC-2026-4333
    CVE-2026-112406 GHSA-f2g6-542v-9j58
    CVE-2026-112891 GHSA-fc9g-5qg7-mf45
    CVE-2026-112892 GHSA-gg2r-wvfm-q9rx
    CVE-2026-112893 GHSA-j4m9-6w36-pw47
    CVE-2026-112894 GHSA-jg86-v3jv-hrpm
    CVE-2026-112895 GHSA-p4wg-p73j-c528
    CVE-2026-112896 GHSA-pchr-8j38-3x4p PYSEC-2026-4334
    CVE-2026-112897 GHSA-q45j-58j2-45rg
    CVE-2026-112898 GHSA-rfgq-q3w2-pmv2
    CVE-2026-112899 GHSA-vx6w-vv9f-6gvg
    CVE-2026-112900 GHSA-w889-685w-c8fh
    CVE-2026-112901 GHSA-xgh8-jp6q-p5x8 PYSEC-2026-4335
    CVE-2026-112902 GHSA-xvh5-g6gv-559v
    CVE-2026-114501 GHSA-654x-cwwv-5j9c PYSEC-2026-4336
    CVE-2026-96636 GHSA-p8hx-v2p6-rp84 PYSEC-2026-4337
    CVE-2026-96637 GHSA-g842-9rc4-69pw PYSEC-2026-4338
    CVE-2026-96638 GHSA-24rc-8hj2-v98c
    CVE-2026-96639 GHSA-v9pv-5gc8-w2m6 PYSEC-2026-4339
    CVE-2026-96640 GHSA-mrfp-p49p-3hf2
    CVE-2026-96641 GHSA-4rm8-3hmm-39fg PYSEC-2026-4340
    CVE-2026-96642 GHSA-3j72-p6qg-6xp7 PYSEC-2026-4341
    CVE-2026-96643 GHSA-g4qv-2jxx-769h PYSEC-2026-4342
    CVE-2026-96644 GHSA-9v9v-9qvf-h2hj PYSEC-2026-4343
    CVE-2026-96778 GHSA-g7cr-3p24-7wf7
    GHSA-78g4-xqwv-q3g8
"""
CLOSED_IDS = frozenset(form.upper() for form in CLOSED_ADVISORIES.split())


def _sources():
    for folder in ("app", "scripts"):
        root = BACKEND / folder
        if root.is_dir():
            for path in sorted(root.rglob("*.py")):
                yield path.relative_to(BACKEND).as_posix(), path.read_text(encoding="utf-8")


def _ignored_ids(workflow_text):
    """Every advisory id a workflow tells pip-audit to ignore: the entries of each
    `ignore-vulns: |` block, and any --ignore-vuln flag on a command line."""
    ids = []
    lines = workflow_text.splitlines()
    for at, line in enumerate(lines):
        key = re.match(r"^(\s*)ignore-vulns:\s*\|", line)
        if key is None:
            continue
        for entry in lines[at + 1:]:
            # The block ends at the first line indented no deeper than its key.
            if entry.strip() and len(entry) - len(entry.lstrip()) <= len(key.group(1)):
                break
            ids.extend(entry.split())
    ids.extend(re.findall(r"--ignore-vuln[=\s]+([\w-]+)", workflow_text))
    return ids


def test_no_app_module_imports_pillow():
    offenders = [name for name, src in _sources() if PILLOW.search(src)]
    assert offenders == [], f"Pillow reached from app code - re-triage the acceptance: {offenders}"


def test_no_app_module_loads_an_image_embedding_model():
    offenders = [name for name, src in _sources() if IMAGE_MODELS.search(src)]
    assert offenders == [], f"an image-embedding model hands Pillow bytes - re-triage: {offenders}"


def test_the_fastembed_pin_lets_the_fixed_pillow_in():
    requirements = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
    pins = re.findall(r"^fastembed==(\d+(?:\.\d+)*)\s*$", requirements, re.M)
    assert len(pins) == 1, f"expected one exact fastembed pin in requirements.txt, found {pins}"
    assert tuple(int(part) for part in pins[0].split(".")) >= FASTEMBED_FLOOR, (
        f"fastembed {pins[0]} requires pillow<13 - that reinstalls Pillow 12.3.0 and the 41 "
        "advisories closed 2026-11-10. Keep the pin at 0.9.1 or newer.")


def test_no_scan_ignores_a_closed_pillow_advisory():
    workflows = [(path.name, path.read_text(encoding="utf-8"))
                 for path in sorted(WORKFLOWS.glob("*.y*ml"))]
    assert any("pip-audit" in text for _, text in workflows), (
        f"no workflow with a pip-audit step under {WORKFLOWS} - this guard read nothing")
    offenders = [f"{name}: {found}" for name, text in workflows
                 for found in _ignored_ids(text) if found.upper() in CLOSED_IDS]
    assert offenders == [], (
        "an ignore line for a Pillow advisory that 13.0.0 fixes is back - the acceptance "
        f"closed 2026-11-10; remove the line, or re-triage with the reason written down: {offenders}")


def test_the_ignore_list_reader_sees_every_id_form():
    """Control for the test above, which passes on a list it cannot read: a block
    entry under each id form, a blank line inside the block, the block's end, and a
    command-line flag."""
    sample = (
        "      - uses: pypa/gh-action-pip-audit@v1\n"
        "        with:\n"
        "          inputs: backend/requirements.txt\n"
        "          ignore-vulns: |\n"
        "            CVE-2026-45830\n"
        "            ghsa-22hg-r758-rrw9\n"
        "\n"
        "            PYSEC-2026-4326\n"
        "  bandit:\n"
        "    steps:\n"
        "      - run: pip-audit --ignore-vuln CVE-2026-114501 -r requirements.txt\n"
    )
    ids = _ignored_ids(sample)
    assert ids == ["CVE-2026-45830", "ghsa-22hg-r758-rrw9", "PYSEC-2026-4326", "CVE-2026-114501"]
    assert [found for found in ids if found.upper() in CLOSED_IDS] == ids[1:]
    assert len(CLOSED_IDS) == 99, "41 advisories under 99 ids - the table above was edited"
