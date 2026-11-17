"""list_scanned_images.py, and the weekly image scan it feeds (2026-11-17).

The repository scan reads files and lockfiles. Nothing read the container
images the stack runs: the ones a compose file pulls and the base image every
Dockerfile builds FROM. .github/workflows/image-scan.yml scans them, one
matrix job per image, and the list is read from the files by the script.
These pin what the list holds, what it leaves out and why, and the wiring of
the workflow that consumes it.

FIX (each fails where the script or the workflow is missing):
  - a compose service's image: is listed; a commented-out service is not
  - every Dockerfile's FROM is listed: nested, suffixed and prefixed file
    names, a continued line, a --platform flag stepped over
  - the same image spelled twice is listed once, an untagged one as :latest
  - an image named through a variable, or a name that is not an image
    reference, stops the listing (exit 2) instead of being dropped from it
  - an empty tree is exit 2, never an empty list
  - --json prints the list as one line of JSON
  - the workflow: weekly and on demand only; contents: read and nothing
    more, no token left by a checkout; a matrix over the listing with
    fail-fast off; the repository scan's trivy pin, version and runner in
    image mode with the same flags; no expression pasted into a shell
CONTROL (one per skip rule; each fixture holds the thing the rule leaves out
beside a real image, so with the rule removed the wrong name is listed and
the test fails - each was mutation-checked that way when it was written):
  - a stage an earlier FROM named (FROM deps AS builder), any case
  - scratch
  - an image a compose service builds itself, and a service that runs it
  - a Dockerfile under node_modules
  - a Dockerfile inside a nested checkout (a git worktree)
GUARD:
  - this repository's own files list clean: at least one image, each tagged
"""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "backend" / "scripts" / "list_scanned_images.py"
WORKFLOW = REPO / ".github" / "workflows" / "image-scan.yml"
TRIVY_ACTION = "aquasecurity/trivy-action@"


@pytest.fixture(scope="module")
def lsi():
    spec = importlib.util.spec_from_file_location("list_scanned_images", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _listed(lsi, root):
    return lsi.list_images(str(root))


# --- what the list holds --------------------------------------------------


def test_a_compose_service_image_is_listed_and_a_commented_one_is_not(lsi, tmp_path):
    """FIX: the image a compose service pulls is listed; a service commented
    out is no service (a line-matching reader would list it)."""
    _write(tmp_path, "docker-compose.yml",
           "services:\n"
           "  db:\n"
           "    image: postgres:16-alpine\n"
           "  # cache:\n"
           "  #   image: memcached:1.6-alpine\n")
    assert _listed(lsi, tmp_path) == ["postgres:16-alpine"]


def test_every_dockerfile_from_is_listed(lsi, tmp_path):
    """FIX: every Dockerfile in the tree, whatever its spelling, every FROM in
    it; a continued line is joined and a --platform flag is not an image."""
    _write(tmp_path, "api/Dockerfile", "FROM python:3.11-slim\nRUN true\n")
    _write(tmp_path, "web/Dockerfile.web",
           "# FROM commented:1\nFROM --platform=linux/amd64 node:24-alpine\n")
    _write(tmp_path, "tools/worker.Dockerfile", "FROM \\\n  golang:1.23\n")
    assert _listed(lsi, tmp_path) == ["golang:1.23", "node:24-alpine", "python:3.11-slim"]


def test_the_same_image_spelled_twice_is_listed_once(lsi, tmp_path):
    """FIX: Docker Hub's implied registry and library/ path, and an implied
    :latest, are one image however they are written - and in any file."""
    _write(tmp_path, "docker-compose.yml",
           "services:\n"
           "  edge:\n"
           "    image: nginx:1.30-alpine\n"
           "  cache:\n"
           "    image: docker.io/library/redis\n")
    _write(tmp_path, "docker-compose.extra.yml",
           "services:\n"
           "  cache-two:\n"
           "    image: redis:latest\n")
    _write(tmp_path, "web/Dockerfile", "FROM docker.io/library/nginx:1.30-alpine\n")
    assert _listed(lsi, tmp_path) == ["nginx:1.30-alpine", "redis:latest"]
    # Another registry's library/ path is its own name, and a port is not a tag.
    assert lsi.normalise("ghcr.io/library/tool:1") == "ghcr.io/library/tool:1"
    assert lsi.normalise("localhost:5000/app") == "localhost:5000/app:latest"
    assert lsi.normalise("alpine@sha256:" + "0" * 64) == "alpine@sha256:" + "0" * 64


@pytest.mark.parametrize("rel, text, says", [
    ("Dockerfile", "FROM python:${PY_VERSION}-slim\n", "through a variable"),
    ("docker-compose.yml", "services:\n  db:\n    image: mysql:${MYSQL_TAG:-8.0}\n",
     "through a variable"),
    ("docker-compose.yml", "services:\n  db:\n    image: \"mysql:8.0; echo x\"\n",
     "not an image reference"),
    ("Dockerfile", "FROM\n", "FROM with no image"),
], ids=["arg-in-from", "variable-in-compose", "not-a-reference", "from-without-image"])
def test_a_name_that_cannot_be_listed_truthfully_stops_the_listing(lsi, tmp_path, capsys, rel, text, says):
    """FIX: the listing stops with exit 2, prints nothing to stdout and says
    why on stderr, rather than print every image but the one it could not
    read."""
    _write(tmp_path, "base/Dockerfile", "FROM alpine:3.20\n")   # a real image beside it
    _write(tmp_path, rel, text)
    with pytest.raises(lsi.ListingError):
        _listed(lsi, tmp_path)
    assert lsi.main(["--root", str(tmp_path), "--json"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("list_scanned_images: ") and says in out.err


def test_an_empty_tree_is_exit_2_not_an_empty_list(lsi, tmp_path, capsys):
    """FIX: no image at all is a broken reading; the matrix never gets [] ."""
    _write(tmp_path, "README.md", "nothing to build here\n")
    _write(tmp_path, "docker-compose.yml", "volumes:\n  data:\n")
    assert lsi.main(["--root", str(tmp_path), "--json"]) == 2
    assert capsys.readouterr().out == ""


def test_json_prints_the_list_on_one_line(lsi, tmp_path, capsys):
    """FIX: --json is one line, because the workflow writes it to
    $GITHUB_OUTPUT as name=value; without it, one image per line."""
    _write(tmp_path, "Dockerfile", "FROM python:3.11-slim\nFROM nginx:1.30-alpine\n")
    assert lsi.main(["--root", str(tmp_path), "--json"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out) == ["nginx:1.30-alpine", "python:3.11-slim"]
    assert lsi.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines() == ["nginx:1.30-alpine", "python:3.11-slim"]


# --- what it leaves out, one rule each -------------------------------------


def test_control_a_named_stage_is_not_an_image(lsi, tmp_path):
    """CONTROL: the stage rule. Removed, deps:latest and builder:latest are
    listed - names no registry holds."""
    _write(tmp_path, "Dockerfile",
           "FROM node:24-alpine AS deps\n"
           "FROM deps AS Builder\n"
           "FROM builder\n"
           "FROM nginx:1.30-alpine\n")
    assert _listed(lsi, tmp_path) == ["nginx:1.30-alpine", "node:24-alpine"]


def test_control_scratch_is_not_an_image(lsi, tmp_path):
    """CONTROL: the scratch rule. Removed, scratch:latest is listed."""
    _write(tmp_path, "Dockerfile", "FROM golang:1.23 AS build\nFROM scratch\n")
    assert _listed(lsi, tmp_path) == ["golang:1.23"]


def test_control_an_image_the_repo_builds_is_not_pulled(lsi, tmp_path):
    """CONTROL: the build rule. A service with build: names the tag it
    produces; neither it nor a service running that tag (in this file or
    another) pulls anything. Removed, example-backend:latest is listed."""
    _write(tmp_path, "docker-compose.yml",
           "services:\n"
           "  backend:\n"
           "    build: ./backend\n"
           "    image: example-backend:latest\n"
           "  worker:\n"
           "    image: example-backend\n"
           "  db:\n"
           "    image: postgres:16-alpine\n")
    _write(tmp_path, "docker-compose.peers.yml",
           "services:\n"
           "  peer:\n"
           "    image: example-backend:latest\n")
    assert _listed(lsi, tmp_path) == ["postgres:16-alpine"]


def test_control_node_modules_is_not_walked(lsi, tmp_path):
    """CONTROL: a package's own Dockerfile is not this stack's image.
    Removed, ubuntu:18.04 is listed."""
    _write(tmp_path, "Dockerfile", "FROM python:3.11-slim\n")
    _write(tmp_path, "frontend/node_modules/some-package/Dockerfile", "FROM ubuntu:18.04\n")
    assert _listed(lsi, tmp_path) == ["python:3.11-slim"]


def test_control_a_nested_checkout_is_not_walked(lsi, tmp_path):
    """CONTROL: a git worktree inside the tree (a directory holding a .git
    file) is a second copy of the files, maybe an older one. Removed, its
    python:3.10-slim is listed."""
    _write(tmp_path, "Dockerfile", "FROM python:3.11-slim\n")
    _write(tmp_path, ".worktrees/feature/.git", "gitdir: /elsewhere/.git/worktrees/feature\n")
    _write(tmp_path, ".worktrees/feature/Dockerfile", "FROM python:3.10-slim\n")
    assert _listed(lsi, tmp_path) == ["python:3.11-slim"]


# --- this repository --------------------------------------------------------


def test_guard_this_repository_lists_clean(lsi):
    """GUARD: what the weekly run's first job will print, read here first: no
    error, at least one image, every one tagged or pinned by digest."""
    images = _listed(lsi, REPO)
    assert images
    for img in images:
        assert "@" in img or ":" in img.rsplit("/", 1)[-1], img


# --- the workflow -----------------------------------------------------------


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _trivy_steps(job):
    return [s for s in job.get("steps") or [] if str(s.get("uses", "")).startswith(TRIVY_ACTION)]


def test_the_workflow_runs_weekly_and_on_demand_only():
    """FIX: never on push or pull request - an image's findings move with the
    vulnerability database, not with this tree, and minutes cost money."""
    wf = _workflow()
    on = wf.get("on", wf.get(True))   # YAML 1.1 reads a bare `on` key as true
    assert set(on) == {"schedule", "workflow_dispatch"}
    (entry,) = on["schedule"]
    minute, _hour, day_of_month, month, day_of_week = entry["cron"].split()
    assert minute.isdigit() and minute not in ("0", "00")   # off the top of the hour
    assert day_of_month == "*" and month == "*" and day_of_week.isdigit()


def test_the_workflow_reads_nothing_but_contents():
    """FIX: contents: read for the whole file, no job asks for more, and no
    checkout leaves the token in .git/config for the steps after it."""
    wf = _workflow()
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        assert "permissions" not in job, name
        checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert checkouts, name
        for step in checkouts:
            assert (step.get("with") or {}).get("persist-credentials") is False, name


def test_the_scan_is_a_matrix_over_the_listing():
    """FIX: the first job prints the list, the scan runs once per image, and
    one image's finding does not cancel the others."""
    jobs = _workflow()["jobs"]
    listing, scan = jobs["list"], jobs["scan"]
    runs = "\n".join(s.get("run", "") for s in listing["steps"])
    assert "python backend/scripts/list_scanned_images.py --json" in runs
    assert listing["outputs"]["images"] == "${{ steps.list.outputs.images }}"
    assert scan["needs"] == "list"
    assert scan["strategy"]["fail-fast"] is False
    assert scan["strategy"]["matrix"]["image"] == "${{ fromJSON(needs.list.outputs.images) }}"


def test_the_scan_uses_the_repository_scans_trivy_pin_in_image_mode():
    """FIX: the same action pin, scanner version and runner as the repository
    scan in this repository's other workflows, so a bump to one is a visible
    mismatch here; image mode, HIGH and CRITICAL, fixed findings only, a
    finding fails the job. The checkout comes first, for .trivyignore."""
    theirs = []
    for path in sorted(WORKFLOW.parent.glob("*.y*ml")):
        if path.name == WORKFLOW.name:
            continue
        for job in (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).values():
            theirs.extend((job, step) for step in _trivy_steps(job))
    assert theirs, "no repository trivy job to take the pin from"
    pins = {step["uses"] for _, step in theirs}
    versions = {step["with"]["version"] for _, step in theirs}
    runners = {str(job["runs-on"]) for job, _ in theirs}
    assert len(pins) == len(versions) == len(runners) == 1, (pins, versions, runners)

    jobs = _workflow()["jobs"]
    scan = jobs["scan"]
    (mine,) = _trivy_steps(scan)
    assert mine["uses"] == pins.pop()
    assert mine["with"]["version"] == versions.pop()
    runner = runners.pop()
    assert str(scan["runs-on"]) == runner and str(jobs["list"]["runs-on"]) == runner
    assert scan["steps"][0]["uses"].startswith("actions/checkout@")
    w = mine["with"]
    assert w["scan-type"] == "image"
    assert w["image-ref"] == "${{ matrix.image }}"
    assert w["severity"] == "HIGH,CRITICAL"
    assert w["ignore-unfixed"] is True
    assert str(w["exit-code"]) == "1"


def test_no_expression_reaches_a_shell():
    """FIX: a name read from a file reaches the scan as an action input, never
    pasted into a run: script, where a quote in it would be shell."""
    for name, job in _workflow()["jobs"].items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), (name, step.get("name"))
