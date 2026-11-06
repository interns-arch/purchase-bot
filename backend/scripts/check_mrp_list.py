"""Self-contained check of the monthly MRP list. Runs against a THROWAWAY SQLite
file and never messages anyone or calls the Portal:

    .venv\\Scripts\\python.exe -m backend.scripts.check_mrp_list

Exit code 0 = every check passed."""

from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="mrp-list-check-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["AI_FALLBACK_ENABLED"] = "false"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_ADMIN_PHONE_NUMBER"] = ""
os.environ["GOOGLE_SHEETS_ENABLED"] = "false"
os.environ.pop("MRP_LIST_ENABLED", None)

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def _csv(name: str, rows: list[list[str]]) -> Path:
    path = _TMP / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return path


def main() -> int:
    from sqlalchemy import select

    import backend.app.main  # noqa: F401 -- registers every table
    from backend.app.integrations.dealer_portal import delta
    from backend.app.integrations.whatsapp import mrp_list, whapi
    from backend.app.integrations.whatsapp.models import VendorMrpItem
    from backend.app.notifications import broker
    from backend.app.workers import document_worker as worker
    from backend.app.workers import scheduler
    from core.db import get_session, init_db
    from core.models import Vendor
    from core.services import inventory_import_service as imp

    init_db(force=True)
    mrp_list.now_ist = lambda: datetime(2026, 11, 3, 11, 0)
    mrp_list.now_ist_naive = lambda: datetime(2026, 11, 3, 11, 0)

    print("\n[1] which file is an MRP list")
    check('"MRP_LIST_NOV.xlsx" is named', mrp_list.is_named(None, "MRP_LIST_NOV.xlsx"))
    check('caption "price list" is named', mrp_list.is_named("price list", "a.xlsx"))
    check('"rate-list.csv" is named', mrp_list.is_named(None, "rate-list.csv"))
    check('"Stock with MRP.xlsx" is not', not mrp_list.is_named(None, "Stock with MRP.xlsx"))
    check('"stock_03.xlsx" is not', not mrp_list.is_named(None, "stock_03.xlsx"))
    check('"primrpart.xlsx" is not', not mrp_list.is_named(None, "primrpart.xlsx"))

    print("\n[2] reading the file")
    plain = mrp_list.read_list(_csv("plain.csv", [
        ["Cartrends MRP November"],
        [],
        ["Part No", "Part Description", "MRP"],
        ["AAA111", "Oil filter", "150"],
        ["BBB222", "Air filter", "1,250.50"],
        ["BBB222", "Air filter", "900"],
        ["CCC333", "", "300"],
        ["DDD444", "Bad", "NA"],
        ["", "No part", "99"],
        ["ZZZ999", "Free", "0"],
    ]))
    check("finds the table below a title row", plain is not None and "AAA111" in plain.items)
    check("keeps the higher MRP of a repeated part", plain.items["BBB222"][2] == Decimal("1250.50"))
    check("counts the bad lines", plain.bad_rows == 3)
    check("has no quantity column", not plain.has_quantity_column)
    check("a list with MRP and no quantity counts unnamed", mrp_list.is_list(plain, named=False))
    stock_file = _csv("daily.csv", [["Part No", "Part Description", "Stock Qty", "MRP"], ["AAA111", "Oil", "5", "100"]])
    stock = mrp_list.read_list(stock_file)
    check("our normal stock file does NOT count unnamed", not mrp_list.is_list(stock, named=False))
    check("the same file counts if the vendor names it", mrp_list.is_list(stock, named=True))
    rate_only = mrp_list.read_list(_csv("rates.csv", [["Part No", "Rate"], ["AAA111", "80"]]))
    check("a Rate-only file needs the name", not mrp_list.is_list(rate_only, named=False) and mrp_list.is_list(rate_only, named=True))
    check("no part/price table gives None", mrp_list.read_list(_csv("no.csv", [["Name", "City"], ["A", "B"]])) is None)

    print("\n[3] the Portal price")
    with get_session() as s:
        vendor = Vendor(name="Check Motors", vendor_code="CT_CHECK")
        s.add(vendor)
        s.flush()
        vendor_id = vendor.id
    with get_session() as s:
        imp.run_import(vendor_id, _csv("stock.csv", [
            ["Part No", "Part Description", "Stock Qty", "MRP", "Rate"],
            ["AAA111", "Oil filter", "5", "100", "80"],
            ["BBB222", "Air filter", "3", "200", ""],
            ["CCC333", "Belt", "2", "", ""],
        ]), s)
    account = SimpleNamespace(key="CHECK", vendor_ids=[vendor_id], full_snapshot=False)

    def rows():
        with get_session() as s:
            result = delta.build_delta(account, s)
        return {r.part_no: (r.quantity, r.price) for r in result.rows}, result

    the_list = mrp_list.read_list(_csv("list.csv", [
        ["Part No", "MRP"], ["AAA111", "150"], ["BBB222", "250"], ["CCC333", "300"], ["DDD444", "400"],
    ]))
    with get_session() as s:
        mrp_list.save(vendor_id, the_list, "list.csv", s)

    before, result = rows()
    check("switched off: stock file's MRP is the price, as before", before["BBB222"][1] == Decimal("200"))
    check("switched off: a part with no price is skipped, as before", "CCC333" not in before and result.priceless_count == 1)

    os.environ["MRP_LIST_ENABLED"] = "true"
    after, result = rows()
    check("a part with a Rate keeps it", after["AAA111"] == (5, Decimal("80")))
    check("the list replaces the stock file's MRP", after["BBB222"] == (3, Decimal("250")))
    check("the list fills a part with no price", after["CCC333"] == (2, Decimal("300")) and result.priceless_count == 0)
    check("quantities still come from the stock file", {q for q, _ in after.values()} == {5, 3, 2})
    check("a listed part with no stock is NOT sent", "DDD444" not in after)

    with get_session() as s:
        mrp_list.save(vendor_id, mrp_list.read_list(_csv("l2.csv", [["Part No", "MRP"], ["BBB222", "275"]])), "l2.csv", s)
    after, _ = rows()
    check("next list: newest MRP wins", after["BBB222"][1] == Decimal("275"))
    check("next list: a part left out keeps its MRP", after["CCC333"][1] == Decimal("300"))
    with get_session() as s:
        count = len(s.execute(select(VendorMrpItem).where(VendorMrpItem.vendor_id == vendor_id)).scalars().all())
    check("one saved row per part", count == 4)

    print("\n[4] the file arriving in the worker")
    with get_session() as s:
        other = Vendor(name="Other Motors", vendor_code="CT_OTHER")
        s.add(other)
        s.flush()
        other_id = other.id
    party = SimpleNamespace(party_type="vendor", party_id=other_id, name="Other Motors")
    replies, pushes = [], []
    source = {"path": None}

    def fake_download(_media, filename, _client):
        target = _TMP / f"staged_{len(os.listdir(_TMP))}_{filename}"
        shutil.copy(source["path"], target)
        source["staged"] = target
        return target

    worker.registry.lookup = lambda sender, session: party
    worker.WhatsAppClient = lambda settings: None
    worker.download_document_media = fake_download
    worker.send_reply_safe = lambda to, text: replies.append(text)
    worker.dealer_portal.request_push = lambda vid, name=None: pushes.append(vid)

    def msg(filename, caption=None, photo=False):
        return SimpleNamespace(sender="919800000001", media_id="m", filename=filename, caption=caption,
                               message_id="x", is_photo=photo)

    source["path"] = stock_file
    check("a stock file falls through to stock in the window", worker._mrp_list_file(msg("daily.csv")) is False)
    check("its extra copy is removed", not source["staged"].exists())
    check("nothing pushed for it", pushes == [] and replies == [])

    source["path"] = _TMP / "list.csv"
    check("an unnamed list in the window is taken", worker._mrp_list_file(msg("nov.csv")) is True)
    check("the vendor gets a count", "4 parts" in replies[-1])
    check("the Portal push is requested", pushes == [other_id])
    with get_session() as s:
        check("this month is marked received", other_id in mrp_list.received_vendor_ids("2026-11", s))

    source["path"] = stock_file
    source.pop("staged")
    check("after their list, unnamed files are not even opened", worker._mrp_list_file(msg("daily.csv")) is False and "staged" not in source)

    mrp_list.now_ist = lambda: datetime(2026, 11, 20, 11, 0)
    with get_session() as s:
        third = Vendor(name="Third Motors", vendor_code="CT_THIRD")
        s.add(third)
        s.flush()
        party.party_id = third.id
    check("past the window, unnamed files are not opened", worker._mrp_list_file(msg("daily.csv")) is False and "staged" not in source)
    source["path"] = _TMP / "list.csv"
    check("past the window, a named list is still taken", worker._mrp_list_file(msg("MRP.csv")) is True)
    mrp_list.now_ist = lambda: datetime(2026, 11, 3, 11, 0)

    replies.clear()
    check("a named PDF is not stock", worker._mrp_list_file(msg("MRP list.pdf")) is True)
    check("the vendor is asked for Excel", replies == [mrp_list.SEND_AS_EXCEL])
    replies.clear()
    source["path"] = _csv("nothing.csv", [["Name", "City"], ["A", "B"]])
    check("a named file with no table is not stock", worker._mrp_list_file(msg("MRP.csv")) is True)
    check("the vendor is told why", replies == [mrp_list.NOT_READABLE])

    party.party_type = "customer"
    check("a customer's file is never an MRP list", worker._mrp_list_file(msg("MRP.csv")) is False)
    party.party_type = "vendor"
    os.environ["MRP_LIST_ENABLED"] = "false"
    check("switched off: nothing is taken", worker._mrp_list_file(msg("MRP.csv")) is False)
    os.environ["MRP_LIST_ENABLED"] = "true"

    print("\n[5] the request in the groups")
    posted, warnings, recorded = [], [], []
    whapi.enabled = lambda: True
    whapi.list_groups = lambda: [{"id": g} for g in ("g-check", "g-other", "g-new", "g-bad")]
    whapi.vendor_group_codes = lambda: {
        "g-check": "CT_CHECK", "g-other": "CT_OTHER", "g-new": "CT_NEW", "g-bad": "CT_BAD", "g-absent": "CT_ABSENT",
    }

    def fake_post(group, text):
        if group == "g-bad":
            raise RuntimeError("refused")
        posted.append((group, text))

    whapi.post_as_owner = fake_post
    whapi.record = lambda *a: recorded.append(a)
    broker.publish = lambda *a, **k: warnings.append(a)
    pauses = []
    result = mrp_list.send_requests(sleep=pauses.append)
    check("asked only those who have not sent it", [g for g, _ in posted] == ["g-new"])
    check("the words are the request", posted[0][1] == mrp_list.REQUEST_TEXT)
    check("vendors who sent it this month are skipped", {"g-check", "g-other"} <= set(result["skipped"]))
    check("a group the number is not in is skipped", "g-absent" in result["skipped"])
    check("a failed group does not stop the rest", result["failed"] == ["g-bad"])
    check("there is a pause between groups", len(pauses) == 1)
    check("a failure warns the team", len(warnings) == 1)
    check("it shows in the Groups feed", recorded and recorded[-1][1] == "mrp request")

    mrp_list.now_ist = lambda: datetime(2026, 12, 1, 10, 0)
    posted.clear()
    mrp_list.send_requests(sleep=lambda _: None)
    check("next month everyone is asked again", {g for g, _ in posted} == {"g-check", "g-other", "g-new"})

    whapi.enabled = lambda: False
    posted.clear()
    check("group side off: nothing is sent", mrp_list.send_requests() == {"sent": [], "skipped": [], "failed": []} and not posted)

    print("\n[6] the schedule")
    scheduler._schedule_mrp_requests()
    check("group side off: no job", scheduler._scheduler.get_job("whapi_monthly_mrp_request") is None)
    whapi.enabled = lambda: True
    scheduler._schedule_mrp_requests()
    job = scheduler._scheduler.get_job("whapi_monthly_mrp_request")
    trigger = str(job.trigger) if job else ""
    check("runs on the 1st at 10:00", "day='1'" in trigger and "hour='10'" in trigger and "minute='0'" in trigger)
    scheduler._scheduler.remove_job("whapi_monthly_mrp_request")

    print(f"\n{'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
