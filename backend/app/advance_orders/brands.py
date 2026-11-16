"""Which brand a part belongs to, for routing advance-order questions.

Advance orders mostly arrive WITHOUT a brand (the sales bot sends one only
sometimes), and the part master has none. Found live 9 Oct 2026: every order
went to the catch-all "*" list -- empty -- so no vendor was ever asked.

Three sources, best first (used by `service._brand_for_line`):
  1. `part_brand_hints`, rebuilt from the Brand column of the stock files
     (Bijwashan / Jaipur / Honda ClosingStock: 13,000+ parts) by
     `refresh_hints_from_stock` -- nightly, and on demand;
  2. a part-number PATTERN: Maruti Suzuki numbers are five digits + "M"
     (16510M68K10, 45201M76T01, 28300M79MA1) -- distinctive enough to trust;
  3. otherwise "*".

Every brand name is mapped onto the spelling the VENDOR BRAND MAPPING uses
(`canonical_brand`), so "MARUTI" / "MARUTI  ACCESSORIES" / "GM" find the right
vendors."""

from __future__ import annotations

import re

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from core.logging_setup import get_logger

logger = get_logger(__name__)

# Spellings seen in stock files and from the sales bot -> the mapping's name.
_ALIASES = {
    "MARUTI": "MARUTI SUZUKI",
    "MARUTI GENUINE": "MARUTI SUZUKI",
    "MSIL": "MARUTI SUZUKI",
    "MARUTI SUZUKI GENUINE": "MARUTI SUZUKI",
    "MARUTI ACCESSORIES": "MARUTI SUZUKI ACCESSORIES",
    "MARUTI GENUINE ACCESSORIES": "MARUTI SUZUKI ACCESSORIES",
    "MGA": "MARUTI SUZUKI ACCESSORIES",
    "GM": "GENERAL MOTORS",
    "CHEVROLET": "GENERAL MOTORS",
    "VW": "VOLKSWAGEN",
    "M&M": "MAHINDRA",
    "MAHINDRA & MAHINDRA": "MAHINDRA",
    "MAHINDRA AND MAHINDRA": "MAHINDRA",
    "HYUNDAI MOBIS": "HYUNDAI",
    "MOBIS": "HYUNDAI",
    "TATA MOTORS": "TATA",
    "TOYOTA KIRLOSKAR": "TOYOTA",
    "UNOMINDA": "UNO MINDA",
}

_MARUTI_PART = re.compile(r"^\d{5}M[0-9A-Z]{4,7}$")


def canonical_brand(value: str | None) -> str | None:
    """'maruti  accessories' -> 'MARUTI SUZUKI ACCESSORIES'; None for blank."""
    name = " ".join(str(value or "").upper().split())
    if not name or name in {"LOCAL", "GENERAL", "-", "NA", "N/A"}:
        return None
    return _ALIASES.get(name, name)


def brand_from_pattern(normalised_part: str) -> str | None:
    if _MARUTI_PART.match(normalised_part or ""):
        return "MARUTI SUZUKI"
    return None


def refresh_hints_from_stock(session: Session) -> int:
    """Rebuild part -> brand hints from every stock row that carries a
    Brand / Make column. Newest file wins when a part appears twice.
    Returns how many hints were written."""
    from backend.app.advance_orders.models import PartBrandHint
    from core.ingestion.column_detector import normalise_part_number

    rows = session.execute(
        text(
            """
            select vi.vendor_part_number,
                   coalesce(vi.raw_data::jsonb->>'Brand', vi.raw_data::jsonb->>'BRAND',
                            vi.raw_data::jsonb->>'brand', vi.raw_data::jsonb->>'Make') as brand
            from vendor_inventory vi
            join inventory_imports i on i.id = vi.import_id
            where coalesce(vi.raw_data::jsonb->>'Brand', vi.raw_data::jsonb->>'BRAND',
                           vi.raw_data::jsonb->>'brand', vi.raw_data::jsonb->>'Make') is not null
            order by i.created_at
            """
        )
        if session.bind.dialect.name == "postgresql"
        else text("select '' as vendor_part_number, '' as brand where 1=0")
    ).all()
    latest: dict[str, str] = {}
    for part_number, brand in rows:
        key = normalise_part_number(part_number)
        name = canonical_brand(brand)
        if key and name:
            latest[key] = name
    existing = {h.part_number: h for h in session.execute(select(PartBrandHint)).scalars()}
    written = 0
    for key, name in latest.items():
        hint = existing.get(key)
        if hint is None:
            session.add(PartBrandHint(part_number=key, brand=name, source="stock files"))
            written += 1
        elif hint.brand != name and (hint.source or "") == "stock files":
            hint.brand = name
            written += 1
    session.flush()
    logger.info("Part brand hints refreshed from stock files: %d part(s) known, %d written.", len(latest), written)
    return written
