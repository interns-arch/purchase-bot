"""Load part -> brand hints from stock sheets, for routing advance orders.

    python -m backend.scripts.import_part_brands "MOTORRIFIC % CARTRENDS.xlsx"
    python -m backend.scripts.import_part_brands "MOTORRIFIC % CARTRENDS.xlsx" --apply
    python -m backend.scripts.import_part_brands "..." --apply \
        --brand-alias "MARUTI ACCESSORIES=MARUTI SUZUKI ACCESSORIES"

DRY RUN BY DEFAULT. Reads every sheet that has a "PART NO" and a "BRAND"
column (the others -- a discount table, a discrepancy log -- are skipped),
and reports how many parts it would route and which brands have no vendor
list to route them to.

WHAT --apply WRITES
-------------------
  part_brand_hints   one row per part number, normalised

WHAT IT NEVER TOUCHES
---------------------
  parts, part_aliases, vendor_inventory -- the part master that inventory,
  allocation and Part Intelligence all read. Hints are a separate table read
  only by advance-order routing (see `models.PartBrandHint`).

BRAND ALIASES
-------------
A stock sheet and the vendor mapping can name one brand two ways
("MARUTI ACCESSORIES" vs "MARUTI SUZUKI ACCESSORIES"). The script does not
decide that two names are the same brand -- it reports the brands with no
vendor list, and you declare an alias with --brand-alias OLD=NEW, as many
times as needed.

A part listed under two different brands across sheets is reported and
skipped, never resolved by picking one.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import backend.app.advance_orders.models  # noqa: E402,F401 -- registers the tables
from backend.app.advance_orders import models as m  # noqa: E402
from core.db import engine, get_session, init_db  # noqa: E402
from core.ingestion.column_detector import normalise_part_number  # noqa: E402
from sqlalchemy import select  # noqa: E402


def parse_aliases(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--brand-alias needs OLD=NEW, got {value!r}")
        old, new = value.split("=", 1)
        old, new = " ".join(old.split()).upper(), " ".join(new.split()).upper()
        if not old or not new:
            raise SystemExit(f"--brand-alias needs OLD=NEW, got {value!r}")
        out[old] = new
    return out


def read_hints(path: Path, aliases: dict[str, str]) -> tuple[dict[str, set[str]], list[str]]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    brands_by_part: dict[str, set[str]] = defaultdict(set)
    used_sheets: list[str] = []
    for sheet in workbook.worksheets:
        rows = sheet.iter_rows(values_only=True)
        try:
            header = [" ".join(str(h or "").split()).upper() for h in next(rows)]
        except StopIteration:
            continue
        if "PART NO" not in header or "BRAND" not in header:
            continue
        i_part, i_brand = header.index("PART NO"), header.index("BRAND")
        used_sheets.append(sheet.title.strip())
        for values in rows:
            if len(values) <= max(i_part, i_brand):
                continue
            raw_part, raw_brand = values[i_part], values[i_brand]
            if raw_part is None or raw_brand is None:
                continue
            part_text = str(raw_part).strip()
            # Excel turns a numeric part number into 90331679003.0.
            if part_text.endswith(".0") and part_text[:-2].isdigit():
                part_text = part_text[:-2]
            key = normalise_part_number(part_text)
            brand = " ".join(str(raw_brand).split()).upper()
            if not key or not brand:
                continue
            brands_by_part[key].add(aliases.get(brand, brand))
    return brands_by_part, used_sheets


def mapped_brands(session) -> set[str]:
    return set(session.execute(select(m.VendorBrand.brand).distinct()).scalars())


def main() -> int:
    parser = argparse.ArgumentParser(description="Import part -> brand hints from stock sheets.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    parser.add_argument("--brand-alias", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--allow-sqlite", action="store_true", help="permit a local SQLite database")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"No such file: {args.path}", file=sys.stderr)
        return 2
    url = engine.url
    print(f"Target database: {url.render_as_string(hide_password=True)}")
    if url.get_backend_name() == "sqlite" and args.apply and not args.allow_sqlite:
        print(
            "ABORTING: this resolved to a local SQLite database. Set DATABASE_URL, or pass\n"
            "--allow-sqlite if you really mean to write to the local dev database.",
            file=sys.stderr,
        )
        return 2

    aliases = parse_aliases(args.brand_alias)
    init_db()
    brands_by_part, used_sheets = read_hints(args.path, aliases)

    clean = {part: next(iter(brands)) for part, brands in brands_by_part.items() if len(brands) == 1}
    conflicts = {part: brands for part, brands in brands_by_part.items() if len(brands) > 1}
    per_brand = Counter(clean.values())

    written = 0
    with get_session() as session:
        known_brands = mapped_brands(session)
        if args.apply:
            existing = {h.part_number: h for h in session.execute(select(m.PartBrandHint)).scalars()}
            for part, brand in clean.items():
                hint = existing.get(part)
                if hint is None:
                    session.add(m.PartBrandHint(part_number=part, brand=brand, source=args.path.name))
                    written += 1
                elif hint.brand != brand:
                    hint.brand = brand
                    hint.source = args.path.name
                    written += 1
        else:
            session.rollback()

    routed = sum(n for brand, n in per_brand.items() if brand in known_brands)
    unrouted = sorted(((n, b) for b, n in per_brand.items() if b not in known_brands), reverse=True)

    line = "-" * 78
    print(line)
    print("APPLIED -- written to the database" if args.apply else "DRY RUN -- nothing was written")
    print(line)
    print(f"Sheets read:              {', '.join(used_sheets) or 'none with PART NO + BRAND'}")
    print(f"Unique part numbers:      {len(brands_by_part)}")
    print(f"Brands:                   {len(per_brand)}")
    if aliases:
        print(f"Brand aliases applied:    {', '.join(f'{k} -> {v}' for k, v in aliases.items())}")
    if known_brands:
        share = f" ({100 * routed // max(1, len(clean))}%)"
        print(f"Parts with a vendor list: {routed}{share}")
    else:
        print("Parts with a vendor list: unknown -- vendor_brands is empty; import the vendor mapping first")
    if args.apply:
        print(f"Hints written or updated: {written}")

    if conflicts:
        print()
        print(f"PARTS UNDER TWO BRANDS -- skipped, not guessed ({len(conflicts)})")
        for part, brands in sorted(conflicts.items())[:50]:
            print(f"  {part}: {' | '.join(sorted(brands))}")
    if known_brands and unrouted:
        print()
        print(f"BRANDS WITH NO VENDOR LIST ({len(unrouted)}) -- these parts go to the '*' list,")
        print("or to the admin when '*' is empty. If one is another name for a mapped brand,")
        print('re-run with --brand-alias "THIS NAME=MAPPED NAME".')
        for n, brand in unrouted:
            print(f"  {n:6}  {brand}")
    print()
    print("The parts master (parts / part_aliases) was NOT touched.")
    if not args.apply:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
