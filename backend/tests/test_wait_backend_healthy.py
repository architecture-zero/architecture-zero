"""wait_backend_healthy.sh: after the stack is brought up, every backend
container must answer its own health route, or the run fails.

`docker compose up -d` returns once the containers are started. A backend that
cannot boot exits, its restart policy starts it again, and `up` has already
returned 0 - so a deploy that stops at `up` reports success for a backend that
never answered. The script is the step that asks. These tests drive it against
a stand-in `docker` - a small executable on a PATH that holds no other, which
a lookup confirms before any case runs - and never against the real one.

FIX (each fails where the script is missing):
  - a backend that answers: exit 0
  - one that never answers: exit 1 inside the deadline, with one ::error::
    line that names the container and its state
  - a container that answers but reports another build: exit 1
  - two names, the second failing: exit 1, and only the second is named
  - it keeps asking until the backend answers (the third try here)
  - a miss changes nothing: the only calls are `exec` and `inspect`
  - called wrongly it checks nothing and says so (exit 2)
  - the script is LF-only ASCII with no eval
CONTROL (these pass with or without the script, by design):
  - nothing the stand-in prints as a log, an environment or a traceback
    reaches the output
  - the stand-in really does print the marker - the control's own instrument

They need bash. On Linux, where CI and a deployment run the script, a missing
bash FAILS them. On Windows they use the bash that Git for Windows installs
and skip, saying so, when there is none.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wait_backend_healthy.sh"
MARKER = "MARKER-that-must-not-be-printed"

# The stand-in. It answers from small files in $FAKE_DOCKER_STATE and writes
# every call it gets to calls.log there. Whatever a careless caller might echo
# - a log, an environment, a traceback, an error - carries the marker.
STUB = r"""#!/bin/sh
s="$FAKE_DOCKER_STATE"
printf '%s\n' "$*" >> "$s/calls.log"
sub="${1:-}"
[ "$#" -gt 0 ] && shift
case "$sub" in
  exec)
    name="$1"; shift
    if [ "$1" = "printenv" ]; then
      if [ -f "$s/$name.sha" ]; then cat "$s/$name.sha"; exit 0; fi
      echo "SOME_SETTING=@MARKER@"
      exit 1
    fi
    tries=0
    [ -f "$s/$name.tries" ] && tries=$(cat "$s/$name.tries")
    tries=$((tries + 1))
    echo "$tries" > "$s/$name.tries"
    [ -f "$s/$name.healthy" ] && exit 0
    if [ -f "$s/$name.answers_on" ] && [ "$tries" -ge "$(cat "$s/$name.answers_on")" ]; then exit 0; fi
    echo "Traceback (most recent call last): the value @MARKER@ was refused" >&2
    echo "@MARKER@"
    exit 1
    ;;
  inspect)
    if [ "${1:-}" = "--format" ]; then
      name="$3"
      if [ -f "$s/$name.state" ]; then cat "$s/$name.state"; exit 0; fi
      echo "Error: No such object: $name @MARKER@" >&2
      exit 1
    fi
    echo '[{"Config": {"Env": ["SOME_SETTING=@MARKER@"]}}]'
    exit 0
    ;;
  logs)
    echo "refusing to start: SOME_SETTING is @MARKER@, which is not one of the two words"
    exit 0
    ;;
  *)
    echo "docker stand-in: unexpected call @MARKER@" >&2
    exit 64
    ;;
esac
"""


def _bash(tmp_path):
    """(bash, the folders that go on the PATH beside the stand-in), or
    (None, []) without a bash. Those folders hold the two programs the script
    and the stand-in call, sleep and cat, and no docker."""
    if os.name != "nt":
        bash = shutil.which("bash")
        if bash is None:
            return None, []
        # A folder of its own with links to those two programs alone: the
        # system's folders may hold a real docker.
        tools = tmp_path / "tools"
        tools.mkdir()
        for tool in ("sleep", "cat"):
            found = shutil.which(tool)
            if found is None:
                pytest.fail("no `%s` on this machine - the wait script needs it" % tool)
            os.symlink(found, str(tools / tool))
        return bash, [str(tools)]
    # On Windows a bare `bash` can be the WSL launcher, which runs in another
    # filesystem. Git for Windows ships a real one beside git itself, and its
    # own folders hold sleep and cat and no docker. The one under usr\bin, not
    # bin\bash.exe: that one is a wrapper that puts folders of its own (the
    # user's home bin among them) AHEAD of the PATH it is handed, and this
    # one searches exactly the PATH it is given.
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
    def __init__(self, code, output, seconds, state):
        self.code, self.output, self.seconds, self._state = code, output, seconds, state

    @property
    def errors(self):
        return [ln for ln in self.output.splitlines() if ln.startswith("::error")]

    @property
    def calls(self):
        log = self._state / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def tries(self, name):
        f = self._state / (name + ".tries")
        return int(f.read_text().strip()) if f.exists() else 0


@pytest.fixture
def box(tmp_path):
    """A folder holding the stand-in and its answers, and a runner for the
    script whose PATH is that folder and the two programs it needs - nothing
    else. Before any case runs, a lookup checks that the `docker` this bash
    finds on that PATH is the stand-in itself."""
    bash, tools = _bash(tmp_path)
    if bash is None:
        if os.name == "nt":
            pytest.skip("no Git for Windows bash on this machine; CI runs these on Linux")
        pytest.fail("bash is not installed, so the wait script cannot run here - "
                    "and a deploy from this machine could not run it either")
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    stub = bin_dir / "docker"
    stub.write_bytes(STUB.replace("@MARKER@", MARKER).encode("ascii"))   # LF, on any platform
    stub.chmod(0o755)

    def env_for(extra):
        env = dict(os.environ)
        for name in ("EXPECT_GIT_SHA", "WAIT_BACKEND_DEADLINE", "WAIT_BACKEND_INTERVAL"):
            env.pop(name, None)
        env["PATH"] = os.pathsep.join([str(bin_dir)] + tools)
        env["FAKE_DOCKER_STATE"] = state.as_posix()
        env.update(extra)
        return env

    # Lookup only, nothing is run: the `docker` this bash finds on that PATH
    # must be the stand-in itself. A real docker in a folder the shell adds on
    # its own fails here, before any case could call it.
    found = subprocess.run([bash, "-c", '[ "$(command -v docker)" -ef "$FAKE_DOCKER_STUB" ]'],
                           env=env_for({"FAKE_DOCKER_STUB": stub.as_posix()}),
                           cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    if found.returncode != 0:
        pytest.fail("the `docker` this bash would call is not the stand-in - "
                    "no case was run")

    class Box:
        def put(self, name, kind, text=""):
            (state / ("%s.%s" % (name, kind))).write_bytes((text + "\n").encode("ascii"))

        def run(self, *names, deadline="0", interval="0.1", **extra):
            settings = {"WAIT_BACKEND_DEADLINE": deadline, "WAIT_BACKEND_INTERVAL": interval}
            settings.update(extra)
            env = env_for(settings)
            started = time.monotonic()
            done = subprocess.run([bash, str(SCRIPT), *names], env=env, cwd=str(tmp_path),
                                  capture_output=True, text=True, timeout=180)
            return Outcome(done.returncode, done.stdout + done.stderr,
                           time.monotonic() - started, state)

        def docker(self, *args):
            """The stand-in called directly, as the script would find it."""
            done = subprocess.run([bash, "-c", 'docker "$@"', "docker", *args], env=env_for({}),
                                  cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
            return done.stdout + done.stderr

    return Box()


# --- FIX ------------------------------------------------------------------------

def test_a_backend_that_answers_passes(box):
    box.put("az_backend", "healthy")
    r = box.run("az_backend")
    assert r.code == 0, r.output
    assert r.errors == []
    assert "az_backend answered /api/health" in r.output
    assert r.tries("az_backend") == 1          # it went on at the first answer


def test_a_backend_that_never_answers_fails_inside_the_deadline_and_is_named(box):
    box.put("az_backend", "state", "restarting 7 1")
    r = box.run("az_backend", deadline="1", interval="0.2")
    assert r.code == 1, r.output
    assert r.seconds < 90, "a 1 s deadline took %.0f s" % r.seconds
    # How many times it asked inside one second depends on the machine's load,
    # so it is not counted here: test_it_keeps_asking_until_the_backend_answers
    # is the case that proves it asks again.
    assert r.tries("az_backend") >= 1
    assert len(r.errors) == 1, r.output
    line = r.errors[0]
    assert "az_backend did not answer /api/health within 1s" in line
    assert "status=restarting restarts=7 exit_code=1" in line
    assert "docker logs --tail 50 az_backend" in line


def test_a_container_that_is_not_there_is_named_as_unreadable(box):
    r = box.run("az_backend")
    assert r.code == 1, r.output
    assert len(r.errors) == 1 and "az_backend did not answer" in r.errors[0]
    assert "no container of that name could be read" in r.errors[0]


def test_a_container_reporting_another_build_fails(box):
    box.put("az_backend", "healthy")
    box.put("az_backend", "sha", "aaaaaaa")
    r = box.run("az_backend", EXPECT_GIT_SHA="bbbbbbb")
    assert r.code == 1, r.output
    assert len(r.errors) == 1, r.output
    line = r.errors[0]
    assert "az_backend answers /api/health" in line
    assert "reports build aaaaaaa" in line and "this deploy is build bbbbbbb" in line


@pytest.mark.parametrize("reported,expected", [
    ("abc1234", "abc1234"),
    ("abc1234", "abc1234" + "0" * 33),     # the container's short id opens the full one
    ("abc12345", "abc1234"),               # and the other way round
])
def test_the_expected_build_passes(box, reported, expected):
    box.put("az_backend", "healthy")
    box.put("az_backend", "sha", reported)
    r = box.run("az_backend", EXPECT_GIT_SHA=expected)
    assert r.code == 0, r.output
    assert r.errors == []
    assert "build " + reported in r.output


def test_an_image_built_without_a_build_id_fails_when_one_is_expected(box):
    box.put("az_backend", "healthy")
    box.put("az_backend", "sha", "unknown")
    r = box.run("az_backend", EXPECT_GIT_SHA="abc1234")
    assert r.code == 1, r.output
    assert len(r.errors) == 1 and "reports build unknown" in r.errors[0]


def test_a_container_with_no_build_id_at_all_fails_when_one_is_expected(box):
    box.put("az_backend", "healthy")           # and no build id to hand out
    r = box.run("az_backend", EXPECT_GIT_SHA="abc1234")
    assert r.code == 1, r.output
    assert len(r.errors) == 1 and "reports build none" in r.errors[0]


def test_with_no_build_expected_the_build_is_not_asked(box):
    box.put("az_backend", "healthy")
    r = box.run("az_backend")
    assert r.code == 0, r.output
    assert not any("printenv" in call for call in r.calls)


def test_two_names_the_second_failing_fails_the_run_and_only_it_is_named(box):
    box.put("az_backend", "healthy")
    box.put("az_peer", "state", "running 0 0")
    r = box.run("az_backend", "az_peer")
    assert r.code == 1, r.output
    assert len(r.errors) == 1, r.output
    assert "az_peer did not answer" in r.errors[0]
    assert "az_backend answered /api/health" in r.output


def test_every_name_is_asked_even_after_one_has_failed(box):
    box.put("az_peer", "healthy")
    r = box.run("az_backend", "az_peer")
    assert r.code == 1, r.output
    assert len(r.errors) == 1 and "az_backend did not answer" in r.errors[0]
    assert "az_peer answered /api/health" in r.output


def test_it_keeps_asking_until_the_backend_answers(box):
    box.put("az_backend", "answers_on", "3")
    r = box.run("az_backend", deadline="120", interval="0.1")
    assert r.code == 0, r.output
    assert r.tries("az_backend") == 3
    assert r.errors == []


def test_a_miss_changes_nothing(box):
    """No restart, no stop, no removal, no rollback, and no read of the log:
    the script's only calls are the request inside the container and the
    three state fields."""
    box.put("az_backend", "state", "restarting 3 1")
    r = box.run("az_backend")
    assert r.code == 1, r.output
    assert r.calls, "the stand-in was never called"
    assert {call.split(" ", 1)[0] for call in r.calls} == {"exec", "inspect"}, r.calls
    for call in r.calls:
        if call.startswith("inspect"):
            assert call.startswith("inspect --format {{.State.Status}} {{.RestartCount}} "
                                   "{{.State.ExitCode}} "), call


def test_an_expected_build_that_is_empty_is_an_error_never_no_check(box):
    box.put("az_backend", "healthy")
    r = box.run("az_backend", EXPECT_GIT_SHA="")
    assert r.code == 2, r.output
    assert len(r.errors) == 1 and "nothing was checked" in r.errors[0]
    assert r.calls == []


def test_called_with_no_name_it_checks_nothing_and_says_so(box):
    r = box.run()
    assert r.code == 2, r.output
    assert len(r.errors) == 1 and "nothing was checked" in r.errors[0]
    assert r.calls == []


@pytest.mark.parametrize("name", ["-rf", "two words", "a;b", "$(id)", "a/b", ""])
def test_a_name_that_is_not_a_container_name_is_refused_before_any_call(box, name):
    r = box.run("az_backend", name)
    assert r.code == 2, r.output
    assert len(r.errors) == 1 and "nothing was checked" in r.errors[0]
    assert r.calls == []


@pytest.mark.parametrize("setting,value", [("WAIT_BACKEND_DEADLINE", "soon"),
                                           ("WAIT_BACKEND_INTERVAL", "1.2.3")])
def test_a_deadline_or_interval_that_is_not_a_number_is_refused(box, setting, value):
    box.put("az_backend", "healthy")
    r = box.run("az_backend", **{setting: value})
    assert r.code == 2, r.output
    assert r.calls == []


def test_the_script_is_lf_only_ascii_with_no_eval():
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r" not in raw, "a CRLF shell script dies at its first line on Linux"
    text = raw.decode("ascii")                     # raises on anything else
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("eval" in ln.split() or "`" in ln for ln in code)


# --- CONTROL --------------------------------------------------------------------

def test_control_nothing_from_a_log_an_environment_or_a_traceback_reaches_the_output(box):
    outputs = []
    box.put("never", "state", "restarting 7 1 " + MARKER)       # junk after the three fields
    outputs.append(box.run("never").output)
    box.put("junk", "state", MARKER + " " + MARKER + " " + MARKER)   # junk in place of them
    outputs.append(box.run("junk").output)
    outputs.append(box.run("absent").output)                    # inspect itself fails
    box.put("other", "healthy")
    box.put("other", "sha", "aaaaaaa")
    outputs.append(box.run("other", EXPECT_GIT_SHA="bbbbbbb").output)
    box.put("odd", "healthy")
    box.put("odd", "sha", MARKER)                               # a build id that is not one
    outputs.append(box.run("odd", EXPECT_GIT_SHA="bbbbbbb").output)
    box.put("mute", "healthy")                                  # printenv fails and prints
    outputs.append(box.run("mute", EXPECT_GIT_SHA="bbbbbbb").output)
    for out in outputs:
        assert MARKER not in out, out


def test_control_the_stand_in_does_print_the_marker(box):
    """The control above proves nothing unless its instrument works: the log,
    the whole inspect record, a failed request and a failed printenv all carry
    the marker when the stand-in is called directly."""
    assert MARKER in box.docker("logs", "az_backend")
    assert MARKER in box.docker("inspect", "az_backend")
    assert MARKER in box.docker("exec", "az_backend", "python", "-c", "pass")
    assert MARKER in box.docker("exec", "az_backend", "printenv", "GIT_SHA")
