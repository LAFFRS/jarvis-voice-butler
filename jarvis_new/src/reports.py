"""Saves research write-ups as Markdown files the user can open later."""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path


def reports_dir() -> Path:
    override = os.environ.get("JARVIS_REPORTS_DIR")
    return (
        Path(override).expanduser() if override else Path.home() / "Jarvis" / "reports"
    )


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60].rstrip("-") or "report"


def save_report(
    title: str,
    body: str,
    sources: list[str] | None = None,
    *,
    folder: Path | None = None,
    today: date | None = None,
) -> Path:
    if not body.strip():
        raise ValueError("The report is empty.")
    folder = folder or reports_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{(today or date.today()).isoformat()}-{slugify(title)}"

    path = folder / f"{stem}.md"
    counter = 2
    while path.exists():
        path = folder / f"{stem}-{counter}.md"
        counter += 1

    parts = [f"# {title.strip()}", "", body.strip(), ""]
    if sources:
        parts += ["## Sources", "", *[f"- {source}" for source in sources], ""]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
