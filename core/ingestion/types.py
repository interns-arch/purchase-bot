from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedFile:
    """Uniform result shape shared by csv_reader and excel_reader."""

    rows: list[dict[str, str]]
    headers: list[str]
    encoding: str | None = None
    used_fallback_encoding: bool = False
    sheet_name: str | None = None
    ignored_sheets: list[str] = field(default_factory=list)
