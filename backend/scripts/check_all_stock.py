"""Check: the ALL STOCK tab -- Bijwashan, Honda, Jaipur first, then vendors
in the order they sent stock, only since the last 20:00 clear (29 Oct 2026).

    .venv\\Scripts\\python.exe -m backend.scripts.check_all_stock
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="allstock-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({"GOOGLE_SHEETS_ALL_STOCK_ENABLED": "true", "WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1"})

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    from core.db import get_session, init_db
    from core.models import InventoryImport, Vendor, VendorInventory
    from backend.app.integrations.google_sheets import dealer_stock as ds

    init_db(force=True)
    now = datetime(2026, 10, 29, 17, 0)

    def stock(session, code, name, at, parts, active=True):
        v = session.query(Vendor).filter_by(vendor_code=code).one_or_none()
        if v is None:
            v = Vendor(name=name, vendor_code=code)
            session.add(v)
            session.flush()
        imp = InventoryImport(vendor_id=v.id, file_name=f"{code}.xlsx", file_size_bytes=1, content_hash=f"{code}{at}",
                              status="COMPLETED", row_count=len(parts), is_active=active, created_at=at)
        session.add(imp)
        session.flush()
        for i, part in enumerate(parts, 1):
            session.add(VendorInventory(vendor_id=v.id, import_id=imp.id, row_number=i, vendor_part_number=part,
                                        normalized_part_number=part, quantity_available=Decimal(2), raw_data={}))

    with get_session() as s:
        # arrival order today: Northend, Jaipur, Mohan, Bijwashan, Honda
        stock(s, "CT_ND", "Northend Distributors", datetime(2026, 10, 29, 10, 49), ["ND1", "ND2"])
        stock(s, "CT_JS", "JAIPUR STOCK", datetime(2026, 10, 29, 10, 50), ["JS1"])
        stock(s, "CT_MSP", "Mohan Spares", datetime(2026, 10, 29, 10, 44), ["MSP1"])
        stock(s, "CT_BS", "BIJWASHAN STOCK", datetime(2026, 10, 29, 11, 0), ["BS1", "BS2"])
        stock(s, "CT_HS", "honda stock", datetime(2026, 10, 29, 11, 5), ["HS1"])
        # before last night's 20:00 clear: not shown
        stock(s, "CT_EAA", "Ess Aay Automotive", datetime(2026, 10, 28, 19, 59), ["EAA1"])
        # an old, no longer active import: not shown
        stock(s, "CT_AVP", "Ark Vehicles", datetime(2026, 10, 29, 9, 0), ["OLD"], active=False)

    print("\n[1] order")
    with get_session() as s:
        headers, table = ds.all_stock_table(s, now)
    codes = [row[0] for row in table]
    check("headers", headers[:3] == ["Vendor Code", "Vendor", "PartNo"])
    check("Bijwashan, Honda, Jaipur first, then vendors by arrival",
          codes == ["CT_BS", "CT_BS", "CT_HS", "CT_JS", "CT_MSP", "CT_ND", "CT_ND"])
    check("each vendor's rows stay in file order", [r[2] for r in table if r[0] == "CT_BS"] == ["BS1", "BS2"])

    print("\n[2] cleared at 20:00")
    check("last clear before 20:00 is yesterday 20:00", ds.last_clear(now) == datetime(2026, 10, 28, 20, 0))
    check("last clear after 20:00 is today 20:00", ds.last_clear(datetime(2026, 10, 29, 20, 5)) == datetime(2026, 10, 29, 20, 0))
    check("stock from before the clear is left out", "CT_EAA" not in codes)
    check("an inactive import is left out", "OLD" not in [r[2] for r in table])
    with get_session() as s:
        _, after = ds.all_stock_table(s, datetime(2026, 10, 29, 20, 1))
    check("right after 20:00 the tab is empty", after == [])
    with get_session() as s:
        s.query(InventoryImport).filter(InventoryImport.vendor_id == s.query(Vendor).filter_by(vendor_code="CT_ND").one().id).update({"is_active": False})
    with get_session() as s:
        stock(s, "CT_ND", "Northend Distributors", datetime(2026, 10, 29, 21, 0), ["ND9"])
    with get_session() as s:
        _, late = ds.all_stock_table(s, datetime(2026, 10, 29, 21, 30))
    check("a file after the clear shows up again", [r[2] for r in late] == ["ND9"])

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
