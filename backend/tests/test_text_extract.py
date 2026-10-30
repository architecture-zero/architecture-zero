"""Shared extractor (app/text_extract.py) - factored out of the upload
endpoint so every ingest surface behaves identically; the contract must not
drift from what upload historically accepted."""
import pytest

from app.text_extract import ExtractError, extract_text, SUPPORTED_EXTS, TEXT_EXTS


def test_text_file_decodes():
    assert extract_text("notes.md", "hello world".encode()) == "hello world"


def test_historical_ext_list_unchanged():
    # Factoring must not silently widen the API's accepted types.
    assert set(TEXT_EXTS) == {"txt", "md", "py", "js", "ts", "json",
                              "yaml", "yml"}


def test_unsupported_type_flagged():
    with pytest.raises(ExtractError) as ei:
        extract_text("photo.png", b"\x89PNG")
    assert ei.value.unsupported is True


def test_empty_content_rejected_not_unsupported():
    with pytest.raises(ExtractError) as ei:
        extract_text("empty.txt", b"   \n  ")
    assert ei.value.unsupported is False


def test_supported_exts_names_only_types_extract_text_handles():
    # SUPPORTED_EXTS is what the upload picker is pinned to (2026-10-29), so
    # every type it names must reach a handler, never the unsupported refusal.
    for ext in SUPPORTED_EXTS:
        try:
            extract_text(f"f.{ext}", b"x")
        except ExtractError as e:
            assert not e.unsupported, ext
        except Exception:
            pass  # a handled type's parser refusing b"x" is not "unsupported"


def test_no_extension_unsupported():
    with pytest.raises(ExtractError) as ei:
        extract_text("Makefile", b"all: build")
    assert ei.value.unsupported is True
