"""list_scanned_images.py, and the weekly image scan it feeds (2026-11-17).

The repository scan reads files and lockfiles. Nothing read the container
images the stack runs: the ones a compose file pulls and the ones it builds.
.github/workflows/image-scan.yml scans them, one matrix job per entry, and the
list is read from the files by the script. These pin what the list holds,
what it leaves out and why, and the wiring of the workflow that consumes it.

Since 2026-11-21 a Dockerfile a compose service builds is built by the scan
job and the result scanned, instead of its FROM lines scanned as pulled: a
build stage's base ships nothing, and the OS upgrade a Dockerfile applies on
top of its base was never seen.

FIX (each fails where the script or the workflow is missing):
  - a compose service's image: is listed; a commented-out service is not
  - a compose service's build: is listed as a build (a path, or a mapping
    with its Dockerfile read against its context and its target stage), once
    however many services share it, and the Dockerfile it builds has its
    bases left out of the pulls
  - every FROM of a Dockerfile no compose service builds is listed: nested,
    suffixed and prefixed file names, a continued line, a --platform flag
    stepped over
  - the same image spelled twice is listed once, an untagged one as :latest
  - an image or a build named through a variable, a name that is not an
    image reference, or a build this checkout cannot reproduce stops the
    listing (exit 2) instead of being dropped from it
  - an empty tree is exit 2, never an empty list
  - --json prints the entries as one line of JSON
  - the workflow: weekly and on demand only; contents: read and nothing
    more, no token left by a checkout; a matrix over the listing with
    fail-fast off; a build entry built from a fresh base and its image the
    one scanned; the repository scan's trivy pin, version and runner in
    image mode with the same flags; no expression pasted into a shell
CONTROL (one per skip rule; each fixture holds the thing the rule leaves out
beside a real image, so with the rule removed the wrong name is listed and
the test fails - each was mutation-checked that way when it was written):
  - a stage an earlier FROM named (FROM deps AS builder), any case
  - scratch
  - an image a compose service builds itself, and a service that runs it
  - the bases of a Dockerfile a compose service builds
  - a Dockerfile under node_modules
  - a Dockerfile inside a nested checkout (a git worktree)
GUARD:
  - this repository's own files list clean: at least one entry, each image
    tagged, each build's Dockerfile there
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


def test_a_compose_build_is_listed_as_built_and_its_bases_are_not_pulled(lsi, tmp_path):
    """FIX and CONTROL: each build: is one build, its Dockerfile read against
    its context, its target kept; two services building the same thing are
    one build. The bases of a built Dockerfile are not pulls (with that rule
    removed, python:3.11-slim and node:24-alpine are listed); a Dockerfile no
    service builds keeps its bases scanned as pulled."""
    _write(tmp_path, "docker-compose.yml",
           "services:\n"
           "  api:\n"
           "    build: ./api\n"
           "  api-peer:\n"
           "    build:\n"
           "      context: api\n"
           "  web:\n"
           "    build:\n"
           "      context: ./web\n"
           "      dockerfile: Dockerfile.web\n"
           "      target: runtime\n"
           "      args:\n"
           "        GIT_SHA: ${GIT_SHA:-unknown}\n"
           "  worker:\n"
           "    build:\n"
           "      dockerfile: tools/worker.Dockerfile\n")
    _write(tmp_path, "api/Dockerfile", "FROM python:3.11-slim\n")
    _write(tmp_path, "web/Dockerfile.web", "FROM node:24-alpine AS build\nFROM nginx:1.30-alpine AS runtime\n")
    _write(tmp_path, "tools/worker.Dockerfile", "FROM python:3.11-slim\n")
    _write(tmp_path, "other/Dockerfile", "FROM golang:1.23\n")
    assert lsi.list_builds(str(tmp_path)) == [
        (".", "tools/worker.Dockerfile", ""),
        ("api", "api/Dockerfile", ""),
        ("web", "web/Dockerfile.web", "runtime"),
    ]
    assert _listed(lsi, tmp_path) == ["golang:1.23"]


@pytest.mark.parametrize("build, says", [
    ("${APP_DIR}", "through a variable"),
    ("{context: ./api, dockerfile: \"${DOCKERFILE}\"}", "through a variable"),
    ("https://github.com/example/app.git", "remote context"),
    ("{context: ./api, dockerfile_inline: \"FROM alpine:3.20\"}", "dockerfile_inline"),
    ("../elsewhere", "outside the repository"),
    ("{context: ./api, dockerfile: ../../Dockerfile}", "outside the repository"),
    ("./missing", "not a directory"),
    ("{context: ./api, dockerfile: Dockerfile.gone}", "not there"),
    ("{context: ./api, target: \"run time\"}", "not a stage name"),
    ("[./api]", "neither a path nor a mapping"),
], ids=["variable-context", "variable-dockerfile", "remote", "inline", "context-outside",
        "dockerfile-outside", "no-context", "no-dockerfile", "bad-target", "not-a-build"])
def test_a_build_this_checkout_cannot_reproduce_stops_the_listing(lsi, tmp_path, capsys, build, says):
    """FIX: a build the scan job could not run from this checkout, or could
    only run as something other than what compose builds, is exit 2 with the
    reason, never a list without it."""
    _write(tmp_path, "api/Dockerfile", "FROM python:3.11-slim\n")
    _write(tmp_path, "docker-compose.yml",
           "services:\n"
           "  cache:\n"
           "    image: redis:7.4-alpine\n"
           "  app:\n"
           "    build: %s\n" % build)
    with pytest.raises(lsi.ListingError):
        _listed(lsi, tmp_path)
    assert lsi.main(["--root", str(tmp_path), "--json"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("list_scanned_images: ") and says in out.err


def test_every_dockerfile_from_is_listed(lsi, tmp_path):
    """FIX: every Dockerfile in the tree no compose service builds, whatever
    its spelling, every FROM in it; a continued line is joined and a
    --platform flag is not an image."""
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


def test_json_prints_the_entries_on_one_line(lsi, tmp_path, capsys):
    """FIX: --json is one line, because the workflow writes it to
    $GITHUB_OUTPUT as name=value: a pull names its image, a build its
    context, Dockerfile and (only when compose names one) target. Without
    --json, one name per line."""
    _write(tmp_path, "Dockerfile", "FROM python:3.11-slim\nFROM nginx:1.30-alpine\n")
    _write(tmp_path, "docker-compose.yml",
           "services:\n"
           "  app:\n"
           "    build: ./app\n"
           "  web:\n"
           "    build: {context: ./app, target: web}\n")
    _write(tmp_path, "app/Dockerfile", "FROM node:24-alpine AS web\n")
    assert lsi.main(["--root", str(tmp_path), "--json"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out) == [
        {"name": "nginx:1.30-alpine", "image": "nginx:1.30-alpine"},
        {"name": "python:3.11-slim", "image": "python:3.11-slim"},
        {"name": "app/Dockerfile", "context": "app", "dockerfile": "app/Dockerfile"},
        {"name": "app/Dockerfile (stage web)", "context": "app", "dockerfile": "app/Dockerfile",
         "target": "web"},
    ]
    assert lsi.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "nginx:1.30-alpine", "python:3.11-slim", "app/Dockerfile", "app/Dockerfile (stage web)"]


def test_bases_prints_every_dockerfile_base_built_or_not(lsi, tmp_path, capsys, monkeypatch):
    """FIX: --bases is what a build host pulls before it builds: the bases of
    every Dockerfile, the ones compose builds included (the scan leaves those
    out, a host must not), stages and scratch still not images. It reads no
    compose file, so it runs where PyYAML is not installed."""
    _write(tmp_path, "docker-compose.yml",
           "services:\n"
           "  app:\n"
           "    build: ./app\n"
           "  db:\n"
           "    image: postgres:16-alpine\n")
    _write(tmp_path, "app/Dockerfile", "FROM node:24-alpine AS build\nFROM build\nFROM nginx:1.30-alpine\n")
    _write(tmp_path, "tools/Dockerfile", "FROM golang:1.23 AS b\nFROM scratch\n")
    expected = ["golang:1.23", "nginx:1.30-alpine", "node:24-alpine"]
    assert lsi.list_bases(str(tmp_path)) == expected
    monkeypatch.setattr(lsi, "yaml", None)
    assert lsi.main(["--root", str(tmp_path), "--bases"]) == 0
    assert capsys.readouterr().out.splitlines() == expected
    # The scan's listing does need the compose file, and says so.
    assert lsi.main(["--root", str(tmp_path), "--json"]) == 2
    assert "PyYAML is not installed" in capsys.readouterr().err


def test_bases_with_no_dockerfile_is_exit_2(lsi, tmp_path, capsys):
    """FIX: no base at all is a broken reading for --bases too."""
    _write(tmp_path, "docker-compose.yml", "services:\n  db:\n    image: postgres:16-alpine\n")
    assert lsi.main(["--root", str(tmp_path), "--bases"]) == 2
    assert capsys.readouterr().out == ""


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
    _write(tmp_path, "backend/Dockerfile", "FROM python:3.11-slim\n")
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
    error, at least one entry, every image tagged or pinned by digest, every
    build's Dockerfile there."""
    entries = lsi.list_entries(str(REPO))
    assert entries
    for entry in entries:
        if "image" in entry:
            img = entry["image"]
            assert "@" in img or ":" in img.rsplit("/", 1)[-1], img
        else:
            assert (REPO / entry["dockerfile"]).is_file(), entry
            assert (REPO / entry["context"]).is_dir(), entry


def test_guard_no_compose_service_runs_a_base_by_its_tag(lsi):
    """GUARD: a build host may pull every Dockerfile base by name before it
    builds (--bases). A service in the compose file a bare `docker compose`
    runs there, running one of those tags as its own image, would then be
    recreated at its next `compose up -d` whenever the tag moved, dropping its
    open connections unplanned. A service image that is also a base is pinned
    to its exact version instead. Other compose files (a client deployment's,
    named with -f) run on hosts that pull their own way."""
    default = [p for p in lsi.compose_files(str(REPO))
               if Path(p).name in ("docker-compose.yml", "docker-compose.yaml",
                                   "compose.yml", "compose.yaml")]
    bases = set(lsi.list_bases(str(REPO)))
    images, _builds = lsi.read_compose(str(REPO), default)
    assert not bases & set(images), sorted(bases & set(images))


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
    """FIX: the first job prints the list, the scan runs once per entry, and
    one image's finding does not cancel the others."""
    jobs = _workflow()["jobs"]
    listing, scan = jobs["list"], jobs["scan"]
    runs = "\n".join(s.get("run", "") for s in listing["steps"])
    assert "python backend/scripts/list_scanned_images.py --json" in runs
    assert listing["outputs"]["entries"] == "${{ steps.list.outputs.entries }}"
    assert scan["needs"] == "list"
    assert scan["strategy"]["fail-fast"] is False
    assert scan["strategy"]["matrix"]["entry"] == "${{ fromJSON(needs.list.outputs.entries) }}"


def test_a_build_entry_is_built_and_its_image_is_the_one_scanned():
    """FIX: a build entry (and only a build entry) is built after the
    checkout, from a freshly pulled base, with its context, Dockerfile and
    target handed over as environment variables; the scan reads the tag that
    build wrote, and a pull entry's own image otherwise."""
    scan = _workflow()["jobs"]["scan"]
    steps = scan["steps"]
    builds = [s for s in steps if "docker build" in s.get("run", "")]
    (build,) = builds
    assert steps.index(build) > 0 and steps[0]["uses"].startswith("actions/checkout@")
    assert build["if"] == "${{ matrix.entry.dockerfile }}"
    assert build["env"] == {
        "BUILD_CONTEXT": "${{ matrix.entry.context }}",
        "BUILD_DOCKERFILE": "${{ matrix.entry.dockerfile }}",
        "BUILD_TARGET": "${{ matrix.entry.target }}",
    }
    run = build["run"]
    assert "--pull" in run
    assert '-f "$BUILD_DOCKERFILE"' in run and '"$BUILD_CONTEXT"' in run and "$BUILD_TARGET" in run
    tag = run.split(" -t ", 1)[1].split()[0]
    (trivy,) = _trivy_steps(scan)
    assert steps.index(trivy) > steps.index(build)
    assert trivy["with"]["image-ref"] == "${{ matrix.entry.image || '%s' }}" % tag


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
    assert w["image-ref"].startswith("${{ matrix.entry.image")
    assert w["severity"] == "HIGH,CRITICAL"
    assert w["ignore-unfixed"] is True
    assert str(w["exit-code"]) == "1"


def test_no_expression_reaches_a_shell():
    """FIX: a name read from a file reaches the scan as an action input, never
    pasted into a run: script, where a quote in it would be shell."""
    for name, job in _workflow()["jobs"].items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), (name, step.get("name"))
