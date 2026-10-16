from datetime import date
from pathlib import Path

import pytest

from reports import save_report, slugify

DAY = date(2026, 10, 16)


def test_slugify_makes_safe_windows_filenames() -> None:
    assert slugify('Best Python: web frameworks? <2026> "fast"') == (
        "best-python-web-frameworks-2026-fast"
    )
    assert slugify("???") == "report"
    assert len(slugify("x" * 200)) == 60


def test_save_report_writes_markdown_with_sources(tmp_path: Path) -> None:
    path = save_report(
        "Rust vs Go",
        "Both are fast.",
        ["https://a.example", "https://b.example"],
        folder=tmp_path,
        today=DAY,
    )

    assert path.name == "2026-10-16-rust-vs-go.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Rust vs Go\n\nBoth are fast.")
    assert "## Sources\n\n- https://a.example\n- https://b.example" in text


def test_save_report_never_overwrites(tmp_path: Path) -> None:
    first = save_report("Same", "one", folder=tmp_path, today=DAY)
    second = save_report("Same", "two", folder=tmp_path, today=DAY)

    assert first != second
    assert second.name == "2026-10-16-same-2.md"
    assert "one" in first.read_text(encoding="utf-8")


def test_empty_report_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        save_report("Nothing", "  ", folder=tmp_path)
