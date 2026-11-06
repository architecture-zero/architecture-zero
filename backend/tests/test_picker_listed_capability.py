"""The chat picker reads what a vendor's own list row says a model cannot do
(2026-11-06).

Read live that night: Groq's rows carry `output_modalities` (its Orpheus voice
models answer in "speech", Whisper in "transcription") and Mistral's carry
`capabilities.completion_chat`; Gemini's carry neither. The id word list
missed the Orpheus pair (no "tts" in their ids), so they reached the chat
picker. Where the row says it, the row decides; the word list stays the
backstop for rows that say nothing.
"""
from unittest.mock import MagicMock, patch

import pytest

import app.routers.settings as s
from app import model_catalog


@pytest.fixture(autouse=True)
def _clean():
    # getattr: the same file runs against the code before this change (the
    # fail-before control), where the set does not exist yet.
    noted = getattr(model_catalog, "_listed_not_for_chat", set())
    s._compat_models_cache.clear()
    noted.clear()
    yield
    s._compat_models_cache.clear()
    noted.clear()


def _live(rows):
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status.return_value = None
    r.json.return_value = {"data": rows}
    return r


GROQ_ROWS = [
    {"id": "canopylabs/orpheus-v1-english", "input_modalities": ["text"],
     "output_modalities": ["speech"]},
    {"id": "canopylabs/orpheus-arabic-saudi", "input_modalities": ["text"],
     "output_modalities": ["speech"]},
    {"id": "openai/gpt-oss-20b", "input_modalities": ["text"], "output_modalities": ["text"]},
    {"id": "qwen/qwen3.8-27b", "output_modalities": ["text"]},
]


def test_a_groq_voice_model_leaves_the_chat_picker():
    with patch("app.routers.settings.requests.get", return_value=_live(GROQ_ROWS)):
        values = [m["value"] for m in s._fetch_compat_models("groq")]
    assert "groq:canopylabs/orpheus-v1-english" not in values
    assert "groq:canopylabs/orpheus-arabic-saudi" not in values
    assert "groq:openai/gpt-oss-20b" in values and "groq:qwen/qwen3.8-27b" in values


def test_a_mistral_row_that_cannot_chat_leaves_the_chat_picker():
    rows = [
        {"id": "mistral-classifier-latest", "capabilities": {"completion_chat": False}},
        {"id": "mistral-small-latest", "capabilities": {"completion_chat": True}},
    ]
    with patch("app.routers.settings.requests.get", return_value=_live(rows)):
        values = [m["value"] for m in s._fetch_compat_models("mistral")]
    assert values == ["mistral-small-latest"]


def test_the_every_version_list_keeps_them_for_pinning():
    with patch("app.routers.settings.requests.get", return_value=_live(GROQ_ROWS)):
        every = [m["value"] for m in s._fetch_compat_models("groq", all_versions=True)]
    assert "groq:canopylabs/orpheus-v1-english" in every


def test_a_row_that_says_nothing_falls_to_the_word_list():
    # Gemini's rows carry only an id and a display name.
    rows = [{"id": "models/gemini-flash-latest", "display_name": "Gemini Flash Latest"},
            {"id": "models/gemini-robotics-er-2-preview", "display_name": "Robotics"}]
    with patch("app.routers.settings.requests.get", return_value=_live(rows)):
        values = [m["value"] for m in s._fetch_compat_models("gemini")]
    assert values == ["gemini-flash-latest"]


def test_a_later_row_that_can_chat_clears_the_note():
    model_catalog.note_listed_capability("groq:x/y", {"output_modalities": ["speech"]})
    assert model_catalog.listed_not_for_chat("groq:x/y")
    model_catalog.note_listed_capability("groq:x/y", {"output_modalities": ["text", "speech"]})
    assert not model_catalog.listed_not_for_chat("groq:x/y")
