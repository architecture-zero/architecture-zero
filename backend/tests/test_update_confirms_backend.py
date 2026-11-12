"""The runbook's update ends by asking the backend whether it came up.

`docker compose up -d` returns once the containers are started, and the
backend is restarted whenever it exits. An update whose backend cannot boot -
a setting it refuses, a database change that fails - therefore comes back
from `up` looking like success and then restarts in a loop. The runbook's
"Updating" section gave four commands and stopped at `up`: nothing told the
person updating, or the automation they copied the block into, to confirm
that the backend answers.

The block now ends with backend/scripts/wait_backend_healthy.sh, which waits
for the backend container to answer its own health route and exits 1 if it
never does (its behavior: test_wait_backend_healthy.py).

FIX: the Updating block runs the script right after `up`, on the container
name the compose file gives the backend; the section says what a failure
prints, that it changes nothing, and where the reason is; the script is in
the repository.
CONTROL (true before and after): the four commands are still there in their
order, and the compose file still gives the backend that name and still
restarts it unless stopped - the two facts the new paragraph rests on.
"""
from pathlib import Path

import pytest
import yaml  # PyYAML: installed with uvicorn[standard] and chromadb (requirements.txt)

REPO = Path(__file__).resolve().parents[2]
RUNBOOK = REPO / "docs" / "runbook.md"
COMPOSE = REPO / "docker-compose.yml"
SCRIPT = "backend/scripts/wait_backend_healthy.sh"

pytestmark = pytest.mark.skipif(
    not (RUNBOOK.exists() and COMPOSE.exists()),
    reason="the runbook and the compose file are not beside this suite")

BEFORE = [
    "docker compose stop",
    "sudo cp -a backend/data ../data-before-update-$(date +%Y%m%d-%H%M%S)",
    "git pull",
    "docker compose up -d --build",
]


def _updating() -> str:
    """The "Updating" section, up to the next heading of its level."""
    text = RUNBOOK.read_text(encoding="utf-8")
    body = text.split("\n## Updating\n", 1)[1]
    return body.split("\n## ", 1)[0]


def _first_block() -> list:
    """The commands of the section's first indented block."""
    block = []
    for line in _updating().splitlines():
        if line.startswith("    "):
            block.append(line.strip())
        elif block:
            break
    return block


def _backend_container() -> str:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["backend"]["container_name"]


def test_the_update_ends_by_waiting_for_the_backend_to_answer():
    block = _first_block()
    call = "bash %s %s" % (SCRIPT, _backend_container())
    assert block[-1] == call, block
    assert block.index(call) == block.index("docker compose up -d --build") + 1
    assert "||" not in block[-1]          # its exit code is the update's


def test_the_section_says_what_a_failure_means_and_where_the_reason_is():
    text = " ".join(_updating().split())
    assert "comes back from `up` looking like success" in text
    assert "exits 1" in text
    assert "nothing from its log or its environment" in text
    assert "It changes nothing." in text
    assert "`docker compose logs --tail 50 backend` says why" in text
    assert "`WAIT_BACKEND_DEADLINE`" in text and "`EXPECT_GIT_SHA`" in text


def test_the_script_the_runbook_names_is_in_the_repository():
    assert (REPO / SCRIPT).is_file()


def test_control_the_four_commands_are_still_there_in_their_order():
    assert _first_block()[:4] == BEFORE


def test_control_the_backend_is_still_named_and_still_restarted_unless_stopped():
    backend = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["backend"]
    assert backend["container_name"] == "az_backend"
    assert backend["restart"] == "unless-stopped"
