"""The Vendor Creation form as an ordered list of chat questions. This one
table drives the whole conversation: order, wording, validation, optional
fields, choice lists and pre-filled suggestions.

GSTIN is asked FIRST on purpose: the GST registry then supplies the company
name, PAN and address, so the vendor only confirms them with "ok"."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from backend.app.vendor_onboarding import validators as v
from backend.app.vendor_onboarding.config import vendor_onboarding_settings

Validator = Callable[[str, dict], tuple[object | None, str | None]]
Suggester = Callable[[dict, dict], str | None]


@dataclass(frozen=True)
class Question:
    key: str
    label: str  # shown in the summary
    prompt: str
    validate: Validator
    optional: bool = False
    choices: Callable[[], list[str]] | None = None
    # (answers, context) -> a suggested answer the vendor accepts with "ok".
    # context: {"gst": GST registry dict or None, "number": sender's mobile}.
    suggest: Suggester | None = None
    hint: str = ""
    extra: dict = field(default_factory=dict)


def _choice_validator(choices: Callable[[], list[str]]) -> Validator:
    def validate(value: str, _answers: dict):
        options = choices()
        text = (value or "").strip()
        if text.isdigit() and 1 <= int(text) <= len(options):
            return options[int(text) - 1], None
        for option in options:
            if option.lower() == text.lower():
                return option, None
        return None, "Please reply with one of the numbers shown."

    return validate


def _gst_name(_answers: dict, ctx: dict) -> str | None:
    gst = ctx.get("gst") or {}
    return gst.get("trade_name") or gst.get("legal_name")


def _gst_address(_answers: dict, ctx: dict) -> str | None:
    gst = ctx.get("gst") or {}
    address, pincode = gst.get("address"), gst.get("pincode")
    if address and pincode and pincode not in address:
        address = f"{address} - {pincode}"
    return address


def _mobile_from_number(_answers: dict, ctx: dict) -> str | None:
    value, _error = v.mobile(ctx.get("number") or "")
    return value


QUESTIONS: list[Question] = [
    Question(
        "gstin", "GSTIN", "What is your GSTIN? (15 characters, e.g. 07AALFN8339A1Z8)",
        lambda value, _a: v.gstin(value),
    ),
    Question(
        "vendor_name", "Vendor / company name", "Your company name?",
        lambda value, _a: v.text(value), suggest=_gst_name,
    ),
    Question(
        "pan", "PAN", "Your PAN number?",
        lambda value, answers: v.pan(value, gstin_value=answers.get("gstin")),
        suggest=lambda answers, _ctx: (answers.get("gstin") or "")[2:12] or None,
    ),
    Question(
        "vendor_type", "Vendor type", "What type of vendor are you?",
        _choice_validator(lambda: vendor_onboarding_settings.vendor_types),
        choices=lambda: vendor_onboarding_settings.vendor_types,
    ),
    Question(
        "brands_supplied", "Brands supplied",
        "Which brands do you supply? (e.g. Maruti, Hyundai, Bosch)",
        lambda value, _a: v.text(value),
    ),
    Question(
        "contact_person", "Contact person", "Name of the contact person?",
        lambda value, _a: v.text(value),
    ),
    Question(
        "mobile", "Mobile", "Contact mobile number?",
        lambda value, _a: v.mobile(value), suggest=_mobile_from_number,
    ),
    Question(
        "email", "Email", "Email address?",
        lambda value, _a: v.email(value), optional=True,
    ),
    Question(
        "address", "Address", "Full business address?",
        lambda value, _a: v.text(value, min_length=10), suggest=_gst_address,
    ),
    Question(
        "account_holder", "Account holder name", "Bank account holder name?",
        lambda value, _a: v.text(value),
        suggest=lambda answers, _ctx: answers.get("vendor_name"),
    ),
    Question(
        "bank_name", "Bank name", "Bank name?",
        lambda value, _a: v.text(value),
    ),
    Question(
        "account_number", "Account number", "Bank account number?",
        lambda value, _a: v.account_number(value),
    ),
    Question(
        "ifsc", "IFSC code", "IFSC code of the branch?",
        lambda value, _a: v.ifsc(value),
    ),
    Question(
        "brand_discounts", "Brand-wise discount",
        "Brand-wise discount you give us. One brand per line, e.g.\nMaruti - 12\nHyundai - 10",
        lambda value, _a: v.brand_discounts(value), optional=True,
    ),
    Question(
        "credit_days", "Credit period (days)", "Credit period you give us, in days? (e.g. 30)",
        lambda value, _a: v.days(value),
    ),
    Question(
        "credit_limit", "Credit limit (₹)", "Credit limit you extend to us, in ₹?",
        lambda value, _a: v.money(value), optional=True,
    ),
    Question(
        "dispatch_mode", "Dispatch mode", "How do you dispatch goods?",
        _choice_validator(lambda: vendor_onboarding_settings.dispatch_modes),
        choices=lambda: vendor_onboarding_settings.dispatch_modes,
    ),
    Question(
        "freight_terms", "Freight terms", "Freight terms?",
        _choice_validator(lambda: vendor_onboarding_settings.freight_terms),
        choices=lambda: vendor_onboarding_settings.freight_terms,
    ),
    Question(
        "transporter", "Preferred transporter", "Preferred transporter?",
        lambda value, _a: v.text(value), optional=True,
    ),
    Question(
        "lead_time_days", "Lead time (days)", "Usual lead time, in days?",
        lambda value, _a: v.days(value), optional=True,
    ),
    Question(
        "min_order_value", "Minimum order value (₹)", "Minimum order value, in ₹?",
        lambda value, _a: v.money(value), optional=True,
    ),
    Question(
        "return_policy", "Return / replacement policy", "Your return / replacement policy?",
        lambda value, _a: v.text(value), optional=True,
    ),
    Question(
        "remarks", "Remarks", "Any remarks?",
        lambda value, _a: v.text(value, min_length=1), optional=True,
    ),
]

BY_KEY: dict[str, Question] = {question.key: question for question in QUESTIONS}


def index_of(key: str) -> int:
    return next(i for i, question in enumerate(QUESTIONS) if question.key == key)


def format_answer(key: str, value) -> str:
    if value in (None, "", []):
        return "—"
    if key == "brand_discounts":
        return ", ".join(f"{row['brand']} {row['discount_pct']}%" for row in value)
    return str(value)
