"""Every service in the shipped compose file rotates its container log
(2026-11-10).

Docker's default json-file log driver keeps a container's whole output unless
it is capped, so a service that names no cap grows its log until the disk is
full. docker-compose.yml carries one top-level block (x-logging) that keeps a
log to 5 files of 20 MB, and every service names it.

FIX: each service's logging is that block, and the block caps both the size
of a file and the number kept. A service added without it fails here by name.
CONTROL: the same parse still reads what each service already said beside
the new line - the three services, each restarting unless stopped, the
backend with its 45 s stop - so the change added a setting and moved nothing.
"""
from pathlib import Path

import pytest
import yaml  # PyYAML: installed with uvicorn[standard] and chromadb (requirements.txt)

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

pytestmark = pytest.mark.skipif(not COMPOSE.exists(),
                                reason="docker-compose.yml is not in this checkout")

CAPPED = {"driver": "json-file", "options": {"max-size": "20m", "max-file": "5"}}
# A database or cache service is the one kind left without the block (the
# comment above it in the compose file says why). This file has none.
DATASTORES = []


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_the_block_caps_the_size_of_a_file_and_the_number_kept():
    assert _compose().get("x-logging") == CAPPED


def test_every_service_but_the_datastores_names_the_block():
    unrotated = sorted(name for name, service in _compose()["services"].items()
                       if service.get("logging") != CAPPED)
    assert unrotated == DATASTORES


def test_control_what_each_service_already_said_is_unchanged():
    services = _compose()["services"]
    assert {name: service["restart"] for name, service in services.items()} == {
        "backend": "unless-stopped", "frontend": "unless-stopped", "extractor": "unless-stopped"}
    assert services["backend"]["stop_grace_period"] == "45s"
