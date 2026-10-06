"""Vendor onboarding / ledger settings, read from the environment
(`backend/.env`). Same tiny-class idiom as `advance_orders.config`."""

from __future__ import annotations

import os

from backend.app.integrations.whatsapp.registry import normalize_number


def _labelled(name: str, *, fallback: str | None = None) -> dict[str, str]:
    """'Prateek sir:9198xxxxxxx, NK Jain sir:9197xxxxxxx' -> {number: label}.
    A bare number is labelled with itself. When `name` is unset, the
    `fallback` variable's numbers are used (the admin numbers), so nothing is
    ever sent to nobody."""
    result: dict[str, str] = {}
    raw_value = os.environ.get(name) or (os.environ.get(fallback) if fallback else "") or ""
    for part in raw_value.split(","):
        label, _, raw = part.rpartition(":")
        number = normalize_number(raw)
        if number:
            result[number] = label.strip() or number
    return result


def _numbers(name: str) -> list[str]:
    return list(_labelled(name))


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


class VendorOnboardingSettings:
    # MASTER SWITCH. Off: "new vendor" / "ledger" are not claimed and the
    # rest of the bot behaves exactly as before.
    enabled: bool = os.environ.get("VENDOR_ONBOARDING_ENABLED", "false").strip().lower() == "true"

    # "Name:number" lists, comma-separated (a bare number works too).
    # Prateek sir, NK Jain sir -- every request goes to all; first reply wins.
    approvers: dict[str, str] = _labelled("VENDOR_APPROVER_NUMBERS", fallback="WHATSAPP_ADMIN_PHONE_NUMBER")
    approver_numbers: list[str] = list(approvers)
    # Sunny sir, Anurag -- every ledger goes to all; first reply wins.
    ledger_checkers: dict[str, str] = _labelled("LEDGER_CHECKER_NUMBERS", fallback="WHATSAPP_ADMIN_PHONE_NUMBER")
    ledger_checker_numbers: list[str] = list(ledger_checkers)
    # Told about every failed ledger (Prateek sir).
    ledger_escalation_numbers: list[str] = list(_labelled("LEDGER_ESCALATION_NUMBERS", fallback="WHATSAPP_ADMIN_PHONE_NUMBER"))

    # Purchase team: may register a vendor ON BEHALF of that vendor (the vendor's
    # own mobile is then asked for, and registered, instead of the sender's).
    # Approvers are always included. A vendor created by an APPROVER needs no
    # approval -- it is created straight away.
    purchase_team: dict[str, str] = {**_labelled("VENDOR_PURCHASE_TEAM_NUMBERS"), **approvers}

    # Who gets the nightly "what the assistant learned" report and may switch
    # lessons off (in addition to the admin numbers).
    learning_report_numbers: list[str] = _numbers("LEARNING_REPORT_NUMBERS")

    def name_of(self, number: str) -> str:
        return (
            self.approvers.get(number)
            or self.purchase_team.get(number)
            or self.ledger_checkers.get(number)
            or number
        )

    def honorific(self, number: str) -> str | None:
        """How to address a known person: 'Prateek sir', 'Seema ji', 'Yash ji'.
        None when this number is not on any of the lists."""
        label = self.name_of(number)
        if not label or label == number:
            return None
        return label if label.lower().endswith((" sir", " madam", " ji")) else f"{label} ji"

    def role_of(self, number: str) -> str | None:
        if number in self.approvers:
            return "CarTrends approver (purchase head)"
        if number in self.purchase_team:
            return "CarTrends purchase team"
        if number in self.ledger_checkers:
            return "CarTrends accounts team"
        return None

    def is_purchase_team(self, number: str) -> bool:
        return number in self.purchase_team

    def is_approver(self, number: str) -> bool:
        return number in self.approvers

    # Meta-approved templates for people outside WhatsApp's 24-hour window.
    # Body params: {{1}} request id, {{2}} one-line summary. Blank = plain text
    # only (fine while the approver has messaged the bot in the last 24 h).
    approval_template: str = (os.environ.get("VENDOR_APPROVAL_TEMPLATE") or "").strip()
    ledger_template: str = (os.environ.get("LEDGER_CHECK_TEMPLATE") or "").strip()
    template_language: str = (os.environ.get("VENDOR_ONBOARDING_TEMPLATE_LANGUAGE") or "en").strip()

    # A half-filled form is dropped after this many hours of silence.
    draft_expiry_hours: int = max(1, _int("VENDOR_DRAFT_EXPIRY_HOURS", 48))

    # GST lookup (gstinapi.in). Blank key = offline checksum only.
    gst_api_url: str = (os.environ.get("GST_API_URL") or "https://gstinapi.in/v1/gstin").rstrip("/")
    gst_api_key: str = (os.environ.get("GST_API_KEY") or "").strip()

    # Choice lists (comma-separated) -- set them to the Google Form's options.
    vendor_types: list[str] = [
        part.strip()
        for part in (os.environ.get("VENDOR_TYPES") or "Distributor,Dealer,Manufacturer,Wholesaler,Retailer").split(",")
        if part.strip()
    ]
    dispatch_modes: list[str] = [
        part.strip()
        for part in (os.environ.get("VENDOR_DISPATCH_MODES") or "Transport,Courier,Self pickup,Vendor delivery").split(",")
        if part.strip()
    ]
    freight_terms: list[str] = [
        part.strip()
        for part in (os.environ.get("VENDOR_FREIGHT_TERMS") or "Paid by vendor,Paid by us,To pay").split(",")
        if part.strip()
    ]


vendor_onboarding_settings = VendorOnboardingSettings()
