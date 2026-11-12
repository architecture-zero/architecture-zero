#!/usr/bin/env bash
#
# wait_backend_healthy.sh - after the stack is brought up, wait for each
# backend container to answer its own health route, and fail if one never does.
#
#   bash backend/scripts/wait_backend_healthy.sh <container> [<container> ...]
#
# WHY. `docker compose up -d` returns as soon as the containers are started.
# A backend that cannot boot - a setting it refuses, an import error, a failed
# migration - exits, its restart policy starts it again, and `up` has already
# returned 0. A deploy that stops at `up` reports success for a backend that
# never answered. This is the step that asks.
#
# WHAT IT DOES. For each container named, it runs a one-line Python request
# INSIDE that container against http://127.0.0.1:8000/api/health - so it holds
# whether or not the port is published on the host - every few seconds, and
# goes on the moment the container answers. A generous deadline therefore
# costs nothing on a healthy boot.
#
#   WAIT_BACKEND_DEADLINE  seconds for the whole run, every container together
#                          (default 300). Once it has passed, each container
#                          still to be asked gets one try.
#   WAIT_BACKEND_INTERVAL  seconds between two tries (default 2).
#   EXPECT_GIT_SHA         optional: the commit the running build must report
#                          in its GIT_SHA variable (the image carries it when
#                          the build is given it). With it, a container that
#                          answers but reports another build fails - an old
#                          container that is still answering is not a deploy.
#                          The shorter id only has to open the longer one. Set
#                          but empty is an error, never "no check".
#
# ON A MISS it prints one ::error:: line per container - GitHub Actions shows
# it on the run's page, anywhere else it is a plain line - with the
# container's name and its state (status, restart count, exit code), and exits
# 1 once every container has been asked.
#
# WHAT IT NEVER PRINTS: the container's log, or anything from its environment.
# A backend that refuses to boot can quote the value it refused, and the
# output of a deploy run is read by more people than the host is. Each state
# field is printed only when it looks like what it is (a word, two numbers)
# and a build id only when it is a commit id. The reason is on the host:
#
#   docker logs --tail 50 <container>
#
# WHAT IT NEVER DOES: restart, stop, remove or roll back anything. It reports.
# The request leaves no file behind in the container (python -B).
#
# Exit codes: 0 every container answered (and is the expected build, if one
# was given); 1 at least one did not; 2 it was called wrongly and checked
# nothing.

set -u
# Tracing off, even when the caller's shell options ask for it: a trace would
# print the two values this script reads from docker before they are checked.
set +x
export LC_ALL=C

TITLE="Backend health"
PROBE='import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:8000/api/health", timeout=5)'

# $1 is one of this script's own sentences, never something it was given.
usage_error() {
  echo "::error title=${TITLE}::wait_backend_healthy.sh: $1 - nothing was checked"
  exit 2
}

[ "$#" -ge 1 ] || usage_error "no container was named"

for name in "$@"; do
  case "$name" in
    "" | [!A-Za-z0-9]* | *[!A-Za-z0-9_.-]*)
      usage_error "a container name starts with a letter or a digit and holds only letters, digits, dot, dash and underscore"
      ;;
  esac
done

deadline="${WAIT_BACKEND_DEADLINE:-300}"
interval="${WAIT_BACKEND_INTERVAL:-2}"
[[ "$deadline" =~ ^[0-9]{1,5}$ ]] || usage_error "WAIT_BACKEND_DEADLINE must be a whole number of seconds"
[[ "$interval" =~ ^[0-9]{1,3}(\.[0-9]{1,3})?$ ]] || usage_error "WAIT_BACKEND_INTERVAL must be a number of seconds"
deadline=$((10#$deadline))

expect=""
if [ -n "${EXPECT_GIT_SHA+set}" ]; then
  expect="$EXPECT_GIT_SHA"
  [[ "$expect" =~ ^[0-9a-f]{4,40}$ ]] || usage_error "EXPECT_GIT_SHA is set but is not a commit id (4 to 40 lowercase hex digits)"
fi

command -v docker >/dev/null 2>&1 || usage_error "docker is not on PATH"

# The container's state, from three named fields and nothing else. A field is
# printed only when it looks like what it is; anything else reads "unknown".
container_state() {
  local raw status restarts code rest
  if ! raw="$(docker inspect --format '{{.State.Status}} {{.RestartCount}} {{.State.ExitCode}}' "$1" 2>/dev/null </dev/null)"; then
    echo "no container of that name could be read"
    return 0
  fi
  status="" restarts="" code="" rest=""
  read -r status restarts code rest <<<"$raw" || true
  [[ "$status" =~ ^[a-z]{1,20}$ ]] || status="unknown"
  [[ "$restarts" =~ ^[0-9]{1,9}$ ]] || restarts="unknown"
  [[ "$code" =~ ^-?[0-9]{1,9}$ ]] || code="unknown"
  echo "status=${status} restarts=${restarts} exit_code=${code}"
}

SECONDS=0
failed=0

for name in "$@"; do
  echo "wait_backend_healthy: asking ${name} for /api/health (deadline ${deadline}s for the whole run)"
  answered=0
  next_note=30
  while :; do
    if docker exec "$name" python -I -B -c "$PROBE" >/dev/null 2>&1 </dev/null; then
      answered=1
      break
    fi
    [ "$SECONDS" -lt "$deadline" ] || break
    if [ "$SECONDS" -ge "$next_note" ]; then
      echo "wait_backend_healthy: still waiting for ${name} after ${SECONDS}s ($(container_state "$name"))"
      next_note=$((SECONDS + 30))
    fi
    sleep "$interval"
  done

  if [ "$answered" -ne 1 ]; then
    msg="${name} did not answer /api/health within ${deadline}s ($(container_state "$name"))."
    msg="${msg} Nothing was rolled back. The reason is in its log, on the host:"
    msg="${msg} docker logs --tail 50 ${name}"
    echo "::error title=${TITLE}::${msg}"
    failed=1
    continue
  fi

  if [ -z "$expect" ]; then
    echo "wait_backend_healthy: ${name} answered /api/health after ${SECONDS}s"
    continue
  fi

  got="$(docker exec "$name" printenv GIT_SHA 2>/dev/null </dev/null)" || got=""
  got="${got//$'\r'/}"
  if [[ "$got" =~ ^[0-9a-f]{4,40}$ ]]; then
    shown="$got"
  elif [ "$got" = "unknown" ]; then
    shown="unknown (the image was built without GIT_SHA)"
    got=""
  elif [ -z "$got" ]; then
    shown="none (it carries no GIT_SHA)"
  else
    shown="something that is not a commit id"
    got=""
  fi
  # The same build when the shorter id opens the longer one.
  if [ -z "$got" ] || { [ "${expect#"$got"}" = "$expect" ] && [ "${got#"$expect"}" = "$got" ]; }; then
    msg="${name} answers /api/health, but it reports build ${shown} and this deploy is build ${expect}:"
    msg="${msg} the container that answered is not the one this deploy built, so the build or the"
    msg="${msg} recreate did not take. Nothing was rolled back. The output of the step that brought"
    msg="${msg} the stack up says which; the container's own log is on the host:"
    msg="${msg} docker logs --tail 50 ${name}"
    echo "::error title=${TITLE}::${msg}"
    failed=1
    continue
  fi
  echo "wait_backend_healthy: ${name} answered /api/health after ${SECONDS}s, build ${got}"
done

exit "$failed"
