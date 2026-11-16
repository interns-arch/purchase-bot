"""Put an ALREADY APPROVED vendor registration on the Dealer Portal with the
direct API (portal_vendor_api) -- for requests whose creation mail nobody
read after the data-entry robot was switched off on 7 Nov 2026
(VR-19 OM SAI RAM ENTERPRISES).

    # show what would be sent, change nothing:
    python -m backend.scripts.push_vendor_to_portal 19 --customer nahi
    # really create it (still skipped if the mobile/GSTIN is already on the Portal):
    python -m backend.scripts.push_vendor_to_portal 19 --customer nahi --go

--customer is the form's "also add as customer" answer (haan / nahi); a
request that already has one keeps it unless --customer is given.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request_id", type=int)
    parser.add_argument("--customer", choices=["haan", "nahi"])
    parser.add_argument("--go", action="store_true")
    args = parser.parse_args()

    from core.db import get_session
    from backend.app.vendor_onboarding import dealer_portal_vendor, portal_vendor_api as api
    from backend.app.vendor_onboarding import models as m

    with get_session() as session:
        request = session.get(m.VendorRegistrationRequest, args.request_id)
        if request is None:
            print(f"No VR-{args.request_id}.")
            return 1
        ref = request.dealer_portal_ref or ""
        print(f"VR-{request.id} {request.vendor_name} status={request.status} ref={ref or '-'}")
        retryable = request.status == m.REG_CREATE_FAILED or (
            request.status == m.REG_CREATED and (ref.startswith("VEND-") or ref == "DRY-RUN" or not ref)
        )
        if not retryable:
            print("Not a request waiting for the Portal (already DP-... or not approved). Nothing done.")
            return 1
        answers = dict(request.answers or {})
        if args.customer:
            answers["also_customer"] = "Haan" if args.customer == "haan" else "Nahi"
        if "also_customer" not in answers:
            print("The form has no 'also add as customer' answer -- pass --customer haan|nahi.")
            return 1
        body = api.vendor_body(answers, request.gst_data, "<username>", "<password>")
        print("would send:", {k: v for k, v in body.items() if k != "password"})
        if not args.go:
            print("Dry run -- add --go to create it.")
            return 0
        if not api.settings.enabled:
            print("DEALER_PORTAL_ADMIN_USERNAME / _PASSWORD are not set in backend/.env.")
            return 1
        result = dealer_portal_vendor._via_api(answers, gst=request.gst_data)  # noqa: SLF001
        if not result.ok:
            request.dealer_portal_error = result.error
            session.commit()
            print("FAILED:", result.error)
            return 1
        request.answers = answers
        request.dealer_portal_ref = result.reference
        request.dealer_portal_error = None
        request.status = m.REG_CREATED
        session.commit()
        print(result.note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
