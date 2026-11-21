"""Portal procurement settings, read from `backend/.env`. Same tiny-class
idiom as the other integrations.

The service account is a DEDICATED Dealer Portal login with only
purchase:view and purchase:create (brief). It is never a vendor's dealer
login and never a person's password. Secrets live in backend/.env (or the
file named by PORTAL_PROCUREMENT_SECRETS_FILE) and are never logged."""

from __future__ import annotations

import json
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _flag(name: str, default: str = "false") -> bool:
    return (os.environ.get(name) or default).strip().lower() == "true"


SOURCE = "dealer_portal"  # AdvanceOrder.source for Portal demand


class PortalProcurementSettings:
    # Master switch. Off: no poll, no vendor question, no Portal write.
    enabled: bool = _flag("PORTAL_PROCUREMENT_ENABLED")

    # The service account. Either here, or in a JSON file
    # {"username": ..., "password": ...} named by PORTAL_PROCUREMENT_SECRETS_FILE
    # (an AWS Secrets Manager export, a docker secret, ...).
    username: str = (os.environ.get("PORTAL_PROCUREMENT_USERNAME") or "").strip()
    password: str = (os.environ.get("PORTAL_PROCUREMENT_PASSWORD") or "").strip()
    secrets_file: str = (os.environ.get("PORTAL_PROCUREMENT_SECRETS_FILE") or "").strip()
    device_id: str = (os.environ.get("PORTAL_PROCUREMENT_DEVICE_ID") or "procurehub-vendor-bot-prod").strip()
    device_info: str = "ProcureHub AWS vendor bot"

    # PILOT (default) vs LIVE (user, 12 Nov 2026: "do not start the full live
    # campaign yet"). In pilot the Portal is read and stored, but ONLY the
    # source lines in PORTAL_PROCUREMENT_PILOT_LINES go to vendors, and ONLY
    # the bot vendors in PORTAL_PROCUREMENT_PILOT_VENDORS are asked. No pilot
    # lines = nobody is asked at all. PORTAL_PROCUREMENT_MODE=live lifts both.
    mode: str = (os.environ.get("PORTAL_PROCUREMENT_MODE") or "pilot").strip().lower()
    pilot_lines: set[int] = {
        int(x) for x in (os.environ.get("PORTAL_PROCUREMENT_PILOT_LINES") or "").replace(" ", "").split(",")
        if x.isdigit()
    }
    pilot_vendors: set[int] = {
        int(x) for x in (os.environ.get("PORTAL_PROCUREMENT_PILOT_VENDORS") or "").replace(" ", "").split(",")
        if x.isdigit()
    }

    # The pilot can also be set WITHOUT a restart, in a small JSON file
    # (re-read on every run): {"lines": [13998], "parts": ["6N2914A132AF"],
    # "vendors": [59]}. A Portal line is a pilot line when its id OR its part
    # number is listed -- so a fresh advance order for a known part number is
    # picked up as soon as the Portal shows it. The file wins over the env.
    pilot_file: str = (os.environ.get("PORTAL_PROCUREMENT_PILOT_FILE") or "uploads/portal_pilot.json").strip()

    def live(self) -> bool:
        return self.mode == "live"

    def pilot(self) -> dict:
        """{"lines": set[int], "parts": set[str], "brands": set[str], "vendors": set[int], "skip_lines": set[int]}
        for this run. "brands" (16 Nov 2026): every line of these brands, in
        the bot's own brand names ("FORD", "MARUTI SUZUKI")."""
        lines, parts, brands, vendors = set(self.pilot_lines), set(), set(), set(self.pilot_vendors)
        skip: set[int] = set()
        try:
            with open(self.pilot_file, encoding="utf-8") as fh:
                data = json.load(fh) or {}
            lines = {int(x) for x in data.get("lines") or [] if str(x).strip().isdigit()}
            parts = {"".join(ch for ch in str(x).upper() if ch.isalnum()) for x in data.get("parts") or [] if x}
            vendors = {int(x) for x in data.get("vendors") or [] if str(x).strip().isdigit()}
            # "skip_lines" (16 Nov 2026): Portal lines never asked, even when
            # their part or brand is in the pilot (an older real order).
            skip = {int(x) for x in data.get("skip_lines") or [] if str(x).strip().isdigit()}
            from backend.app.advance_orders.brands import canonical_brand

            brands = {canonical_brand(str(x)) for x in data.get("brands") or [] if str(x).strip()} - {None}
        except (OSError, ValueError):
            pass
        return {"lines": lines, "parts": parts, "brands": brands, "vendors": vendors, "skip_lines": skip}

    # A line is sent to vendors only while the Portal still shows it: seen in
    # the last N hours (the Portal is read every 10 min). Older "discovered"
    # rows are lines that left the 7-day Active Procurement list (21 Nov 2026:
    # 3 pilot lines from 12-13 Nov would have gone out on the first live round).
    fresh_hours: int = max(1, _int("PORTAL_PROCUREMENT_FRESH_HOURS", 6))
    # Right before a vendor question / reminder is sent, Active Procurement is
    # read again (one read covers all sends for this many seconds).
    recheck_seconds: int = max(0, _int("PORTAL_PROCUREMENT_RECHECK_SECONDS", 120))

    # BATCH rule (User, 21 Nov 2026). Parts collected over a round's 3 hours go
    # out together to every vendor of their brand; vendors get ONE 3-hour
    # window (clock time) with one reminder halfway. At the end: parts with a
    # good answer -> winner -> PO-MST; the rest -> ONE brand-wise message to
    # the purchase team, and vendors are not asked about that batch again.
    # Late replies are kept for the vendor record only.
    batch: bool = _flag("PORTAL_PROCUREMENT_BATCH", "true")
    batch_reply_minutes: int = max(30, _int("PORTAL_PROCUREMENT_BATCH_REPLY_MINUTES", 180))
    batch_reminder_minutes: int = max(0, _int("PORTAL_PROCUREMENT_BATCH_REMINDER_MINUTES", 90))
    # Rajkumar, Alam sir, Prateek sir.
    batch_escalation_numbers: list[str] = [
        n.strip() for n in (os.environ.get("PORTAL_PROCUREMENT_BATCH_ESCALATION_NUMBERS")
                            or "919217396458,919217030408,919999492550").split(",") if n.strip()
    ]

    def allowed_vendors(self) -> set[int]:
        """LIVE mode's vendor allow-list (user, 21 Nov 2026: "remove pilot mode
        but restrict to limited vendors"): `"allowed_vendors": [72, 110, 145]`
        in the same pilot file, re-read every run. Set -> in live mode only
        these vendors are asked, and only Portal parts of THEIR brands are
        taken (other parts wait untouched). Empty -> every vendor."""
        try:
            with open(self.pilot_file, encoding="utf-8") as fh:
                data = json.load(fh) or {}
            return {int(x) for x in data.get("allowed_vendors") or [] if str(x).strip().isdigit()}
        except (OSError, ValueError):
            return set()

    def skip_lines(self) -> set[int]:
        """Portal lines never asked, in pilot AND live mode."""
        return set(self.pilot()["skip_lines"])

    # How often the Active Procurement list is read, and how often the
    # allocation steps (allocate, knockoff, approval, Odoo, PDF) move on.
    poll_minutes: int = max(5, _int("PORTAL_PROCUREMENT_POLL_MINUTES", 10))

    # ROUNDS (User, 16 Nov 2026): the open lines go to the vendors once every
    # `round_hours` inside vendor hours (09:30, 12:30, 15:30, 18:30). A vendor
    # is asked a part at most `max_asks` times (the first ask + reminders one
    # round apart); silent after that -> Prateek sir is told and that vendor
    # is not asked about it again. Only a reply of at most `max_tat_days`
    # with the minimum margin can win; otherwise the bot keeps asking, and
    # when nobody is left to ask Prateek sir chooses (or cancels).
    rounds: bool = (os.environ.get("PORTAL_PROCUREMENT_ROUNDS") or "true").strip().lower() == "true"
    round_hours: int = max(1, _int("PORTAL_PROCUREMENT_ROUND_HOURS", 3))
    max_asks: int = max(1, _int("PORTAL_PROCUREMENT_MAX_ASKS", 3))
    max_tat_days: int = max(1, _int("PORTAL_PROCUREMENT_MAX_TAT_DAYS", 7))
    # Round times (User, 16 Nov 2026): first at 10:00, last at 19:00, every
    # `round_hours` between -> 10:00, 13:00, 16:00, 19:00.
    round_start: str = (os.environ.get("PORTAL_PROCUREMENT_ROUND_START") or "10:00").strip()
    round_end: str = (os.environ.get("PORTAL_PROCUREMENT_ROUND_END") or "19:00").strip()
    # A vendor whose stock sheet from TODAY shows the part is not asked if he
    # has it: the sheet is his answer -- available, MRP (else the Portal's),
    # discount from the sheet price, days from a days/TAT column. A discount or
    # days the sheet does not give is asked -- only that (User, 16 Nov 2026).
    # PORTAL_PROCUREMENT_SHEET_TAT_DAYS set = use it instead of asking days.
    sheet_tat_days: int | None = (
        max(0, _int("PORTAL_PROCUREMENT_SHEET_TAT_DAYS", 0))
        if (os.environ.get("PORTAL_PROCUREMENT_SHEET_TAT_DAYS") or "").strip() else None
    )
    sheet_today_only: bool = (os.environ.get("PORTAL_PROCUREMENT_SHEET_TODAY_ONLY") or "true").strip().lower() == "true"
    step_minutes: int = max(1, _int("PORTAL_PROCUREMENT_STEP_MINUTES", 5))
    # Brief: the last seven calendar days (today minus six, IST).
    days: int = max(1, _int("PORTAL_PROCUREMENT_DAYS", 7))
    page_size: int = max(1, min(5000, _int("PORTAL_PROCUREMENT_PAGE_SIZE", 5000)))
    include_interbranch: bool = _flag("PORTAL_PROCUREMENT_INCLUDE_INTERBRANCH")

    # Vendor safety: at most this many NEW demand lines go to vendors per poll,
    # and at most this many parts in one vendor message. Lines over the cap
    # wait for the next poll -- vendors are never flooded (a bulk send got
    # Rajkumar's number banned, 2 Nov 2026).
    max_new_lines_per_poll: int = max(1, _int("PORTAL_PROCUREMENT_MAX_NEW_LINES", 25))
    max_lines_per_enquiry: int = max(1, _int("PORTAL_PROCUREMENT_LINES_PER_ENQUIRY", 10))

    # User, 14 Nov 2026: keep at least this margin, in % of MRP, over what the
    # customer pays. A vendor below it cannot win; none above it -> the line
    # is held and the team decides. 0 = off.
    min_margin_percent: float = float(os.environ.get("PORTAL_PROCUREMENT_MIN_MARGIN_PERCENT") or 2)

    # Who gets the PO PDF + summary. Blank = the admin numbers and purchase team.
    notify_numbers: list[str] = [
        x.strip() for x in (os.environ.get("PORTAL_PROCUREMENT_NOTIFY_NUMBERS") or "").split(",") if x.strip()
    ]
    # Operations alerts (stopped runs, 409s, onboarding needed). Blank = same as above.
    alert_numbers: list[str] = [
        x.strip() for x in (os.environ.get("PORTAL_PROCUREMENT_ALERT_NUMBERS") or "").split(",") if x.strip()
    ]

    timeout_seconds: float = float(os.environ.get("PORTAL_PROCUREMENT_TIMEOUT_SECONDS") or 60)
    get_attempts: int = max(1, _int("PORTAL_PROCUREMENT_GET_ATTEMPTS", 4))
    # A step that keeps failing goes to the dead-letter state after this many tries.
    max_step_attempts: int = max(1, _int("PORTAL_PROCUREMENT_MAX_STEP_ATTEMPTS", 5))
    # PDF sanity: anything smaller is not a real PO.
    min_pdf_bytes: int = max(100, _int("PORTAL_PROCUREMENT_MIN_PDF_BYTES", 1000))

    # ---- 19 Nov 2026 procurement meeting ---------------------------------
    # A vendor whose brand discount is already known (set on the Dealer Portal
    # for this line, else a standing % in vendor_brands) is NOT asked for a
    # discount; first-time vendors with none on file are asked.
    use_known_discount: bool = (os.environ.get("PORTAL_PROCUREMENT_USE_KNOWN_DISCOUNT") or "true").strip().lower() == "true"
    known_discount_ttl_hours: int = max(1, _int("PORTAL_PROCUREMENT_KNOWN_DISCOUNT_TTL_HOURS", 12))
    # Prateek sir's own vendor order, first = most preferred, inside the same
    # speed band. Re-read on every run, like the pilot file:
    # {"vendors": [145, 110, 72]}. Not listed = after the listed ones.
    priority_file: str = (os.environ.get("PORTAL_PROCUREMENT_PRIORITY_FILE") or "uploads/portal_priority.json").strip()
    # No answer good enough this many hours after the first ask -> Prateek
    # sir is told, with "ask this vendor instead" (a new vendor) / cancel.
    escalate_hours: int = max(1, _int("PORTAL_PROCUREMENT_ESCALATE_HOURS", 6))
    # A vendor quoting less than the customer's discount -> Prateek sir is told at once.
    low_discount_alert: bool = (os.environ.get("PORTAL_PROCUREMENT_LOW_DISCOUNT_ALERT") or "true").strip().lower() == "true"
    # (Parts as a file to vendors: the enquiry CSV of 18 Nov 2026 does that --
    # advance_orders/enquiry_csv.py, ADVANCE_ORDER_ASK_CSV.)
    # End-of-day purchase report to Prateek sir (IST, HH:MM; blank = off).
    report_time: str = (os.environ.get("PORTAL_PROCUREMENT_REPORT_TIME") or "20:00").strip()
    report_excel: bool = (os.environ.get("PORTAL_PROCUREMENT_REPORT_EXCEL") or "true").strip().lower() == "true"

    def priority_vendors(self) -> list[int]:
        """Vendor ids, most preferred first (empty = no list)."""
        try:
            with open(self.priority_file, encoding="utf-8") as fh:
                data = json.load(fh) or {}
            return [int(x) for x in data.get("vendors") or [] if str(x).strip().isdigit()]
        except (OSError, ValueError, AttributeError):
            return []

    def credentials(self) -> tuple[str, str]:
        """(username, password); empty strings when not configured."""
        if self.secrets_file:
            try:
                with open(self.secrets_file, encoding="utf-8") as fh:
                    data = json.load(fh) or {}
                return str(data.get("username") or "").strip(), str(data.get("password") or "").strip()
            except (OSError, ValueError):
                return "", ""
        return self.username, self.password

    def configured(self) -> bool:
        user, password = self.credentials()
        return bool(user and password)


settings = PortalProcurementSettings()
