"""Dealer Portal Active Procurement -> vendor enquiries -> PO-MST -> Odoo ->
PO PDF to the purchase user on WhatsApp (brief: "Vendor Bot to Dealer Portal
Procurement", 11 Nov 2026).

    config.py   settings (PORTAL_PROCUREMENT_*), off unless enabled
    client.py   the Portal procurement API, with the brief's retry rules
    models.py   demand lines, allocations, and the transition audit log
    service.py  poll -> enquire -> allocate -> knockoff -> approval -> Odoo
    notify.py   the PO PDF and summary to the purchase user

Demand comes ONLY from the Portal (user, 11 Nov 2026: the sales bot never
asks the purchase bot). Vendor questions reuse the advance-order engine:
every Portal line becomes an `AdvanceOrder` with source "dealer_portal"."""
