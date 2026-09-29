"""Load the Founder's VENDOR BRAND MAPPING.xlsx into advance-order routing.

    python -m backend.scripts.import_vendor_brand_mapping "VENDOR BRAND MAPPING.xlsx"
    python -m backend.scripts.import_vendor_brand_mapping "VENDOR BRAND MAPPING.xlsx" --apply

DRY RUN BY DEFAULT. Without --apply nothing is written: the script reads the
sheet, matches every vendor name against the vendors ProcureHub already
knows, and prints what WOULD happen -- which vendors match, which would be
created, which discounts had to be normalised, which phone numbers clash.
Read that report first.

WHAT --apply WRITES
-------------------
  vendors                  only for a name that matches no existing vendor,
                           onboarded through the SAME path a WhatsApp upload
                           uses (`dispatcher._resolve_or_onboard_vendor`), so
                           it gets a permanent vendor code the usual way
  vendor_brands            one row per vendor+brand, with the standing terms
  advance_vendor_contacts  the phone numbers to ask on

WHAT IT NEVER TOUCHES
---------------------
  whatsapp_registered_numbers -- on purpose. A registered vendor number gets
  the 09:30 "please share your stock" template every morning and counts as
  pending in the 11:00 summary. 104 of the sheet's 111 rows say the vendor
  will never share stock; registering them would message those vendors daily.
  See `models.AdvanceVendorContact` for the full reasoning.

Re-running is safe: rows are matched on (brand, vendor) and (vendor, number),
so a second run updates the terms rather than duplicating anything.

EXPECTED COLUMNS (matched by header text, case-insensitive, in any order)
  VENDOR NAME, BRAND, DISCOUNT, PHONE NO (one or more), TRANSPORTATION,
  PAYMENT TERMS, CAN SHARE STOCK
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

# Same reason as migrate_schema_updates.py: target the database the app uses.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import backend.app.advance_orders.models  # noqa: E402,F401 -- registers the tables
import backend.app.integrations.whatsapp.models  # noqa: E402,F401
from backend.app.advance_orders import models as m  # noqa: E402
from backend.app.integrations.whatsapp.config import whatsapp_settings  # noqa: E402
from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber  # noqa: E402
from backend.app.integrations.whatsapp.registry import normalize_number  # noqa: E402
from core.db import engine, get_session, init_db  # noqa: E402
from core.models import Vendor  # noqa: E402
from core.services import vendor_service  # noqa: E402
from sqlalchemy import select  # noqa: E402

SOURCE_LABEL = "VENDOR BRAND MAPPING.xlsx"


# ------------------------------------------------------------------ parsing
@dataclass
class Terms:
    discount_type: str
    discount_pct: Decimal | None
    note: str | None
    # A human-readable account of anything the parser changed, for the report.
    conversion: str | None = None


def parse_discount(raw) -> Terms:
    """The DISCOUNT cell, turned into terms. Nothing is guessed silently:
    every conversion is reported, and anything that is not a plain number or
    the word "rate" is kept verbatim as a note for a human."""
    if raw is None or str(raw).strip() == "":
        return Terms(m.DISC_RATE, None, None, "blank -- treated as rate (the vendor will be asked)")

    text = str(raw).strip()
    lowered = text.lower()

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        value = None

    if value is not None:
        if value < 0:
            return Terms(m.DISC_OTHER, None, text, f"negative discount {text} -- kept as a note, not used")
        # The sheet mixes two ways of writing a percentage: 21 means 21%, and
        # 0.07 also means 7% (AADI AUTO PARTS / FORD, Fair Brother / LUK).
        # Read literally, 0.07 would be a 0.07% discount and rank last forever.
        if 0 < value < 1:
            pct = (value * 100).quantize(Decimal("0.001"))
            return Terms(m.DISC_PERCENT, pct, None, f"{text} read as a fraction -> {format(pct.normalize(), "f")}%")
        if value > 100:
            return Terms(m.DISC_OTHER, None, text, f"{text} is over 100% -- kept as a note, not used")
        return Terms(m.DISC_PERCENT, value, None, None)

    if lowered == "rate":
        return Terms(m.DISC_RATE, None, None, None)
    # "rate + scheme", "1000 discount", anything else: no formula can carry it.
    return Terms(m.DISC_OTHER, None, text, f"'{text}' kept verbatim -- the vendor will be asked for a rate")


def parse_yes_no(raw) -> bool | None:
    text = str(raw or "").strip().upper()
    if text in {"YES", "Y", "TRUE", "1"}:
        return True
    if text in {"NO", "N", "FALSE", "0"}:
        return False
    return None


def clean_phone(raw) -> str:
    """Excel stores 9829197173 as 9829197173.0 -- strip that before normalising."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".")[0]
    return normalize_number(text)


def clean_text(raw) -> str | None:
    text = " ".join(str(raw or "").split())
    return text or None


@dataclass
class Row:
    sheet_row: int
    vendor_name: str
    brand: str
    terms: Terms
    phones: list[str]
    transport: str | None
    payment_terms: str | None
    can_share_stock: bool | None


def read_sheet(path: Path) -> list[Row]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    sheet = workbook.worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise SystemExit(f"{path}: the first sheet is empty.")

    header = [" ".join(str(h or "").split()).upper() for h in rows[0]]

    def col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            raise SystemExit(f"{path}: no '{name}' column. Found: {[h for h in header if h]}") from None

    i_vendor, i_brand, i_discount = col("VENDOR NAME"), col("BRAND"), col("DISCOUNT")
    phone_cols = [i for i, h in enumerate(header) if h.startswith("PHONE")]
    optional = {name: (header.index(name) if name in header else None)
                for name in ("TRANSPORTATION", "PAYMENT TERMS", "CAN SHARE STOCK")}

    def cell(values, index):
        return values[index] if index is not None and index < len(values) else None

    out: list[Row] = []
    for number, values in enumerate(rows[1:], start=2):
        vendor_name = clean_text(cell(values, i_vendor))
        brand = clean_text(cell(values, i_brand))
        if not vendor_name or not brand:
            continue
        phones: list[str] = []
        for index in phone_cols:
            phone = clean_phone(cell(values, index))
            if phone and phone not in phones:
                phones.append(phone)
        out.append(
            Row(
                sheet_row=number,
                vendor_name=vendor_name,
                brand=brand.upper(),
                terms=parse_discount(cell(values, i_discount)),
                phones=phones,
                transport=clean_text(cell(values, optional["TRANSPORTATION"])),
                payment_terms=clean_text(cell(values, optional["PAYMENT TERMS"])),
                can_share_stock=parse_yes_no(cell(values, optional["CAN SHARE STOCK"])),
            )
        )
    return out


# ------------------------------------------------------------------ planning
@dataclass
class Report:
    rows: int = 0
    vendors_matched: dict[str, str] = field(default_factory=dict)  # sheet name -> existing name
    vendors_new: list[str] = field(default_factory=list)
    brands: set[str] = field(default_factory=set)
    conversions: list[str] = field(default_factory=list)
    phone_notes: list[str] = field(default_factory=list)
    same_phone_different_names: list[str] = field(default_factory=list)
    duplicate_pairs: list[str] = field(default_factory=list)
    vendor_brands_written: int = 0
    contacts_written: int = 0


def priority_key(row: Row) -> tuple:
    """Who is asked FIRST for a brand. With fan-out 3 and nine Maruti vendors,
    this decides which three hear about a part first -- so the best known
    discount goes first, then rate vendors (terms unknown until they answer),
    then the sheet's own order."""
    if row.terms.discount_type == m.DISC_PERCENT and row.terms.discount_pct is not None:
        return (0, -row.terms.discount_pct, row.sheet_row)
    return (1, Decimal(0), row.sheet_row)


def plan(rows: list[Row], session) -> tuple[Report, dict[str, Vendor | None]]:
    report = Report(rows=len(rows))
    resolved: dict[str, Vendor | None] = {}
    for row in rows:
        if row.vendor_name in resolved:
            continue
        resolved[row.vendor_name] = vendor_service.get_vendor_by_name(row.vendor_name, session)

    for name, vendor in resolved.items():
        if vendor is None:
            report.vendors_new.append(name)
        else:
            report.vendors_matched[name] = vendor.name

    admins = {normalize_number(a) for a in whatsapp_settings.admin_phone_numbers}
    registered = {
        n.whatsapp_number: n.vendor_id
        for n in session.execute(select(WhatsAppRegisteredNumber)).scalars()
    }

    names_by_phone: dict[str, set[str]] = {}
    seen_pairs: dict[tuple[str, str], int] = {}
    for row in rows:
        report.brands.add(row.brand)
        if row.terms.conversion:
            report.conversions.append(
                f"row {row.sheet_row}: {row.vendor_name} / {row.brand}: {row.terms.conversion}"
            )
        pair = (row.vendor_name.lower(), row.brand)
        if pair in seen_pairs:
            report.duplicate_pairs.append(
                f"row {row.sheet_row} repeats row {seen_pairs[pair]}: {row.vendor_name} / {row.brand} "
                "(the later row's terms win; phone numbers from both are kept)"
            )
        else:
            seen_pairs[pair] = row.sheet_row
        if not row.phones:
            report.phone_notes.append(f"row {row.sheet_row}: {row.vendor_name} has no phone number -- cannot be asked")
        for phone in row.phones:
            names_by_phone.setdefault(phone, set()).add(row.vendor_name)
            if phone in admins:
                report.phone_notes.append(
                    f"row {row.sheet_row}: {row.vendor_name}: {phone} is an ADMIN number -- skipped"
                )
            vendor = resolved.get(row.vendor_name)
            owner = registered.get(phone)
            if owner is not None and (vendor is None or owner != vendor.id):
                owner_vendor = session.get(Vendor, owner)
                report.phone_notes.append(
                    f"row {row.sheet_row}: {row.vendor_name}: {phone} is already the registered number of "
                    f"'{owner_vendor.name if owner_vendor else owner}' -- stored as an enquiry contact anyway; check it"
                )

    for phone, names in sorted(names_by_phone.items()):
        if len(names) > 1:
            report.same_phone_different_names.append(f"{phone}: {' | '.join(sorted(names))}")
    return report, resolved


# ------------------------------------------------------------------ writing
def apply(rows: list[Row], resolved: dict[str, Vendor | None], report: Report, session) -> None:
    from backend.app.services.document_processor.dispatcher import _resolve_or_onboard_vendor

    vendors: dict[str, Vendor] = {}
    for name, existing in resolved.items():
        if existing is not None:
            vendors[name] = existing
        else:
            vendor, _message = _resolve_or_onboard_vendor(name, session)
            vendors[name] = vendor

    admins = {normalize_number(a) for a in whatsapp_settings.admin_phone_numbers}

    by_brand: dict[str, list[Row]] = {}
    for row in rows:
        by_brand.setdefault(row.brand, []).append(row)

    for brand, brand_rows in by_brand.items():
        # One row per vendor per brand; a repeated pair keeps the LATER terms.
        latest: dict[int, Row] = {}
        for row in brand_rows:
            latest[vendors[row.vendor_name].id] = row
        ordered = sorted(latest.items(), key=lambda item: priority_key(item[1]))

        for position, (vendor_id, row) in enumerate(ordered, start=1):
            vb = session.execute(
                select(m.VendorBrand).where(m.VendorBrand.brand == brand, m.VendorBrand.vendor_id == vendor_id)
            ).scalar_one_or_none()
            if vb is None:
                vb = m.VendorBrand(brand=brand, vendor_id=vendor_id)
                session.add(vb)
            vb.priority = position
            vb.active = True
            vb.discount_type = row.terms.discount_type
            vb.discount_pct = row.terms.discount_pct
            vb.discount_note = row.terms.note
            vb.transport = row.transport
            vb.payment_terms = row.payment_terms
            vb.can_share_stock = row.can_share_stock
            report.vendor_brands_written += 1

    for row in rows:
        vendor = vendors[row.vendor_name]
        for phone in row.phones:
            if phone in admins:
                continue
            contact = session.execute(
                select(m.AdvanceVendorContact).where(
                    m.AdvanceVendorContact.vendor_id == vendor.id,
                    m.AdvanceVendorContact.whatsapp_number == phone,
                )
            ).scalar_one_or_none()
            if contact is None:
                session.add(m.AdvanceVendorContact(vendor_id=vendor.id, whatsapp_number=phone, source=SOURCE_LABEL))
                session.flush()
                report.contacts_written += 1
            elif not contact.active:
                contact.active = True
                report.contacts_written += 1
    session.flush()


# ------------------------------------------------------------------ output
def print_report(report: Report, *, applied: bool) -> None:
    line = "-" * 78
    print(line)
    print("APPLIED -- written to the database" if applied else "DRY RUN -- nothing was written")
    print(line)
    print(f"Sheet rows read:        {report.rows}")
    print(f"Brands:                 {len(report.brands)}")
    print(f"Vendors matched:        {len(report.vendors_matched)}")
    print(f"Vendors to be created:  {len(report.vendors_new)}")
    if applied:
        print(f"vendor_brands written:  {report.vendor_brands_written}")
        print(f"enquiry contacts added: {report.contacts_written}")

    def section(title: str, items) -> None:
        items = list(items)
        if not items:
            return
        print()
        print(f"{title} ({len(items)})")
        for item in items:
            print(f"  {item}")

    renamed = [f"{sheet!r} -> existing {actual!r}" for sheet, actual in report.vendors_matched.items() if sheet != actual]
    section("MATCHED UNDER A DIFFERENT SPELLING -- check these are the same company", renamed)
    section("NEW VENDORS -- would be onboarded with a fresh vendor code", sorted(report.vendors_new))
    section("DISCOUNTS NORMALISED", report.conversions)
    section(
        "SAME PHONE, DIFFERENT VENDOR NAMES -- a reply from one of these cannot be credited to the "
        "right vendor while two of them are being asked; such replies go to the admin",
        report.same_phone_different_names,
    )
    section("REPEATED VENDOR + BRAND ROWS", report.duplicate_pairs)
    section("PHONE NUMBER NOTES", report.phone_notes)
    print()
    print("The WhatsApp number registry was NOT touched -- no vendor starts getting")
    print("the 09:30 stock request because of this import.")
    if not applied:
        print("Re-run with --apply to write.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import VENDOR BRAND MAPPING.xlsx into advance-order routing.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
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

    init_db()
    rows = read_sheet(args.path)
    with get_session() as session:
        report, resolved = plan(rows, session)
        if args.apply:
            apply(rows, resolved, report, session)
        else:
            session.rollback()
    print_report(report, applied=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
