"""pull_bases.sh: a build host pulls every Dockerfile base by name before it
builds (2026-11-21).

A host keeps the bases it pulled first and builds on them from then on: a
build without --pull takes the local tag, and a build's own --pull does not
move that tag. The script pulls each base that list_scanned_images.py --bases
names, so the builds after it start from what the registry holds now. These
drive it against a stand-in `docker` (a small executable on a PATH that holds
no other, which a lookup confirms before any case runs), in a throwaway
repository holding the script, the lister and fixture Dockerfiles, and never
against the real one.

FIX (each fails where the script is missing):
  - every base is pulled by name, once each, and nothing else is called
  - a pull that fails is one ::warning:: naming the image, the other bases are
    still pulled, and the script exits 0: an outage must not stop a deploy
  - a listing that fails stops the script, non-zero, before any pull
  - it finds its repository from wherever it is called (every case runs it
    from a folder outside the repository)
  - the script is LF-only ASCII with no eval
CONTROL (true with or without the script, by design):
  - the stand-in refuses a pull it was told to refuse: the instrument the
    warning case reads

They need bash. On Linux, where CI and a deployment run the script, a missing
bash FAILS them. On Windows they use the bash that Git for Windows installs
and skip, saying so, when there is none.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "pull_bases.sh"
LISTER = SCRIPTS / "list_scanned_images.py"

# The stand-in. It writes every call it gets to calls.log, refuses a pull of
# any image named in the file `fail`, and refuses anything that is not a pull.
STUB = r"""#!/bin/sh
s="$FAKE_DOCKER_STATE"
printf '%s\n' "$*" >> "$s/calls.log"
if [ "${1:-}" = "pull" ]; then
  img="$3"
  case " $(cat "$s/fail" 2>/dev/null) " in
    *" $img "*) echo "Error response from daemon: the stand-in refused $img" >&2; exit 1 ;;
  esac
  echo "sha256:0000"
  exit 0
fi
echo "docker stand-in: unexpected call" >&2
exit 64
"""

BASES = {
    "api/Dockerfile": "FROM python:3.11-slim\nRUN true\n",
    "web/Dockerfile": "FROM node:24-alpine AS build\nFROM build AS test\nFROM nginx:1.30-alpine\n",
    "tools/worker.Dockerfile": "FROM python:3.11-slim\n",
}
PULLS = ["pull -q nginx:1.30-alpine", "pull -q node:24-alpine", "pull -q python:3.11-slim"]


def _bash(tmp_path):
    """(bash, the folders that go on the PATH beside the stand-in), or
    (None, []) without a bash. Those folders hold the two programs the script
    and the stand-in call, dirname and cat, and no docker."""
    if os.name != "nt":
        bash = shutil.which("bash")
        if bash is None:
            return None, []
        tools = tmp_path / "tools"
        tools.mkdir()
        for tool in ("dirname", "cat"):
            found = shutil.which(tool)
            if found is None:
                pytest.fail("no `%s` on this machine - the script needs it" % tool)
            os.symlink(found, str(tools / tool))
        return bash, [str(tools)]
    # Git for Windows' own bash, the one under usr\bin (see
    # test_wait_backend_healthy.py for why not bin\bash.exe or a bare `bash`).
    roots = []
    git = shutil.which("git")
    if git:
        here = Path(git).resolve().parent
        roots += [here.parent, here.parent.parent]
    roots.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git")
    for root in roots:
        if (root / "usr" / "bin" / "bash.exe").is_file():
            return (str(root / "usr" / "bin" / "bash.exe"),
                    [str(root / "usr" / "bin"), str(root / "mingw64" / "bin")])
    return None, []


class Outcome:
    def __init__(self, code, output, state):
        self.code, self.output, self._state = code, output, state

    @property
    def warnings(self):
        return [ln for ln in self.output.splitlines() if ln.startswith("::warning")]

    @property
    def calls(self):
        """Split on LF alone, so a stray CR in an argument stays visible."""
        log = self._state / "calls.log"
        if not log.exists():
            return []
        return log.read_bytes().decode("utf-8").split("\n")[:-1]


@pytest.fixture
def box(tmp_path):
    """A throwaway repository (the script, the lister, fixture Dockerfiles), a
    folder holding the stand-in, and a runner whose PATH is that folder and
    the two programs the script needs. Before any case runs, a lookup checks
    that the `docker` this bash finds on that PATH is the stand-in itself."""
    bash, tools = _bash(tmp_path)
    if bash is None:
        if os.name == "nt":
            pytest.skip("no Git for Windows bash on this machine; CI runs these on Linux")
        pytest.fail("bash is not installed, so the script cannot run here - "
                    "and a deploy from this machine could not run it either")
    repo, bin_dir, state, elsewhere = (tmp_path / n for n in ("repo", "bin", "state", "elsewhere"))
    for d in (bin_dir, state, elsewhere, repo / "backend" / "scripts"):
        d.mkdir(parents=True)
    shutil.copyfile(SCRIPT, repo / "backend" / "scripts" / "pull_bases.sh")
    shutil.copyfile(LISTER, repo / "backend" / "scripts" / "list_scanned_images.py")
    stub = bin_dir / "docker"
    stub.write_bytes(STUB.encode("ascii"))   # LF, on any platform
    stub.chmod(0o755)

    def env_for(extra):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(bin_dir)] + tools)
        env["FAKE_DOCKER_STATE"] = state.as_posix()
        env["PULL_BASES_PYTHON"] = Path(sys.executable).as_posix()
        env.update(extra)
        return env

    found = subprocess.run([bash, "-c", '[ "$(command -v docker)" -ef "$FAKE_DOCKER_STUB" ]'],
                           env=env_for({"FAKE_DOCKER_STUB": stub.as_posix()}),
                           cwd=str(elsewhere), capture_output=True, text=True, timeout=60)
    if found.returncode != 0:
        pytest.fail("the `docker` this bash would call is not the stand-in - no case was run")

    class Box:
        def dockerfiles(self, files):
            for rel, text in files.items():
                p = repo / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(text.encode("ascii"))

        def refuse(self, *images):
            (state / "fail").write_bytes((" ".join(images) + "\n").encode("ascii"))

        def run(self):
            script = (repo / "backend" / "scripts" / "pull_bases.sh").as_posix()
            done = subprocess.run([bash, script], env=env_for({}), cwd=str(elsewhere),
                                  capture_output=True, text=True, timeout=120)
            return Outcome(done.returncode, done.stdout + done.stderr, state)

        def docker(self, *args):
            """The stand-in called directly, as the script would find it."""
            done = subprocess.run([bash, "-c", 'docker "$@"', "docker", *args], env=env_for({}),
                                  cwd=str(elsewhere), capture_output=True, text=True, timeout=60)
            return done.returncode, done.stdout + done.stderr

    return Box()


# --- FIX ------------------------------------------------------------------------

def test_every_base_is_pulled_by_name_once_and_nothing_else_is_called(box):
    box.dockerfiles(BASES)
    r = box.run()
    assert r.code == 0, r.output
    assert r.calls == PULLS
    assert r.warnings == []
    for line in PULLS:
        assert "base pulled: " + line.split()[-1] in r.output


def test_a_pull_that_fails_warns_and_the_rest_are_still_pulled(box):
    box.dockerfiles(BASES)
    box.refuse("node:24-alpine")
    r = box.run()
    assert r.code == 0, r.output
    assert r.calls == PULLS
    assert r.warnings == ["::warning::could not pull base node:24-alpine; this build uses the cached copy"]
    assert "base pulled: nginx:1.30-alpine" in r.output and "base pulled: python:3.11-slim" in r.output
    assert "base pulled: node:24-alpine" not in r.output


def test_a_listing_that_fails_stops_before_any_pull(box):
    box.dockerfiles(dict(BASES, **{"bad/Dockerfile": "FROM python:${PY_VERSION}-slim\n"}))
    r = box.run()
    assert r.code != 0, r.output
    assert r.calls == []
    assert "list_scanned_images: " in r.output and "through a variable" in r.output


def test_no_dockerfile_at_all_stops_before_any_pull(box):
    r = box.run()
    assert r.code != 0, r.output
    assert r.calls == []


def test_the_script_is_lf_only_ascii_with_no_eval():
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r" not in raw, "a CRLF shell script dies at its first line on Linux"
    text = raw.decode("ascii")                     # raises on anything else
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("eval" in ln.split() or "`" in ln for ln in code)
    assert "set -euo pipefail" in code


# --- CONTROL --------------------------------------------------------------------

def test_control_the_stand_in_refuses_what_it_was_told_to(box):
    box.refuse("alpine:3.20")
    assert box.docker("pull", "-q", "alpine:3.20")[0] == 1
    assert box.docker("pull", "-q", "alpine:3.21")[0] == 0
