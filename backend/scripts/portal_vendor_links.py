"""Import bot-vendor -> Portal-dealer links from the confirmation Excel.

DRY RUN by default -- nothing is saved (every write is rolled back):

    python -m backend.scripts.portal_vendor_links --file links.xlsx [--accept-exact] [--json]
    python -m backend.scripts.portal_vendor_links --file links.xlsx [--accept-exact] --commit --by "name"

Before planning, the Portal dealer table is refreshed READ-ONLY from the
Portal's own vendor options for every line on Active Procurement (last 7 days).
Rules: see backend/app/portal_procurement/links.py. Stock uploads
(dealer_portal_vendor_map) are read, never written."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--accept-exact", action="store_true",
                    help="accept LIKELY rows whose names are identical (unlinked vendors only)")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--by", default="", help="who decided (kept on every link)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--priority", default="1416,4727,29,24,8091",
                    help="Portal dealer ids to list first in the report")
    args = ap.parse_args()

    import backend.app.main  # noqa: F401 -- registers every table
    from backend.app.portal_procurement import client as pc, links, service
    from core.db import SessionLocal, init_db
    from core.time_utils import now_ist_naive

    init_db()
    session = SessionLocal()
    try:
        client = pc.PortalClient()
        today = now_ist_naive().date()
        date_from = (today - timedelta(days=6)).isoformat()
        offset, lines = 0, []
        while True:
            body = client.get("/procurement/active", {
                "date_from": date_from, "date_to": today.isoformat(), "include_po_mst": "false",
                "include_received": "false", "include_interbranch": "false", "limit": 500, "offset": offset,
                "sort_by": "order_date", "sort_dir": "asc"})
            rows = service._rows(body)
            lines += rows
            offset += len(rows)
            if service._last_page(rows, service._total(body), offset):
                break
        for row in lines:
            sol = row.get("source_order_line_id")
            if sol is not None:
                links.refresh_dealers(session, service._rows(client.vendors_for_line(int(sol))), row.get("brand"))
        client.close()

        steps = links.plan(links.read_sheet(args.file), session, accept_exact=args.accept_exact)
        priority = [int(x) for x in args.priority.split(",") if x.strip().isdigit()]
        order = {pid: i for i, pid in enumerate(priority)}
        steps.sort(key=lambda s: (order.get(s.proposed, 99), s.action != links.LINK, s.row))
        counts: dict[str, int] = {}
        for s in steps:
            counts[s.action] = counts.get(s.action, 0) + 1
        if args.json:
            print(json.dumps({"counts": counts, "steps": [s.__dict__ for s in steps]}, default=str))
        else:
            print(f"{'row':>4} {'vendor':>6} {'old':>6} {'new':>6}  action                 vendor name -> reason")
            for s in steps:
                print(f"{s.row:>4} {s.vendor_id or '-':>6} {s.old_dealer or '-':>6} {s.proposed or '-':>6}  "
                      f"{s.action:<22} {s.vendor} -> {s.reason}")
            print("counts:", counts)
        if args.commit:
            if not args.by.strip():
                print("refused: --commit needs --by <who decided>")
                session.rollback()
                return 2
            done = links.apply(steps, session, source=Path(args.file).name, decided_by=args.by.strip())
            session.commit()
            print("COMMITTED:", done)
        else:
            session.rollback()
            print("DRY RUN -- nothing saved.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
