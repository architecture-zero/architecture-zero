""".env.example says what an Azure deployment must be named (2026-10-30).

On Azure's v1 route the request's `model` is the deployment's name, and this
server reaches the openai entry only by a model name's prefix
(providers._resolve_model; an explicit openai:<name> needs the same prefix).
A deployment named otherwise goes to Ollama, with nothing said. The file
named the Azure base URL and not this, so an operator who named a deployment
"prod-chat" saw it answer from the local model server, or fail there.

FIX: the Azure paragraph names every prefix the openai entry routes by, the
openai:<name> form, and where any other name goes - pinned to the code, so a
prefix added there and not here fails. CONTROL (a guard, passing before):
routing does what the paragraph says.
"""
import re
from pathlib import Path

from app.providers import OPENAI_COMPAT, _resolve_model

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _azure_paragraph() -> str:
    """The comment block from the Azure base URL line to the next setting."""
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "openai.azure.com" in line)
    block = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        block.append(line.lstrip("#").strip())
    return " ".join(block)


def test_the_azure_paragraph_names_the_prefixes_routing_reads():
    para = _azure_paragraph()
    missing = [p for p in OPENAI_COMPAT["openai"]["prefixes"]
               if not re.search(rf"(?<![\w-]){re.escape(p)}(?![\w])", para)]
    assert not missing, f"prefixes the openai entry routes by, not named: {missing}"
    assert "openai:<name>" in para
    assert "Ollama" in para


def test_routing_does_what_the_paragraph_says():
    for name in ("gpt-4o", "o3-mini", "openai:gpt-4o"):
        assert _resolve_model(name)[0] == "openai", name
    for name in ("prod-chat", "openai:prod-chat"):
        assert _resolve_model(name)[0] == "ollama", name
