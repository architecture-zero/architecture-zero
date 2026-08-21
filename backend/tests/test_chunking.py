from app.chunking import (
    CHUNK_OVERLAP, CHUNK_SIZE, chunk_plain, chunk_dated_markdown, chunk_markdown_sections,
)


def test_chunk_plain_matches_legacy_behavior():
    text = "x" * 2500
    chunks = chunk_plain(text)
    # Legacy loop: 1000-char windows advancing by 800.
    assert [len(c) for c in chunks] == [1000, 1000, 900, 100]
    assert chunks[0][-CHUNK_OVERLAP:] == chunks[1][:CHUNK_OVERLAP]


def test_chunk_plain_short_text_is_single_chunk():
    assert chunk_plain("short") == ["short"]
    assert chunk_plain("") == []


def test_dated_markdown_splits_on_entries_and_stamps_dates():
    text = (
        "# Log\npreamble line\n\n"
        "## 2031-03-08 - Dock door retrofit\nshort entry body\n\n"
        "## 2031-03-07 - Long inventory day\n" + ("y" * 3000) + "\n\n"
        "## Undated section\nno date here\n"
    )
    parts = chunk_dated_markdown(text)

    pre = [p for p in parts if p["entry_date"] is None and "preamble" in p["text"]]
    assert len(pre) == 1

    short = [p for p in parts if p["entry_date"] == "2031-03-08"]
    assert len(short) == 1 and short[0]["text"].startswith("## 2031-03-08")

    # Oversized entry: sub-chunked, each piece re-carries the dated heading.
    long = [p for p in parts if p["entry_date"] == "2031-03-07"]
    assert len(long) > 1
    for p in long:
        assert p["text"].startswith("## 2031-03-07 - Long inventory day")

    undated = [p for p in parts if "Undated section" in p["text"]]
    assert len(undated) == 1 and undated[0]["entry_date"] is None

    # No chunk straddles two entries.
    for p in parts:
        assert p["text"].count("\n## ") == 0


def test_dated_markdown_without_headings_falls_back_to_plain():
    text = "no headings at all " * 100
    parts = chunk_dated_markdown(text)
    assert [p["text"] for p in parts] == chunk_plain(text)
    assert all(p["entry_date"] is None for p in parts)


def test_markdown_sections_keeps_dense_facts_whole():
    # A fact doc with small single-topic sections: each section must come out
    # as ONE chunk - the dense embedding target. Blind fixed-size windows
    # dilute atomic facts below the retrieval pool-entry bar.
    text = (
        "# Vendor Handbook\npreamble identity line\n\n"
        "## Payment Terms\n\nNet-30 from invoice receipt; 2% discount within 10 days.\n\n"
        "## Claims\n\nDamage claims must be filed within 14 days of delivery.\n"
    )
    chunks = chunk_markdown_sections(text)
    edu = [c for c in chunks if "Payment Terms" in c]
    assert len(edu) == 1
    assert "Net-30" in edu[0] and edu[0].startswith("## Payment Terms")
    # No chunk straddles two sections, preamble survives on its own.
    assert all(c.count("\n## ") == 0 for c in chunks)
    assert any("preamble identity line" in c and "## " not in c.splitlines()[0][:3] for c in chunks)


def test_markdown_sections_oversized_section_recarries_heading():
    text = "## Shipping SLAs\n" + ("z" * 3000)
    chunks = chunk_markdown_sections(text)
    assert len(chunks) > 1
    assert all(c.startswith("## Shipping SLAs") for c in chunks)


def test_markdown_sections_without_headings_falls_back_to_plain():
    text = "no headings at all " * 100
    assert chunk_markdown_sections(text) == chunk_plain(text)


def test_markdown_sections_matches_dated_texts():
    # The general mode IS the dated splitter minus dates - parity guard so the
    # two modes can't silently diverge.
    text = "# T\npre\n\n## 2031-03-08 - dated\nbody\n\n## Plain section\n" + ("w" * 2500)
    assert chunk_markdown_sections(text) == [p["text"] for p in chunk_dated_markdown(text)]


def test_recency_multiplier_shapes():
    from app.database import _recency_multiplier
    from app.rag_config import RECENCY_FLOOR

    assert _recency_multiplier({}) == 1.0
    assert _recency_multiplier({"entry_date": None}) == 1.0
    assert _recency_multiplier({"entry_date": "not-a-date"}) == 1.0

    from datetime import date, timedelta
    today = date.today()
    fresh = (today - timedelta(days=1)).isoformat()
    old = (today - timedelta(days=5000)).isoformat()
    assert _recency_multiplier({"entry_date": fresh}) > 0.99
    assert _recency_multiplier({"entry_date": old}) == RECENCY_FLOOR
    # Monotonic: older never outweighs newer.
    mid = (today - timedelta(days=200)).isoformat()
    assert (_recency_multiplier({"entry_date": fresh})
            > _recency_multiplier({"entry_date": mid})
            >= _recency_multiplier({"entry_date": old}))
