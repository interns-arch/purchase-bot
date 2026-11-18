"""The parts-enquiry CSV (User, 18 Nov 2026).

A vendor asked about SEVERAL parts gets, with the question, a CSV:

    Part No | Qty Needed | Available Qty | Available (Yes/No) | Discount % | Days (TAT)
    (bot)     (bot)        (vendor)        (vendor)             (vendor)     (vendor)

He fills his columns and sends the file back in his group. That file is his
answer to "Sir ye parts hai kya with pcs?" -- and to the discount and days
for the parts he has. Only what he left empty for a part he HAS is asked
afterwards, one thing at a time (steps.py). A typed answer still works.

The same file opened in Excel and saved as .xlsx is read too. A file that
does not look like this sheet is not taken -- it goes on to the stock import
as before (a vendor may send his stock sheet at any time)."""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backend.app.advance_orders import models as m
from backend.app.advance_orders.parser import LineAnswer

HEADERS = ["Part No", "Qty Needed", "Available Qty", "Available (Yes/No)", "Discount %", "Days (TAT)"]
SUFFIXES = {".csv", ".xlsx", ".xlsm", ".xls"}
_YES = {"yes", "y", "haan", "han", "ha", "hai", "available", "avl", "ok", "1", "true"}
_NO = {"no", "n", "nahi", "nhi", "na", "not available", "na.", "0", "false", "-"}


def _key(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def filename(query_id: int, today: date) -> str:
    # No PO word in it: whapi blocks PO-named files to vendors.
    return f"Cartrends_parts_enquiry_{today:%d%m%y}_{query_id}.csv"


def build(lines: list[m.AdvanceOrderLine]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(HEADERS)
    for line in lines:
        writer.writerow([line.part_number, line.qty, "", "", "", ""])
    # BOM so Excel on a phone/PC opens it as UTF-8 with columns intact.
    return ("﻿" + out.getvalue()).encode("utf-8")


def question(lines: list[m.AdvanceOrderLine]) -> str:
    rows = "\n".join(f"{i}. {line.part_number} x{line.qty}" for i, line in enumerate(lines, start=1))
    return (
        f"Sir ye parts hai kya with pcs?\n\n{rows}\n\n"
        "CSV file bhi bhej raha hoon -- *Available Qty*, *Available (Yes/No)*, *Discount %* aur *Days* "
        "bhar ke wapas bhej dijiye. "
        "Ya yahin likh dijiye."
    )


def reminder(lines: list[m.AdvanceOrderLine]) -> str:
    return "🔔 " + question(lines).replace("CSV file bhi bhej raha hoon", "Upar wali CSV file")


# ------------------------------------------------------------------ reading
def _grid(path: Path) -> list[list[str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        from core.ingestion.csv_reader import read_csv_grid

        return read_csv_grid(path)
    from core.ingestion.excel_reader import read_excel_grid

    return read_excel_grid(path)


def _columns(header: list[str]) -> dict[str, int] | None:
    found: dict[str, int] = {}
    for i, cell in enumerate(header):
        text = str(cell or "").strip().lower()
        if not text:
            continue
        if "disc" in text:
            found.setdefault("disc", i)
        elif any(w in text for w in ("day", "tat", "din", "eta", "lead", "delivery")):
            found.setdefault("days", i)
        elif "yes" in text or "y/n" in text or ("available" in text and "qty" not in text and "quantity" not in text):
            found.setdefault("yes", i)
        elif ("avail" in text or "avl" in text or "stock" in text) and ("qty" in text or "quantity" in text or "pcs" in text):
            found.setdefault("avail", i)
        elif "part" in text:
            found.setdefault("part", i)
        elif "need" in text or "required" in text or text in ("qty", "quantity"):
            found.setdefault("need", i)
    if "part" not in found or not ({"yes", "avail"} & set(found)):
        return None
    return found


def _number(value: object) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group())
    except InvalidOperation:
        return None


def read(path: Path, lines: list[m.AdvanceOrderLine]) -> dict[int, LineAnswer] | None:
    """{line_id: answer} from the vendor's filled sheet; None when the file is
    not this sheet, or names none of these parts, or he filled nothing."""
    try:
        grid = _grid(Path(path))
    except Exception:  # noqa: BLE001 -- not readable as a sheet: not ours
        return None
    header_at, cols = None, None
    for i, row in enumerate(grid[:10]):
        cols = _columns(row)
        if cols:
            header_at = i
            break
    if cols is None:
        return None
    by_key = {_key(line.part_number): line for line in lines}
    out: dict[int, LineAnswer] = {}
    for row in grid[header_at + 1:]:
        def cell(name: str) -> str:
            i = cols.get(name)
            return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

        line = by_key.get(_key(cell("part")))
        if line is None or line.id in out:
            continue
        said = cell("yes").lower().strip(" .")
        qty = _number(cell("avail"))
        if said in _NO or (not said and qty is not None and qty == 0):
            out[line.id] = LineAnswer(available=False, available_qty=0)
        elif said in _YES or (qty is not None and qty > 0):
            have = int(qty) if qty is not None and qty > 0 else line.qty
            disc = _number(cell("disc"))
            days = _number(cell("days"))
            out[line.id] = LineAnswer(
                available=True, available_qty=min(have, line.qty),
                discount_pct=disc if disc is not None and 0 <= disc <= 100 else None,
                tat_days=int(days) if days is not None and days == int(days) and 0 <= days <= 120 else None,
            )
        # a row he left empty: not answered
    if not out:
        return None
    # Parts he left out of a filled sheet: not available from him (as with text).
    for line in lines:
        out.setdefault(line.id, LineAnswer(available=False, available_qty=0))
    return out
