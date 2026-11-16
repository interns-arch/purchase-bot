"""Vendor onboarding and vendor ledger checks over WhatsApp.

- A new vendor texts "new vendor"; the bot collects the Vendor Creation form
  by chat (`conversation`), the approvers decide on WhatsApp (`approvals`),
  and an approved vendor is created in ProcureHub and on the Dealer Portal
  (`service`, `dealer_portal_vendor`).
- A ledger sent with "ledger" is read and its totals checked (`ledger`), the
  accounts team passes or fails it on WhatsApp, and a passed ledger is pushed
  to the Dealer Portal."""
