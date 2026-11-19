"""Vendor Creation tab (12 Nov 2026): every vendor registration filled on
WhatsApp, its full details, and where it stands -- still being filled,
waiting for Prateek sir / NK Jain sir, approved (by whom, when), rejected
(why), and whether the Dealer Portal account was made.

Approving and rejecting stay on WhatsApp, where the approvers decide. The
one action here is RESEND (12 Nov 2026): a request still waiting is sent to
Prateek sir / NK Jain sir again, the same card with Approve / Reject buttons
(or the template + text outside WhatsApp's 24-hour window). At most once per
10 minutes per request, so nobody is flooded. The bank account number is shown masked (last 4
digits) because this page has no login of its own while AUTH_DISABLED is on.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.vendor_onboarding import models as m
from backend.app.vendor_onboarding.questions import QUESTIONS, format_answer
from core.time_utils import ist_isoformat, now_ist_naive

RESEND_GAP = timedelta(minutes=10)

router = APIRouter(
    prefix="/api/vendor-registrations", tags=["vendor-registrations"], dependencies=[Depends(get_current_user)]
)

# What each status means to the purchase team, in plain words.
_STAGE = {
    m.REG_DRAFT: ("FILLING", "Being filled on WhatsApp"),
    m.REG_AWAITING_APPROVAL: ("WAITING", "Waiting for Prateek sir / NK Jain sir"),
    m.REG_CREATED: ("APPROVED", "Approved"),
    m.REG_REJECTED: ("REJECTED", "Rejected"),
    m.REG_CREATE_FAILED: ("FAILED", "Approved, but creating it failed"),
    m.REG_EXPIRED: ("EXPIRED", "Vendor stopped answering"),
    m.REG_CANCELLED: ("CANCELLED", "Cancelled"),
}

_GROUPS = [
    ("Company", ["vendor_name", "gstin", "pan", "vendor_type", "address"]),
    ("Contact", ["contact_person", "mobile", "email"]),
    ("Brands", ["brands_supplied", "brand_discounts"]),
    ("Bank", ["account_holder", "bank_name", "account_number", "ifsc"]),
    ("Terms", ["credit_days", "credit_limit", "dispatch_mode", "freight_terms", "transporter",
               "lead_time_days", "min_order_value", "return_policy"]),
    ("Dealer Portal", ["also_customer"]),
    ("Remarks", ["remarks"]),
]
_LABEL = {q.key: q.label for q in QUESTIONS}


def _mask(account: str | None) -> str | None:
    digits = str(account or "")
    return f"•••• {digits[-4:]}" if len(digits) > 4 else digits or None


def _portal(request: m.VendorRegistrationRequest) -> dict:
    ref = request.dealer_portal_ref or ""
    if request.status == m.REG_CREATE_FAILED:
        return {"state": "FAILED", "text": request.dealer_portal_error or "creation failed"}
    if request.status != m.REG_CREATED:
        return {"state": "NONE", "text": "—"}
    if ref.startswith("DP-EXISTS-"):
        return {"state": "EXISTS", "text": f"Already on the Portal (id {ref[10:]}) — no new account"}
    if ref.startswith("DP-"):
        return {"state": "CREATED", "text": f"Created on the Portal (id {ref[3:]})"}
    if ref.startswith("VEND-"):
        return {"state": "MAILED", "text": f"Sent to data entry by mail ({ref}) — not confirmed"}
    if ref == "DRY-RUN":
        return {"state": "NONE", "text": "Portal was not connected"}
    return {"state": "CREATED" if ref else "NONE", "text": ref or "—"}


def _tez(request: m.VendorRegistrationRequest) -> dict:
    """Was the vendor created on Tez ERP? (Shubham's OK to the message)."""
    status = request.tez_status
    base = {
        "sent_at": ist_isoformat(request.tez_sent_at) if request.tez_sent_at else None,
        "done_at": ist_isoformat(request.tez_done_at) if request.tez_done_at else None,
        "done_by": request.tez_done_by,
    }
    if status == "created":
        return {**base, "state": "CREATED", "text": f"Created on Tez ERP — {request.tez_done_by or 'OK'}"}
    if status == "pending":
        return {**base, "state": "WAITING", "text": "Sent to Shubham — waiting for his OK"}
    if status == "send_failed":
        return {**base, "state": "FAILED", "text": "Could not send the message to Shubham"}
    return {**base, "state": "NONE", "text": "—"}


def _filled_by(request: m.VendorRegistrationRequest) -> str:
    answers = request.answers or {}
    if answers.get("_on_behalf"):
        return f"{answers.get('_created_by') or request.whatsapp_number} (purchase team)"
    return f"Vendor itself ({request.whatsapp_number})"


def _row(request: m.VendorRegistrationRequest, *, details: bool = False) -> dict:
    answers = request.answers or {}
    stage, stage_text = _STAGE.get(request.status, (request.status.upper(), request.status))
    row = {
        "id": request.id,
        "code": f"VR-{request.id}",
        "vendor_name": request.vendor_name or answers.get("vendor_name"),
        "gstin": request.gstin or answers.get("gstin"),
        "mobile": answers.get("mobile"),
        "brands": answers.get("brands_supplied"),
        "also_customer": answers.get("also_customer"),
        "stage": stage,
        "stage_text": stage_text,
        "decided_by": request.decided_by,
        "decided_at": ist_isoformat(request.decided_at) if request.decided_at else None,
        "reason": request.reason,
        "filled_by": _filled_by(request),
        "started_at": ist_isoformat(request.created_at) if request.created_at else None,
        "submitted_at": ist_isoformat(request.submitted_at) if request.submitted_at else None,
        "vendor_id": request.vendor_id,
        "last_resent_at": ist_isoformat(request.reminder_sent_at) if request.reminder_sent_at else None,
        "customer_asked": "also_customer" in answers,
        "portal": _portal(request),
        "tez": _tez(request),
    }
    if details:
        groups = []
        for title, keys in _GROUPS:
            items = []
            for key in keys:
                value = answers.get(key)
                if value in (None, "", []):
                    continue
                text = _mask(value) if key == "account_number" else format_answer(key, value)
                items.append({"label": _LABEL.get(key, key), "value": text})
            if items:
                groups.append({"title": title, "items": items})
        gst = request.gst_data or {}
        row["details"] = groups
        row["gst"] = {
            "verified": bool(gst),
            "legal_name": gst.get("legal_name"),
            "status": gst.get("status"),
            "registration_date": gst.get("registration_date"),
        }
        row["step"] = request.current_step
    return row


@router.get("")
def list_registrations(
    stage: str | None = Query(None, description="FILLING / WAITING / APPROVED / REJECTED / FAILED / EXPIRED / CANCELLED"),
    db: Session = Depends(get_db),
) -> dict:
    rows = db.execute(
        select(m.VendorRegistrationRequest).order_by(m.VendorRegistrationRequest.id.desc()).limit(500)
    ).scalars().all()
    items = [_row(r) for r in rows]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["stage"]] = counts.get(item["stage"], 0) + 1
    if stage:
        items = [i for i in items if i["stage"] == stage.upper()]
    return {"items": items, "counts": counts}


@router.get("/{request_id}")
def get_registration(request_id: int, db: Session = Depends(get_db)) -> dict:
    request = db.get(m.VendorRegistrationRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail=f"No vendor registration VR-{request_id}.")
    return _row(request, details=True)


@router.post("/{request_id}/resend")
def resend_approval(
    request_id: int,
    also_customer: str | None = Body(None, embed=True, description="Haan / Nahi -- for a request filled before that question existed"),
    db: Session = Depends(get_db),
) -> dict:
    """Send a waiting request to the approvers again."""
    from backend.app.vendor_onboarding import approvals
    from backend.app.vendor_onboarding.config import vendor_onboarding_settings as settings

    request = db.get(m.VendorRegistrationRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail=f"No vendor registration VR-{request_id}.")
    if request.status != m.REG_AWAITING_APPROVAL:
        stage = _STAGE.get(request.status, ("", request.status))[1]
        raise HTTPException(status_code=409, detail=f"VR-{request_id} is not waiting for approval ({stage}).")
    if request.reminder_sent_at and now_ist_naive() - request.reminder_sent_at < RESEND_GAP:
        wait = RESEND_GAP - (now_ist_naive() - request.reminder_sent_at)
        raise HTTPException(
            status_code=429,
            detail=f"VR-{request_id} was resent a moment ago. Try again in {max(1, int(wait.total_seconds() // 60) + 1)} min.",
        )
    if not settings.approver_numbers:
        raise HTTPException(status_code=503, detail="No approver numbers are set (VENDOR_APPROVER_NUMBERS).")

    answers = dict(request.answers or {})
    if also_customer is not None:
        value = also_customer.strip().lower()
        if value not in ("haan", "nahi"):
            raise HTTPException(status_code=422, detail="also_customer must be Haan or Nahi.")
        answers["also_customer"] = "Haan" if value == "haan" else "Nahi"
        request.answers = answers
    elif "also_customer" not in answers:
        raise HTTPException(
            status_code=422,
            detail=f"VR-{request_id} was filled before the 'also add as customer' question: choose Haan or Nahi first.",
        )

    request.reminder_sent_at = now_ist_naive()
    db.flush()
    approvals.request_vendor_approval(request, db, reminder=True)
    names = ", ".join(settings.name_of(n) for n in settings.approver_numbers)
    return {"ok": True, "sent_to": names, "row": _row(request)}


@router.post("/{request_id}/tez-send")
def send_to_tez(request_id: int, db: Session = Depends(get_db)) -> dict:
    """Send (or resend) the vendor's details to Shubham as "Create vendor on
    Tez ERP" -- for a vendor created before this existed, or a failed send."""
    from backend.app.vendor_onboarding import tez

    request = db.get(m.VendorRegistrationRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail=f"No vendor registration VR-{request_id}.")
    if request.status != m.REG_CREATED:
        raise HTTPException(status_code=409, detail=f"VR-{request_id} is not created yet, so there is nothing to create on Tez ERP.")
    if not tez.enabled():
        raise HTTPException(status_code=503, detail="No Tez ERP number is set (VENDOR_TEZ_NUMBERS).")
    if request.tez_status == tez.TEZ_CREATED:
        raise HTTPException(status_code=409, detail=f"VR-{request_id} is already marked created on Tez ERP.")
    ok = tez.send_for(request, db, force=True)
    return {"ok": ok, "row": _row(request)}
