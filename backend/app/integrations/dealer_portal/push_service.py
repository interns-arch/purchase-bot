"""Push a vendor stock snapshot into Dealer Portal. The public entry point.

    from backend.app.integrations import dealer_portal
    dealer_portal.request_push(vendor_id)

SAFETY -- THE WHOLE POINT OF THIS MODULE
----------------------------------------
This runs inside `_process_staged_file()`'s post-commit block in
`document_worker.py`, alongside `run_topup_for_vendor()` and
`request_consolidated_send()`. That block runs AFTER the import transaction
has committed, in its own session, best-effort, and is documented as "never
fails the import". This module holds up its end of that:

  1. `DEALER_PORTAL_ENABLED` defaults to FALSE. Off, `request_push()` returns
     on its first line: no query, no audit row, no network call. ProcureHub
     behaves exactly as it does today.
  2. `DEALER_PORTAL_SHADOW` defaults to TRUE. On, the full delta is computed
     and a `DealerPortalPush` row with status=SHADOW is written recording
     exactly what WOULD be sent -- and no byte reaches DP. Run this against
     real vendor uploads for several days and read the output first.
  3. Everything is wrapped. A DP outage, timeout, auth failure, bad config
     or programming error is logged and swallowed. It can never fail an
     import, block allocation, or affect a WhatsApp reply.
  4. Its own session. The import's transaction is already committed and is
     never joined.

Pushes for one account are SERIALISED by an in-process lock: two vendor
files from the same group (Bijvasan then Bijwasan) arriving together would
otherwise compute two deltas concurrently against the same DP account.
Serialising means the second push sees the first one's state. Both are safe
to run -- DP's absolute-replace semantics make a repeat harmless -- but the
order must not interleave.
"""

from __future__ import annotations

import threading
from collections import defaultdict

from backend.app.integrations.dealer_portal import client, credentials, csv_builder, delta
from backend.app.integrations.dealer_portal.config import dealer_portal_settings
from backend.app.integrations.dealer_portal.credentials import DealerPortalAccount
from core.db import get_session
from core.logging_setup import get_logger
from core.models import DealerPortalPush, DealerPortalPushStatus
from core.time_utils import utcnow_naive

logger = get_logger(__name__)

_account_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _lock_for(account_key: str) -> threading.Lock:
    with _locks_guard:
        return _account_locks[account_key]


def request_push(vendor_id: int | None, vendor_name: str | None = None) -> None:
    """Push this vendor's group to Dealer Portal. Best-effort: NEVER raises.

    Must be called AFTER the import's transaction has committed, so the new
    snapshot is visible."""
    try:
        if not dealer_portal_settings.enabled:
            return
        if vendor_id is None:
            return
        _push_for_vendor(vendor_id, vendor_name)
    except Exception:  # noqa: BLE001 -- an output must never affect the import
        logger.exception(
            "Dealer Portal push failed for vendor %s (%s). The import is unaffected.",
            vendor_id,
            vendor_name,
        )


def _push_for_vendor(vendor_id: int, vendor_name: str | None) -> None:
    with get_session() as session:
        account = credentials.account_for_vendor(vendor_id, session)

    if account is None:
        logger.info(
            "Dealer Portal: vendor %s (%s) is not mapped to any account -- nothing "
            "pushed. Add it to a DEALER_PORTAL_<KEY>_VENDORS list to enable it.",
            vendor_id,
            vendor_name,
        )
        return

    with _lock_for(account.key):
        push_account(account, triggering_vendor_id=vendor_id)


def push_account(
    account: DealerPortalAccount,
    *,
    triggering_vendor_id: int | None = None,
    force_full_snapshot: bool = False,
) -> DealerPortalPushStatus:
    """Compute and push one account's delta. Writes exactly one
    `DealerPortalPush` audit row. Returns the status recorded.

    `force_full_snapshot` is used by `dealer_portal_resync.py` to zero
    sold-out parts for an account whose flag is normally off."""
    if force_full_snapshot and not account.full_snapshot:
        logger.warning(
            "Dealer Portal %s: forcing a FULL SNAPSHOT push (zeroing sold-out "
            "parts) even though DEALER_PORTAL_%s_FULL_SNAPSHOT is false.",
            account.key,
            account.key,
        )
        account.full_snapshot = True

    with get_session() as session:
        if not account.vendor_ids:
            credentials.resolve_account_vendors(account, session)
        result = delta.build_delta(account, session)

        push = DealerPortalPush(
            account_key=account.key,
            vendor_id=triggering_vendor_id,
            import_id=result.import_ids[0] if result.import_ids else None,
            vendor_ids=list(result.vendor_ids),
            import_ids=list(result.import_ids),
            rows_sent=result.rows_sent,
            zeroed_count=result.zeroed_count,
            status=DealerPortalPushStatus.PENDING,
        )
        session.add(push)
        session.flush()
        push_id = push.id

    csv_bytes = csv_builder.build_csv(result.rows, tat_days=account.tat_days)

    logger.info(
        "Dealer Portal %s: %d row(s) to send (%d zeroed, %d sold-out but withheld, "
        "%d duplicate(s) collapsed, %d without a known price) across vendor(s) %s.",
        account.key,
        result.rows_sent,
        result.zeroed_count,
        result.withheld_zero_count,
        result.duplicate_count,
        result.priceless_count,
        ", ".join(account.vendor_names) or "-",
    )

    if not csv_bytes:
        return _finish(
            push_id,
            DealerPortalPushStatus.SUCCESS,
            error=None,
            note="nothing to send",
        )

    # ---- SHADOW MODE: everything above, nothing over the wire. ----------
    if dealer_portal_settings.shadow:
        logger.info(
            "Dealer Portal %s [SHADOW] would POST %d byte(s) to %s/stock/"
            "upload-stock-csv:\n%s",
            account.key,
            len(csv_bytes),
            dealer_portal_settings.base_url,
            csv_builder.preview(result.rows, tat_days=account.tat_days),
        )
        return _finish(push_id, DealerPortalPushStatus.SHADOW, error=None)

    # ---- LIVE ----------------------------------------------------------
    try:
        upload = client.upload_csv(
            account, csv_bytes, file_name=f"{account.key.lower()}_stock.csv"
        )
    except client.DealerPortalError as exc:
        logger.error("Dealer Portal %s: push failed -- %s", account.key, exc)
        return _finish(push_id, DealerPortalPushStatus.FAILED, error=str(exc))

    if upload.failed_count:
        # A SIGNAL, not noise: these are parts missing from DP's parts
        # master -- exactly the "new SKU we should add" report.
        logger.warning(
            "Dealer Portal %s: DP REJECTED %d of %d row(s) -- those part numbers "
            "are not in DP's parts master. batch_id=%s",
            account.key,
            upload.failed_count,
            upload.total_rows,
            upload.batch_id,
        )

    logger.info(
        "Dealer Portal %s: pushed OK. batch_id=%s total=%d inserted=%d updated=%d "
        "failed=%d",
        account.key,
        upload.batch_id,
        upload.total_rows,
        upload.inserted_count,
        upload.updated_count,
        upload.failed_count,
    )
    return _finish(push_id, DealerPortalPushStatus.SUCCESS, error=None, upload=upload)


def _finish(
    push_id: int,
    status: DealerPortalPushStatus,
    *,
    error: str | None,
    upload=None,
    note: str | None = None,
) -> DealerPortalPushStatus:
    """Close out the audit row. Never raises -- a bookkeeping failure must
    not turn into an import failure."""
    try:
        with get_session() as session:
            push = session.get(DealerPortalPush, push_id)
            if push is None:
                return status
            push.status = status
            push.error = error
            push.attempts = (push.attempts or 0) + 1
            push.completed_at = utcnow_naive()
            if upload is not None:
                push.batch_id = upload.batch_id
                push.inserted_count = upload.inserted_count
                push.updated_count = upload.updated_count
                push.failed_count = upload.failed_count
    except Exception:  # noqa: BLE001
        logger.exception("Could not record Dealer Portal push %s.", push_id)
    if note:
        logger.info("Dealer Portal push %s: %s.", push_id, note)
    return status


def retry_failed_pushes() -> int:
    """Re-run every push left in FAILED, newest account state. Called by the
    scheduler. Returns how many were retried.

    Safe to repeat: DP's absolute-replace semantics mean re-sending a CSV
    cannot double-count, so a retry is never destructive."""
    if not dealer_portal_settings.enabled:
        return 0

    max_attempts = dealer_portal_settings.retry_max_attempts
    with get_session() as session:
        failed = (
            session.query(DealerPortalPush)
            .filter(DealerPortalPush.status == DealerPortalPushStatus.FAILED)
            .filter(DealerPortalPush.attempts < max_attempts)
            .order_by(DealerPortalPush.created_at.asc())
            .all()
        )
        pending = [(row.id, row.account_key, row.vendor_id) for row in failed]

    if not pending:
        return 0

    # One retry per ACCOUNT: a fresh delta covers every failed push for it.
    seen: set[str] = set()
    retried = 0
    for push_id, account_key, vendor_id in pending:
        if account_key in seen:
            _mark_superseded(push_id)
            continue
        seen.add(account_key)
        try:
            with get_session() as session:
                account = credentials.account_by_key(account_key, session)
            if account is None or not account.enabled:
                logger.info(
                    "Dealer Portal retry: account %s is no longer configured -- "
                    "leaving push %s as FAILED.",
                    account_key,
                    push_id,
                )
                continue
            with _lock_for(account.key):
                status = push_account(account, triggering_vendor_id=vendor_id)
            if status in (
                DealerPortalPushStatus.SUCCESS,
                DealerPortalPushStatus.SHADOW,
            ):
                _mark_superseded(push_id)
            else:
                _bump_attempts(push_id)
            retried += 1
        except Exception:  # noqa: BLE001 -- a background sweep never raises
            logger.exception("Dealer Portal retry failed for account %s.", account_key)
            _bump_attempts(push_id)
    return retried


def _mark_superseded(push_id: int) -> None:
    """A later push covered this account, so the old failure is resolved."""
    try:
        with get_session() as session:
            push = session.get(DealerPortalPush, push_id)
            if push is not None and push.status == DealerPortalPushStatus.FAILED:
                push.status = DealerPortalPushStatus.SUCCESS
                push.error = (push.error or "") + " [resolved by a later push]"
                push.completed_at = utcnow_naive()
    except Exception:  # noqa: BLE001
        logger.exception("Could not close Dealer Portal push %s.", push_id)


def _bump_attempts(push_id: int) -> None:
    try:
        with get_session() as session:
            push = session.get(DealerPortalPush, push_id)
            if push is not None:
                push.attempts = (push.attempts or 0) + 1
    except Exception:  # noqa: BLE001
        logger.exception("Could not bump attempts for Dealer Portal push %s.", push_id)
