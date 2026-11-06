"""Check: a bare stock file from STAFF is never filed under the last vendor
they named, and a file sent twice is imported once (29 Oct 2026 -- Seema's
Ford file went under Ark Vehicles).

    .venv\\Scripts\\python.exe -m backend.scripts.check_vendor_grouping
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="grouping-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({"WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1"})
STAFF, VENDOR_NO = "919289015770", "919811111111"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    from core.db import get_session, init_db
    from core.models import InventoryImport, Vendor
    from core.hashing import sha256_of_file
    from core.time_utils import now_ist_naive
    from backend.app.documents.models import DocumentSource, IncomingDocument, IncomingDocumentStatus, IncomingDocumentType
    from backend.app.integrations.whatsapp import pending_vendor_files, registry
    from backend.app.workers import document_worker as w

    init_db(force=True)
    ford = _TMP / "ford.xls"
    ford.write_bytes(b"part,qty\nF5RZ2525A,10\n")
    copy = _TMP / "ford_copy.xls"
    copy.write_bytes(ford.read_bytes())
    toyota = _TMP / "toyota.csv"
    toyota.write_bytes(b"part,qty\n04465WC001,3\n")
    with get_session() as s:
        v = Vendor(name="Ark Automotive", vendor_code="CT_AA")
        s.add(v)
        s.flush()
        registry.register_vendor_number(VENDOR_NO, v.id, s)

    print("\n[1] who may be grouped")
    check("staff (Seema) is not a vendor's own number", not w._is_vendors_own_number(STAFF))
    check("a registered vendor number is", w._is_vendors_own_number(VENDOR_NO))

    print("\n[2] the bare copy held, then the same file comes with its name")
    with get_session() as s:
        pending_vendor_files.add(STAFF, str(copy), "Part Stock Valuation FORD.xls", s)
        pending_vendor_files.add(STAFF, str(toyota), "toyota.csv", s)
    w._drop_held_copies(STAFF, ford)
    with get_session() as s:
        left = [r.original_filename for r in pending_vendor_files.list_for(STAFF, s)]
    check("the held copy is dropped", "Part Stock Valuation FORD.xls" not in left)
    check("a different held file stays", left == ["toyota.csv"])

    print("\n[3] the named file came first, then a bare copy")
    check("nothing imported yet -> not a recent copy", not w._imported_recently(STAFF, copy))
    with get_session() as s:
        imp = InventoryImport(vendor_id=v.id, file_name="ford.xls", file_size_bytes=10, content_hash=sha256_of_file(ford), status="COMPLETED", row_count=1, created_at=now_ist_naive())
        s.add(imp)
        s.flush()
        s.add(IncomingDocument(source=DocumentSource.WHATSAPP, document_type=IncomingDocumentType.VENDOR_INVENTORY, filename="ford.xls", sender=STAFF, status=IncomingDocumentStatus.PROCESSED, inventory_import_id=imp.id))
    check("the bare copy is recognised as already received", w._imported_recently(STAFF, copy))
    check("a different file is not", not w._imported_recently(STAFF, toyota))
    check("the same file from someone else is not", not w._imported_recently("919000000000", copy))

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
