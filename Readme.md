# Vendor Inventory & Order Fulfillment Automation

## Quick Start (Web App)

The web app is two separate long-running processes -- **you need two
terminals open at the same time**, both left running while you use the app:

- **Terminal 1** runs the backend (FastAPI/uvicorn).
- **Terminal 2** runs the frontend (Vite dev server).

They're independent processes talking over HTTP (frontend calls
`http://127.0.0.1:8000`), not one launching the other, so "run backend then
frontend" really means "start backend, leave it running, then in a *second*
terminal start frontend."

**Important:** every backend command below must be run from the **project
root** (`D:\Downloads\pythonscript`), not from inside `backend\`. Running
`python -m backend.scripts.create_admin` while your terminal's current
directory is `backend\` fails with
`ModuleNotFoundError: No module named 'backend'` -- Python resolves
`-m backend.x.y` relative to the current directory, and there's no
`backend\backend\` folder. `cd ..` back to the project root first (or start
a fresh terminal there).

### Terminal 1 -- Backend

```powershell
cd D:\Downloads\pythonscript          # project root, NOT backend\
venv\Scripts\activate                 # if not already active
pip install -r backend\requirements.txt

# First time only -- creates the admin account:
python -m backend.scripts.create_admin --username admin --password <choose one, 8+ chars>

# Start the API and leave this terminal running:
python -m uvicorn backend.app.main:app --reload --port 8000
```

Leave this window open. `--reload` restarts the server automatically when
you edit backend code, but the process itself must stay running.

### Terminal 2 -- Frontend

Open a **new** terminal window/tab (don't close Terminal 1):

```powershell
cd D:\Downloads\pythonscript\frontend
npm install                           # first time only
copy .env.example .env                # first time only -- points at http://127.0.0.1:8000
npm run dev
```

Then open the URL it prints (default `http://localhost:5173`) and log in
with the admin account you created above.

### Everyday use after first-time setup

You only need the install/`create_admin` steps once. After that, each time
you want to use the app:

```powershell
# Terminal 1
cd D:\Downloads\pythonscript
venv\Scripts\activate
python -m uvicorn backend.app.main:app --reload --port 8000

# Terminal 2
cd D:\Downloads\pythonscript\frontend
npm run dev
```

To reset the admin password later (from the project root, backend does
*not* need to be running for this one):
`python -m backend.scripts.create_admin --username admin --reset`

---

## Overview

This project aims to automate Prateek's inventory sourcing and order fulfillment process. Currently, multiple vendors share their inventory in Excel/CSV files, which are manually consolidated into a master stock list. Customer orders are then manually split across vendors, and fulfillment is tracked manually.

The objective of this project is to eliminate manual work by automating inventory consolidation, vendor allocation, purchase order generation, fulfillment tracking, gap analysis, and vendor performance monitoring.

---

# Current Business Workflow

## Step 1: Vendor Inventory Collection

Multiple vendors send their inventory files (Excel/CSV) containing available stock.

Example:

```
Vendor A Inventory.xlsx
Vendor B Inventory.xlsx
Vendor C Inventory.xlsx
```

Each file contains information such as:

- Part Number
- Quantity
- Price
- Brand
- MRP
- Other product details

---

## Step 2: Master Inventory Creation

All vendor inventories are combined into a single master inventory.

This master inventory is shared with customers as Prateek's available stock.

At this stage, every product still belongs to its original vendor, although this information is maintained internally.

---

## Step 3: Customer Order

The customer places an order containing multiple items.

Example:

| Part Number | Quantity |
|-------------|----------|
| ABC1001 | 5 |
| XYZ2002 | 8 |
| DEF3003 | 2 |

---

## Step 4: Vendor Allocation

Every ordered part is matched against the original vendor inventory.

The system identifies:

- Which vendor owns the part
- Available quantity
- Product information

Purchase Orders are then generated separately for each vendor.

Example:

### Vendor A PO

| Part Number | Qty |
|-------------|-----|
| ABC1001 | 5 |
| LMN2222 | 3 |

### Vendor B PO

| Part Number | Qty |
|-------------|-----|
| XYZ2002 | 8 |

---

## Step 5: Vendor Supply

Each vendor supplies the ordered items.

However, vendors may not supply the complete requested quantity.

Example:

| Part | Ordered | Delivered |
|------|----------|------------|
| ABC1001 | 5 | 5 |
| LMN2222 | 3 | 1 |

---

## Step 6: Customer Dispatch

Prateek consolidates all received products and dispatches the final shipment to the customer.

---

# Existing Problems

## Problem 1: Inventory Matching

Manually searching thousands of products across multiple vendor files is time-consuming.

### Solution

Automatically search every vendor inventory and identify:

- Matching vendor
- Available quantity
- Complete product information

---

## Problem 2: Unfulfilled Items

Vendors often fail to deliver the full ordered quantity.

Example:

Ordered

| Part | Qty |
|------|-----|
| ABC1001 | 10 |

Received

| Part | Qty |
|------|-----|
| ABC1001 | 6 |

Shortfall:

```
10 - 6 = 4 pieces
```

The system should immediately identify these shortages.

---

## Problem 3: Alternative Vendor Search

If one vendor cannot fulfill the requested quantity, the system should automatically search other vendors for the remaining quantity.

Example:

Vendor A

```
Requested : 10
Delivered : 6
Remaining : 4
```

Automatically search:

```
Vendor B
Vendor C
Vendor D
```

and recommend the best alternative vendor.

---

## Problem 4: Vendor Reliability

Some vendors frequently advertise inventory but consistently under-deliver.

This reduces customer satisfaction and business credibility.

The system should continuously measure vendor performance.

---

# Proposed Solution

The project is divided into multiple modules.

---

# Module 1: Inventory Import

Import all vendor Excel/CSV files into a centralized system.

Features:

- Read multiple inventory files
- Detect Part Number column
- Detect Quantity column
- Store inventory in a centralized database
- Preserve vendor information

Status:

- Completed

---

# Module 2: Inventory Search / Vendor Comparison Report

**Redesigned 2026-07-31.** Search vendor inventories and list every matching
vendor -- do NOT choose one. Vendor Selection (manual or rule-based) is a
separate, not-yet-built module (see below).

Features:

- Scan all vendor inventory files (via the database, not the raw files
  directly -- see Module 1)
- Detect matching Part Number
- List EVERY vendor that carries the part, not just one
- Return complete product information: vendor name, part description (if
  available), available quantity, MRP, sale price, discount (if available),
  stock status, and source inventory file

Output (`output/vendor_comparison_report.xlsx`):

| Customer Part Number | Requested Qty | Vendor Name | Part Description | Vendor Available Qty | MRP | Sale Price | Discount | Stock Status | Inventory File |
|---|---|---|---|---|---|---|---|---|---|

Stock Status per vendor row: `Available` (qty >= requested) / `Partial`
(some stock, not enough) / `Out of Stock` (vendor carries the part, zero
qty) / `Not Found` (no vendor carries the part at all).

Status:

- Completed (see `order_matching.py` / `core/services/vendor_comparison_service.py`)

---

# Module 2.5: Vendor Selection (NOT YET BUILT)

Chooses one vendor per order line from the Vendor Comparison Report above,
using a business rule the founder will decide later:

- Lowest MRP
- Lowest Sale Price
- Highest Available Quantity
- Best Vendor Performance Score
- Fastest Delivery History
- Manual selection by the Purchase Team
- A combination of the above

Status:

- Not started. Until this exists, Module 3 below has no input to run
  against (`run_pipeline.py` stops after Module 2 -- see its comments).

---

# Module 3: Vendor Purchase Order Generator

Automatically group matched items vendor-wise.

Example:

Vendor A

| Part | Qty |
|------|-----|
| ABC1001 | 5 |
| LMN2222 | 3 |

Vendor B

| Part | Qty |
|------|-----|
| XYZ2002 | 8 |

Status:

- Implemented (`po_generator.py`), but currently has no valid input to run
  against -- it consumes a chosen-vendor-per-line `matching_output.csv`
  that Module 2 no longer produces (see Module 2.5 above).

---

# Module 4: Delivery Tracking

Import vendor delivery files.

Track:

- Ordered Quantity
- Delivered Quantity
- Pending Quantity

Status:

- Completed (see `delivery_import.py`)

---

# Module 5: Gap Analysis

Automatically calculate:

```
Pending Quantity = Ordered Quantity - Delivered Quantity
```

Example:

| Part | Ordered | Delivered | Pending |
|------|----------|------------|----------|
| ABC1001 | 10 | 6 | 4 |

Status:

- Completed (see `gap_analysis.py`)

---

# Module 6: Alternative Vendor Recommendation

Search remaining vendors for pending quantities.

Features:

- Search all vendors
- Recommend available inventory
- Minimize fulfillment delays

Status:

- Completed (see `alternative_vendor.py`)

---

# Module 7: Vendor Performance Dashboard

Calculate vendor reliability based on historical orders.

Formula:

```
Vendor Accuracy (%) =
(Total Delivered Quantity / Total Ordered Quantity) × 100
```

Example:

| Vendor | Ordered | Delivered | Accuracy |
|----------|----------|------------|-----------|
| Vendor A | 150 | 145 | 96.67% |
| Vendor B | 100 | 80 | 80.00% |

Low-performing vendors can be flagged for review.

Status:

- Completed (see `vendor_performance.py`)

---

# Module 8: Fulfillment Summary Report

Roll everything up into a single top-level report: overall totals, top and
worst performing vendors, and the parts most frequently left pending.

Status:

- Completed (see `summary_report.py`)

---

# Current Project Status

| Module | Status |
|----------|--------|
| Inventory Import | Completed |
| Order Matching Engine | Completed |
| Vendor PO Generation | Completed |
| Delivery Tracking | Completed |
| Gap Analysis | Completed |
| Alternative Vendor Search | Completed |
| Vendor Performance Dashboard | Completed |
| Fulfillment Summary Report | Completed |
| **Web Application** (Login, Dashboard, Vendor Inventory, Customer Orders, Vendor Comparison, Vendor Selection (manual + automatic), Vendor Invoice Verification, Delivery Tracking, Vendor Performance, WhatsApp receive, Gmail automation, Google Sheets sync, Settings) | **Completed** -- see [Web Application](#web-application) below |

---

# Web Application

**Added 2026-08-01, redesigned around the real workflow 2026-08-01, module
table/architecture updated 2026-08-03 to match the current codebase.** The
CLI tool above (`core/`, the module scripts) is being wrapped in a web
application -- FastAPI backend + React frontend -- one module at a time,
per the migration plan. **No business logic was rewritten**: the web app
calls the exact same `core/services/*` functions the CLI scripts call,
against the exact same database (SQLite locally, PostgreSQL in production
-- see `DEPLOYMENT.md`). Uploading a file through the browser and running
`python inventory_import.py` are two different front doors to the same
back room.

Vendors are never managed through a manual CRUD screen -- there is no
`vendors.py` route or `VendorsPage` (an earlier Phase 1 version had one;
it was removed once vendor identification was fully automated). Every
vendor is auto-created from its very first uploaded file, which also
auto-generates its permanent **Vendor Code** (see "Vendor Code" below) --
identification no longer depends on a WhatsApp sender number, since every
vendor messages the same shared WhatsApp Business number.

The application is not just "manage vendor inventory" -- its purpose is to
help the purchase team find which vendors can supply the parts a customer
ordered. The UI is organized around that workflow, not around database
tables:

```
Upload Vendor Inventory -> Upload Customer Order -> Vendor Comparison
                                                          |
                        (purchase team reviews and selects vendors manually,
                         OR runs the automatic vendor-selection rule engine --
                         Own Stock is always tried first, then either can
                         split the remainder across several external
                         vendors when no single vendor covers the full qty)
                                                          |
        Vendor Selection -> Export Selected Vendors (Excel), and if this was
        an AUTOMATIC selection: emails the same Excel to
        ALLOCATION_REPORT_EMAIL, then generates one Purchase Order per
        selected vendor (saved + downloadable, optionally emailed to
        PURCHASE_TEAM_EMAIL for internal review -- never sent to a vendor)
                                                          |
                   Vendor sends an invoice/delivery for what they supplied
                                                          |
   Upload Delivery File, OR Vendor Invoice PDF (auto-extracted + verified
   against the Vendor Selection above for short/extra/missing/unexpected
   supply) -> Delivery Tracking -> Vendor Performance
```

Purchase Orders in the web app (`VendorPurchaseOrder`/`VendorPurchaseOrderItem`)
are a distinct, newer concept from the legacy CLI's PO-based matching
pipeline (`core.services.purchase_order_service`, untouched) -- these are
keyed off `VendorSelection`, generated automatically, and never sent to a
vendor (vendor-direct delivery is a deferred future phase). See
[Automation Flow](#automation-flow) below for how WhatsApp, Gmail, and
Vendor Invoice PDFs feed into this same pipeline automatically.

## Modules implemented so far

| Module | What it does | Backend routes | Frontend page |
|---|---|---|---|
| Login | Username/password, JWT-based session | `POST /api/auth/login`, `/logout`, `/me`, `/change-password` | `LoginPage` |
| Dashboard | Active vendors, files imported, customer orders uploaded, parts matched/not found (from the latest order's comparison), last import time, recent activity | `GET /api/dashboard` | `DashboardPage` |
| Vendor Inventory | Upload one or many CSV/Excel files, progress, validation errors, import history (with each vendor's code), vendor-wise inventory viewer. Vendors are auto-created + auto-coded from their first file, never managed manually -- see "Vendor Code" below | `POST /api/inventory/imports`, `GET /api/inventory/imports`, `GET .../{id}/errors`, `POST .../confirm`, `POST .../cancel` | `VendorInventoryPage` |
| Customer Orders | Upload the customer's order file, order history, view items/errors -- becomes the input to Vendor Comparison | `POST/GET /api/customer-orders`, `GET .../{id}/items`, `GET .../{id}/errors` | `CustomerOrdersPage` |
| Vendor Comparison | **The heart of the app.** Pick a customer order, see every vendor that can supply each part (search/filter/sort/paginate), export to Excel | `GET /api/vendor-comparison/{order_id}`, `GET .../export` | `VendorComparisonPage` |
| Vendor Selection (manual + automatic) | Purchase team picks one or more vendors per order line from the comparison results (splitting a line across vendors when one alone can't cover it), or runs the rule engine (`highest_quantity`, `minimum_vendors`, `combination`) to select automatically -- **Own Stock is always tried first** regardless of strategy, with only the shortfall handed to the chosen strategy; exports the final allocation, and on automatic runs also emails the allocation report + generates Purchase Orders (see below) | `GET /api/vendor-selection/{order_id}`, `PUT/DELETE .../items/{item_id}/vendors/{vendor_id}`, `POST .../auto-select`, `GET .../export` | (reached from `VendorComparisonPage`) |
| Purchase Orders | One Purchase Order generated automatically per vendor after Automatic Vendor Selection, grouped from that vendor's `VendorSelection` rows; always saved + downloadable, optionally emailed to an internal purchase-team address (`PURCHASE_TEAM_EMAIL`) -- **never sent to a vendor** (vendor-direct delivery is a deferred future phase) | `GET /api/purchase-orders`, `GET .../{id}/export`, `GET .../{id}/lines` | `PurchaseOrdersPage` |
| Vendor Invoice Verification | Upload a vendor's invoice PDF (or receive one automatically); extracts vendor name/part numbers/quantities (`pdfplumber`), compares against that vendor's current Vendor Selection allocations, classifies each line (matched / short / extra / missing / unexpected supply), and mirrors matched/short/extra lines into the same delivery data Delivery Tracking and Vendor Performance already consume -- no changes needed to either | `POST/GET /api/vendor-invoices/imports`, `GET .../{id}/lines` | `VendorInvoicesPage` |
| Delivery Tracking | Upload what a vendor actually delivered; ordered vs. delivered vs. short, computed from Vendor Selection + these uploads (or from Vendor Invoice Verification, above) | `POST/GET /api/deliveries/imports`, `GET .../{id}/errors`, `GET /api/delivery-tracking` | `DeliveryTrackingPage` |
| Vendor Performance | Fulfillment %, delivery accuracy, ranking per vendor, computed from the same delivery data | `GET /api/vendor-performance`, `GET .../{vendor_id}` | `VendorPerformancePage` / `VendorPerformanceDetailPage` |
| WhatsApp Integration | Receives inventory/order/delivery files sent as WhatsApp attachments and imports them through the same pipeline as a manual upload. **Receive-only today -- the app cannot send WhatsApp replies.** Connection status/test only, no message composer | `GET/POST /api/whatsapp/webhook` (Meta-facing), `GET /api/integrations/whatsapp/status`, `POST .../test-connection` | `IntegrationStatusPage` (via Settings) |
| Gmail Automation | Polls a dedicated mailbox for unread mail (IMAP or OAuth), downloads Excel attachments (dropping the trailing two when more than two are attached), and imports them as Customer Orders through the same pipeline manual upload uses | `GET /api/integrations/gmail/status`, `POST .../test-connection` | `IntegrationStatusPage` (via Settings) |
| Google Sheets Sync | Pushes a vendor's active inventory to its own worksheet in a shared Google Sheet automatically after every inventory import (manual or WhatsApp) | `GET /api/integrations/google-sheets/status`, `POST .../test-connection` | `IntegrationStatusPage` (via Settings) |
| Settings | Account info, change password | (reuses `/api/auth/*`) | `SettingsPage` |

A "Document Inbox" page/route existed at one point (a read-only list of
every uploaded file's processing status) but was removed in a 2026-08-03
cleanup pass as unused -- nothing else in the app called it, and no
sidebar item pointed anywhere near a page that doesn't exist. The
underlying tracking it displayed (`backend/app/documents/`) is untouched
and still records every upload's lifecycle internally; only the standalone
viewing page is gone.

## Vendor Code

Every vendor's *permanent* identifier throughout this app is its **Vendor
Code**, not a WhatsApp sender number -- all vendors message the same shared
WhatsApp Business number, so a sender's phone number can never tell them
apart (see `core/services/vendor_code_service.py`).

**Format:** the first two letters of the vendor's name, uppercased, plus
`_CT`. Colliding names get a numeric suffix.

| Vendor name | Code |
|---|---|
| Arvind Auto Parts | `AR_CT` |
| North End | `NO_CT` |
| Lumax | `LU_CT` |
| (a second vendor also starting "AR...") | `AR_CT_2`, `AR_CT_3`, ... |

**Onboarding flow:**
1. A vendor's *very first* file may still be named with their real company
   name (e.g. `Arvind Auto Parts.xlsx`) -- the system auto-creates the
   vendor and auto-generates + permanently stores its code. The generated
   code is shown in the upload result message (and in the Vendor Inventory
   import history's "Vendor Code" column) so your team can hand it to the
   vendor.
2. From the **second** upload onward, the vendor must prefix their filename
   with that code, e.g. `AR_CT_Inventory.xlsx`. The system reads the code
   from the filename to identify the vendor -- no need to select one
   manually, and this works identically for a manual upload or a WhatsApp
   attachment.
3. A file whose name carries a code-shaped prefix that doesn't match any
   vendor (typo, or a code that was never assigned) is **rejected** with a
   clear error, rather than being silently imported under the wrong vendor
   or misclassified as a customer order.

`Vendor.whatsapp_number` still exists as informational contact metadata,
but is no longer used to identify which vendor sent a file.

### Own Stock priority (Bijvasan)

The company's own stock is represented by a vendor named **`Bijvasan`**. Any
vendor whose name matches that is automatically treated as own stock the
moment its inventory is uploaded -- **no database flag, no special UI, no
extra step**. Just upload an inventory file for a vendor named `Bijvasan` and
it becomes the priority source.

Matching is tolerant so a slight rename doesn't silently switch off own-stock
priority: it's **case-insensitive** and matches the configured name as a
**whole word**, so `Bijvasan`, `BIJVASAN`, `bijvasan`, `Bijvasan Warehouse`,
`Bijvasan Hub` and `Main Bijvasan Depot` all count as own stock -- while an
unrelated name that merely contains the letters (e.g. `Bijvasannual Traders`)
does not.

During **Automatic Vendor Selection**, own stock is **always allocated first**,
regardless of which strategy (`highest_quantity` / `minimum_vendors` /
`combination`) is chosen. Only the remaining shortfall (if any) is then handed
to the chosen strategy, which only ever sees the external (non-own-stock)
vendors -- the existing strategies are **not modified**, they simply run on
whatever quantity is left after Bijvasan.

Example -- customer needs **100 pcs**, available: `Bijvasan` 40, `Vendor A` 35,
`Vendor B` 50:

```
Bijvasan -> 40   (own stock, taken first)
remaining 60 handed to the chosen strategy across Vendor A + Vendor B
```

If Bijvasan alone covers the whole request (e.g. Bijvasan 120, customer 100),
the result is simply `Bijvasan -> 100` and **no external vendor is used**.

The name is configurable via the `OWN_STOCK_VENDOR_NAME` environment variable
(default `Bijvasan`); set it blank to disable name-based detection. The
original `Vendor.is_own_stock` database flag still works too -- a vendor is
own stock if EITHER its name matches OR that flag is set. See
[`core/services/own_stock.py`](core/services/own_stock.py).

### After Automatic Vendor Selection: Allocation Report email + Purchase Orders

Running **Automatic** Vendor Selection (the `Auto-select` action, i.e.
`POST /api/vendor-selection/{order_id}/auto-select`) triggers two follow-up
steps automatically. **Manual** vendor selection does neither -- it only ever
saves the allocation you can export yourself. Both steps are best-effort: an
email failure is logged and **never** fails the API call, so vendor selection
always completes.

**1. Automatic Allocation Report email.** The same Vendor Allocation Excel you
can export from the UI is generated and emailed to `ALLOCATION_REPORT_EMAIL`
(internal only -- never to a vendor). Skipped entirely, with no error, if that
variable is unset.

- **Subject:** `Vendor Allocation Report - Customer Order <Order Number>`
- **Body:** Customer Order Number, Order File, Date, Total Vendors Selected,
  Total Parts, and a per-vendor allocation summary.
- **Attachment:** the Vendor Allocation `.xlsx`.

**2. Purchase Order generation.** One Purchase Order (`.xlsx`) is generated per
selected vendor, grouped from that vendor's allocations, **stored in the
database**, and always downloadable from the **Purchase Orders** page
(`GET /api/purchase-orders`, `GET /api/purchase-orders/{id}/export`) --
regardless of any email setting. Each PO includes: Company Name / Address /
Phone / Email, Vendor Name, Vendor Code, Customer Order Number, and per line
the Part Number, Vendor Part Number and Quantity, plus the PO Number and Date.

Purchase Orders are **never sent to vendors**. If `ENABLE_PO_EMAIL=true` and
`PURCHASE_TEAM_EMAIL` is set, each PO is *also* emailed to that internal
address for review. Each PO tracks **its own** email status independently, so
if some emails succeed and others fail, only the failed ones are marked
`EMAIL_FAILED` (and PO generation still succeeds and stays downloadable).

If a send fails (e.g. a transient SMTP outage), the purchase team can retry
per-PO with the **Resend Email** button on the Purchase Orders page
(`POST /api/purchase-orders/{id}/resend-email`) once the mail issue is
resolved -- no need to re-run vendor selection. Resend targets
`PURCHASE_TEAM_EMAIL` only (never a vendor) and works even when
`ENABLE_PO_EMAIL` is `false`, since clicking it is an explicit request; it
just needs `PURCHASE_TEAM_EMAIL` configured.

Both steps reuse the already-configured Gmail integration (§7 of
`DEPLOYMENT.md`), so they work in **either** IMAP/App-Password SMTP **or**
OAuth Gmail API mode with no extra credentials.

### Environment variables for these features

All configurable, all documented in
[`backend/.env.example`](backend/.env.example). With every one left unset the
app runs fully in manual mode -- selection and PO download work, only the
automatic emails are skipped.

| Variable | Purpose | Default |
|---|---|---|
| `OWN_STOCK_VENDOR_NAME` | Vendor name treated as own stock (allocated first). Blank disables name-based detection. | `Bijvasan` |
| `ALLOCATION_REPORT_EMAIL` | Internal address the Allocation Report is emailed to after automatic selection. Unset = email skipped. | *(unset)* |
| `ENABLE_PO_EMAIL` | `true` to also email each generated PO to `PURCHASE_TEAM_EMAIL`. POs are generated + downloadable either way. | `false` |
| `PURCHASE_TEAM_EMAIL` | Internal address generated POs are emailed to (when `ENABLE_PO_EMAIL=true`). Never a vendor. | *(unset)* |
| `COMPANY_NAME` | Printed on every generated Purchase Order. | *(unset)* |
| `COMPANY_ADDRESS` | Printed on every generated Purchase Order. | *(unset)* |
| `COMPANY_PHONE` | Printed on every generated Purchase Order. | *(unset)* |
| `COMPANY_EMAIL` | Printed on every generated Purchase Order. | *(unset)* |

See `DEPLOYMENT.md` §4 for the full environment-variable reference and §9 for
the email setup walkthrough.

## Automation Flow

Three end-to-end automated paths, each landing in the same manual-upload
pipeline (`backend/app/services/document_processor/`) so automation and a
human clicking "Upload" are indistinguishable to every service downstream:

```
Vendor -> WhatsApp Business -> Document Processing Engine -> Inventory Import
                                                                     |
                                                          Google Sheets Sync
                                                    (that vendor's worksheet)

Customer -> Gmail -> Document Processing Engine -> Customer Orders
                                                          |
                                                Vendor Comparison
                                                          |
                          Own Stock tried first, then Automatic Vendor
                          Selection (rule engine) or manual selection --
                          either way, the same VendorSelection rows
                                                          |
                              (automatic runs only, beyond this point)
                                                          |
                    Vendor Allocation Report emailed to
                    ALLOCATION_REPORT_EMAIL, and one Purchase Order
                    generated per vendor -- saved + downloadable, optionally
                    emailed to PURCHASE_TEAM_EMAIL for internal review
                    (never sent to a vendor)

Vendor Invoice (PDF, via WhatsApp/email or manual upload)
        -> PDF Extraction (pdfplumber)
        -> Verification against that vendor's current Vendor Selection
        -> Delivery Tracking + Vendor Performance (updated automatically --
           mirrored into the same VendorDeliveryItem rows a delivery file
           upload would produce, so neither service needed to change)
```

Every automation is opt-in via an `_ENABLED` env var (`WHATSAPP_ENABLED`,
`GMAIL_ENABLED`/`ENABLE_EMAIL_AUTOMATION`, `ENABLE_GOOGLE_SHEETS_SYNC`,
`ENABLE_PO_EMAIL`) -- with all left unset/false, the app behaves exactly as
it does with manual uploads only. The Vendor Allocation Report email
(`ALLOCATION_REPORT_EMAIL`) and Purchase Order generation itself have no
separate on/off switch -- they run automatically whenever Automatic Vendor
Selection is used (PO generation always saves + makes them downloadable;
only the internal PO email is gated by `ENABLE_PO_EMAIL`). See
`backend/.env.example` for every variable and `DEPLOYMENT.md` for the full
WhatsApp/Gmail/Google Sheets setup walkthrough and production deployment
notes (including how the Gmail poll runs as an in-process background job,
not a separate worker process).

Vendor Comparison reuses the existing matching engine
(`core.services.vendor_comparison_service.compare_vendors`) completely
unchanged -- the only addition was `compare_vendors_for_order()`, which
feeds it rows from a persisted `CustomerOrder` instead of a raw CSV, and
`to_workbook()`, which both the CLI (`order_matching.py`) and the web
export button now call for the exact same `.xlsx` output (two extra
columns, Vendor Part Number and Brand, were added to every output --
nothing existing was removed).

## Architecture

```
backend/
  app/
    auth/            JWT auth -- separate from business logic on purpose.
                      User/RevokedToken models (share core.models.Base +
                      the same app.db), password hashing (bcrypt),
                      login/logout/me/change-password router. `role`
                      column exists now so future RBAC (Admin/Purchase
                      Team/Warehouse/Manager) can be added by reading it --
                      no migration needed.
    core/             Settings (JWT secret, CORS, upload dir, optional
                      ADMIN_USERNAME/ADMIN_PASSWORD bootstrap) -- config
                      only, not to be confused with the root-level `core/`
                      business package. Reads `backend/.env` if present.
    database/         `get_db()` FastAPI dependency wrapping the existing
                      `core.db` engine/session -- same database, request-
                      scoped commit/rollback.
    schemas/          Pydantic request/response models (API layer only;
                      never imported by core/services).
    api/routes/       Thin FastAPI routers (auth, dashboard, inventory,
                      customer_orders, vendor_comparison, vendor_selection,
                      vendor_invoices, deliveries, delivery_tracking,
                      vendor_performance, integration_status,
                      gmail_integration, google_sheets_integration,
                      whatsapp) -- validate input, call core/services, shape
                      output. No business rules live here. No `vendors.py`
                      -- vendors are auto-created from imported inventory
                      files, never managed via a dedicated CRUD route.
    documents/        `IncomingDocument` model + lifecycle service --
                      internal bookkeeping shared by every upload path
                      (manual, WhatsApp, and Gmail). Not exposed by any
                      listing route today (see the Document Inbox removal
                      note above).
    integrations/     whatsapp/ (Cloud API client, webhook handler, config
                      -- receive-only, cannot send messages), gmail/ (IMAP +
                      OAuth clients behind one interface, config, status),
                      google_sheets/ (sync service + config, called from the
                      inventory dispatch path, not polled).
    workers/          `document_worker.py` -- the WhatsApp receive path's
                      background handler, run via FastAPI `BackgroundTasks`.
                      `email_worker.py` -- Gmail poll, called periodically
                      by `scheduler.py`'s in-process APScheduler job
                      (started/stopped from FastAPI's `lifespan`; no
                      separate worker process/queue to deploy).
    services/         Framework-specific glue only (e.g. saving an
                      uploaded file to disk before handing the path to
                      `core.services.inventory_import_service.run_import`
                      or `customer_order_service.run_customer_order_import`).
  scripts/
    create_admin.py            One-time CLI to create/reset the admin
                                account -- deliberately not an API endpoint.
    migrate_schema_updates.py  One-time schema migration for databases that
                                pre-date the automation changes (see
                                DEPLOYMENT.md §12) -- `create_all` alone
                                can't alter an existing table.
core/
  services/
    rules/                          Automatic Vendor Selection strategies
                                     (`highest_quantity`, `minimum_vendors`,
                                     `combination`) behind one interface --
                                     `engine.py` applies a chosen strategy's
                                     allocations via the same
                                     `vendor_selection_service.upsert_selection`
                                     manual selection uses.
    invoice_extraction_service.py   Vendor Invoice PDF text/table extraction
                                     (`pdfplumber`), separate from the
                                     verification logic below so an OCR
                                     fallback can be added later without
                                     touching it.
    vendor_invoice_verification_service.py
                                     Resolves each extracted line against
                                     `VendorSelection`, classifies the
                                     discrepancy, and mirrors matched/short/
                                     extra lines into `VendorDeliveryItem` so
                                     Delivery Tracking / Vendor Performance
                                     update with zero changes to either.
frontend/
  src/
    api/              axios client + one file per resource (auth,
                      inventory, customerOrders, vendorComparison,
                      vendorSelection, vendorInvoices, deliveries,
                      deliveryTracking, vendorPerformance,
                      integrationStatus, dashboard) -- the UI never talks to
                      the database directly, only this layer. No
                      `vendors.js` -- no vendor CRUD UI.
    context/           AuthContext (JWT in localStorage) + ToastContext
                       (success/error notifications).
    components/        Layout (workflow-ordered sidebar nav + logout),
                       Modal, StatusPill, EmptyState, ProtectedRoute,
                       DashboardFilterBar.
    pages/              LoginPage, DashboardPage, VendorInventoryPage,
                        CustomerOrdersPage, VendorComparisonPage,
                        VendorInvoicesPage, DeliveryTrackingPage,
                        VendorPerformancePage, VendorPerformanceDetailPage,
                        IntegrationStatusPage, SettingsPage.
```

WhatsApp automation is implemented and receive-only: a human uploading a
file through the browser and a vendor sending one over WhatsApp both land
in the same place -- `backend/app/workers/document_worker.py` downloads
the WhatsApp attachment, classifies it, and calls the exact same
`core.services.*` import functions the manual upload endpoints call
(`inventory_import_service.run_import`,
`customer_order_service.run_customer_order_import`, and the delivery
equivalent). Only *how a file arrives* changes -- the endpoints, services,
and UI stay the same either way. The app cannot send a WhatsApp message
back; see `DEPLOYMENT.md` for what's required to enable receiving in
production.

Gmail automation follows the identical shape, just polled instead of
webhook-driven: `backend/app/workers/email_worker.py` (run periodically by
`backend/app/workers/scheduler.py`'s in-process APScheduler job) downloads
each unread message's Excel attachments and calls
`process_document(..., document_type_hint=CUSTOMER_ORDER)` -- the same
Document Processing Engine entry point every other upload path uses, which
in turn calls the same `customer_order_service.run_customer_order_import`.
A new vendor's first WhatsApp inventory message also links their WhatsApp
number to the auto-created vendor record, so subsequent messages from that
number are recognized without needing a caption keyword every time.

A `repositories/` layer (per the original suggested structure) was
deliberately not added: `core/services/*` already combines the repository
+ service roles for these simple CRUD/import operations, and an empty
pass-through wrapper would just be indirection with no behavior of its
own. If a module later needs real query complexity, a repository layer
can be introduced there without touching the others.

**Multi-user readiness, without building it yet:** the `User` model has a
`role` column (default `"admin"`) that nothing enforces today -- adding
Admin/Purchase Team/Warehouse/Manager permissions later means reading that
column in a dependency, not changing `core/services/*` or the DB schema.
Auth lives entirely under `backend/app/auth/`, never inside a business
service, so swapping in SSO or multi-tenant auth later doesn't touch
vendor/inventory logic.

## Setup

**Backend** (run from the repository root so both `core/` and `backend/`
are importable):

```bash
pip install -r backend/requirements.txt   # installs the root requirements.txt too

# Create the one admin account (first time only):
python -m backend.scripts.create_admin --username admin --password <choose one, 8+ chars>

# Start the API (reads/writes the same database/app.db the CLI scripts use):
python -m uvicorn backend.app.main:app --reload --port 8000
```

Configuration is read from `backend/.env` (git-ignored -- never committed)
by `backend/app/core/config.py`: `JWT_SECRET_KEY` (generate a real one for
anything beyond local dev -- without it, a random key is used and every
restart invalidates existing sessions), `JWT_EXPIRE_MINUTES` (default 480),
`CORS_ORIGINS` (default `http://localhost:5173`), `UPLOAD_DIR` (default
`uploads/inventory/`, also git-ignored), and an optional one-time
`ADMIN_USERNAME` / `ADMIN_PASSWORD` bootstrap pair (only takes effect while
zero user accounts exist -- otherwise use `create_admin.py --reset`).
Note: this is `backend/.env`, not `venv/.env` -- the virtualenv directory
is never read by the app.

**Frontend:**

```bash
cd frontend
npm install
cp .env.example .env   # points at http://127.0.0.1:8000 by default
npm run dev            # http://localhost:5173
```

Open `http://localhost:5173`, sign in with the admin account created
above, and you're on the Dashboard.

---

# Current Folder Structure

```
pythonscript/
│
├── inventory_import.py        Module 1: import vendor inventory files
├── order_matching.py          Module 2: match customer order to vendors
├── po_generator.py            Bridge: turn matching results into Purchase Orders
├── delivery_import.py         Module 4: import vendor delivery files
├── gap_analysis.py            Module 5: ordered vs delivered gap report
├── alternative_vendor.py      Module 6: alternative-vendor recommendations
├── vendor_performance.py      Module 7: vendor performance dashboard + charts
├── summary_report.py          Module 8: overall fulfillment summary report
├── run_pipeline.py            Convenience wrapper: runs steps 1-8 in order
├── ordermatching.py           Legacy standalone script (pre-database prototype,
│                              see "Legacy Script" note below)
│
├── input.csv                  Customer order (Module 2 input)
├── requirements.txt           Python dependencies
│
├── core/                      Shared models, ingestion, and service layer
│   ├── models.py              SQLAlchemy ORM models (vendors, parts, orders, ...)
│   ├── db.py                  Engine/session setup for database/app.db
│   ├── hashing.py              File hashing (duplicate-import detection)
│   ├── logging_setup.py       Shared logging configuration
│   ├── ingestion/             File readers + column detection
│   │   ├── csv_reader.py
│   │   ├── excel_reader.py
│   │   ├── column_detector.py
│   │   └── types.py
│   └── services/              Business logic used by the module scripts
│       ├── inventory_import_service.py
│       ├── part_resolution_service.py
│       ├── vendor_service.py
│       ├── vendor_comparison_service.py  now also compare_vendors_for_order()
│       │                                 + to_workbook() (see Web Application)
│       ├── customer_order_service.py     new -- persists uploaded customer
│       │                                 order files (web app only, no CLI
│       │                                 predecessor)
│       ├── purchase_order_service.py
│       ├── delivery_import_service.py
│       ├── gap_analysis_service.py
│       ├── alternative_vendor_service.py
│       ├── vendor_performance_service.py
│       ├── dashboard_service.py      new -- read-only aggregation for the
│       │                            web Dashboard
│       ├── vendor_selection_service.py   web app only -- multi-vendor
│       │                                 allocation per order line
│       ├── rules/                        Automatic Vendor Selection
│       │                                 strategies (highest_quantity,
│       │                                 minimum_vendors, combination)
│       ├── vendor_delivery_service.py    web app's vendor+part delivery
│       │                                 upload (no PO concept)
│       ├── vendor_performance_tracking_service.py  web app's delivery-
│       │                                           tracking-based version
│       ├── invoice_extraction_service.py           Vendor Invoice PDF
│       │                                           extraction (pdfplumber)
│       └── vendor_invoice_verification_service.py  compares extracted
│                                                    invoice lines against
│                                                    VendorSelection
│
├── tests/                      Unit tests (pytest) + fixtures
├── raw_files/                  Vendor inventory files (Module 1 input)
├── delivery_files/             Vendor delivery files (Module 4 input)
├── output/                     Generated .csv / .xlsx reports
├── charts/                     Generated .png charts
├── database/                   SQLite database (app.db) -- shared by the
│                              CLI scripts above AND the web app below
│
├── backend/                    FastAPI web app (see "Web Application")
│   ├── requirements.txt
│   ├── scripts/create_admin.py, migrate_schema_updates.py
│   └── app/
│       ├── main.py
│       ├── auth/               JWT auth -- independent of core/services
│       ├── core/config.py
│       ├── database/session.py
│       ├── documents/          IncomingDocument model + lifecycle service
│       │                       (internal upload-status bookkeeping)
│       ├── integrations/       whatsapp/ (receive-only Cloud API client),
│       │                       gmail/ (IMAP + OAuth clients), google_sheets/
│       │                       (sync service, called from inventory import)
│       ├── workers/             document_worker.py (WhatsApp receive path),
│       │                       email_worker.py (Gmail poll), scheduler.py
│       │                       (in-process APScheduler, no separate worker)
│       ├── schemas/
│       ├── api/routes/         auth.py, dashboard.py, inventory.py,
│       │                       customer_orders.py, vendor_comparison.py,
│       │                       vendor_selection.py, vendor_invoices.py,
│       │                       deliveries.py, delivery_tracking.py,
│       │                       vendor_performance.py, integration_status.py,
│       │                       gmail_integration.py,
│       │                       google_sheets_integration.py, whatsapp.py
│       │                       (no vendors.py -- see note above)
│       └── services/           inventory_service.py, customer_order_service.py,
│                                delivery_service.py, invoice_service.py,
│                                document_processor/ (upload-handling glue,
│                                not business logic)
│
└── frontend/                   React (Vite) web app (see "Web Application")
    └── src/
        ├── api/                auth, inventory, customerOrders,
        │                       vendorComparison, vendorSelection,
        │                       vendorInvoices, deliveries, deliveryTracking,
        │                       vendorPerformance, integrationStatus,
        │                       dashboard, client
        ├── context/
        ├── components/
        └── pages/               LoginPage, DashboardPage, VendorInventoryPage,
                                  CustomerOrdersPage, VendorComparisonPage,
                                  VendorInvoicesPage, DeliveryTrackingPage,
                                  VendorPerformancePage,
                                  VendorPerformanceDetailPage,
                                  IntegrationStatusPage, SettingsPage
```

---

# Setup

## Prerequisites

- Python 3.11+ (developed/tested on Python 3.13)
- pip

## Installation

Run these once, from the project root (`pythonscript/`):

```bash
# 1. (Recommended) create and activate a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt
```

This installs `sqlalchemy`, `openpyxl`, `xlrd`, `matplotlib`, and `pytest`
(see `requirements.txt`). No separate database server is needed — the app
uses a local SQLite file at `database/app.db`, created automatically on
first run.

## Running the tests (optional but recommended)

```bash
pytest
```

---

# Running the Full Pipeline

This is a **local CLI application** -- no server, no web UI. Every script
reads/writes the same SQLite database (`database/app.db`) and is meant to be
run from the project root.

**As of 2026-07-31, only steps 1–2 are wired up end-to-end.** Vendor
Selection (between steps 2 and 3) hasn't been built yet -- see Module 2.5
above -- so steps 3–8 currently have no valid input:

```bash
python inventory_import.py     # 1. import every vendor file in raw_files/
python order_matching.py       # 2. search vendor inventory -> output/vendor_comparison_report.xlsx

# --- Vendor Selection (manual or rule-based) -- NOT YET BUILT ---

python po_generator.py         # 3. turn a CHOSEN vendor per line into Purchase Orders
python delivery_import.py      # 4. import every vendor file in delivery_files/
python gap_analysis.py         # 5. ordered vs delivered -> output/gap_report.xlsx
python alternative_vendor.py   # 6. -> output/alternative_vendor_report.xlsx
python vendor_performance.py   # 7. -> output/vendor_dashboard.xlsx + charts/*.png
python summary_report.py       # 8. -> output/summary_report.xlsx
```

Or run steps 1–2 in one go:

```bash
python run_pipeline.py
```

`run_pipeline.py` is a convenience wrapper, not a separate module — it runs
whatever's in its `scripts` list in order, printing a
`=== Running <script> ===` header before each, and stops immediately if any
script exits with a non-zero return code. Steps 3–8 are commented out in
that list (with a note explaining why) rather than deleted, so re-enabling
the rest of the pipeline once Vendor Selection exists is a one-line change.

Notes before your first run:

- Make sure vendor inventory files are in `raw_files/`, the customer order
  is at `input.csv`, and vendor delivery files are in `delivery_files/`
  (sample fixtures are already included in this repo — see "Sample / test
  data" below).
- `output/`, `charts/`, and `database/` are created automatically if they
  don't exist yet; their contents (except `.gitkeep`-style placeholders) are
  git-ignored (see `.gitignore`), so a fresh clone starts with empty
  folders until you run the pipeline.
- Once Vendor Selection exists: steps 1–2 must run before step 3; step 4
  must run before steps 5–7; step 8 can run any time after step 4, since it
  aggregates from the database rather than from another report's output
  file.

Each script is independently runnable and re-runnable:

- **`po_generator.py`** reads a chosen-vendor-per-line `output/matching_output.csv`
  and creates one `PurchaseOrder` per vendor from its MATCHED/PARTIAL lines,
  with an auto-numbered PO Number (`PO001`, `PO002`, ...). Re-running against
  an unchanged matching result is a no-op. **Currently has no producer:**
  `order_matching.py` now writes `output/vendor_comparison_report.xlsx`
  instead (every vendor listed, none chosen) -- see Module 2.5 above. The
  old file was archived to `output/matching_output.csv.legacy`.
- **`delivery_import.py`** scans every `.csv`/`.xlsx`/`.xlsm`/`.xls` file in
  `delivery_files/`, auto-detects the Vendor / PO Number / Part Number /
  Delivered Quantity columns (case- and spacing-insensitive, e.g. "Vendor
  Name" or "Vendor", "PO Number" or "PO No", "Delivered Qty" or "Delivered
  Quantity"), and validates each row against the vendor, the PO, and the
  part actually ordered on that PO. Invalid rows are logged to the
  `delivery_import_errors` table and skipped; valid rows are stored as
  `DeliveryItem` records. Re-importing an unchanged file (same name + same
  content) is skipped as a duplicate.
- **`gap_analysis.py`**, **`alternative_vendor.py`**, **`vendor_performance.py`**
  and **`summary_report.py`** all compute their results directly from the
  database (`purchase_order_items` + `delivery_items`) -- none of them
  depend on another report's output file, so any of them can be run on its
  own once deliveries have been imported.

Pending Quantity is never stored as a mutable column; it's always computed
fresh as `Ordered Quantity - Delivered Quantity` (floored at 0) from the
two source tables. The generated `.xlsx` reports *are* the durable record of
each run's result.

## Sample / test data

- `raw_files/VendorD_Spares.csv` is a small supplementary vendor inventory
  fixture (not one of the original three vendor files) added purely so
  `alternative_vendor.py` has a real cross-vendor match to demonstrate for
  part `111`. It doesn't change any of the original vendors' data.
- `delivery_files/` contains sample delivery files covering all three gap
  statuses (fully delivered, partially delivered, not delivered) plus
  `bad_delivery_example.csv`, which deliberately contains one row for every
  validation failure `delivery_import.py` can raise (unknown vendor, unknown
  PO, PO/vendor mismatch, unknown part, part not on that PO, invalid
  quantity, blank fields) so you can see the error logging in action.

---

# Legacy Script (`ordermatching.py`)

`ordermatching.py` was the original standalone prototype for Module 2,
written before the database-backed architecture (`core/`, `order_matching.py`)
existed. It is kept in the repo for reference and still runs on its own
(no database required), but it is **not** part of the pipeline described
above — use `order_matching.py` for current work. It performs the following
tasks:

1. Scans every CSV file inside `raw_files/`.
2. Automatically detects the Part Number and Quantity columns.
3. Stores inventory information in memory.
4. Reads the customer order from `input.csv`.
5. Matches each ordered part against all vendor inventories.
6. Selects a suitable inventory row that satisfies the requested quantity.
7. Prevents reuse of the same inventory row.
8. Generates `matching_output.csv` containing:
   - Requested Part Number
   - Requested Quantity
   - Fulfilled Quantity
   - Source File Name
   - Match Status
   - Complete source row information

---

# Future Enhancements

Delivered as part of this CLI application: Excel (.xlsx/.xls) support,
database integration (SQLite via SQLAlchemy), automatic Purchase Order
generation, Excel reports, vendor performance analytics, inventory import
history, and an alternative vendor recommendation engine.

Delivered as part of the Phase 1 web app (see "Web Application" above):
a web-based Dashboard, JWT login, and browser-based Vendor Management /
Inventory Import -- wrapping the same `core/` services, not replacing them.

Still not in scope (Phase 2 onward -- see the web app's roadmap):

- Real-time stock updates
- Customer order tracking (persisted, multi-order lifecycle) via the web UI
- Role-based access control (the `User.role` column exists for this, but
  nothing enforces it yet)
- Multi-user accounts (schema supports it; no self-service user management
  UI yet -- accounts are created via `backend/scripts/create_admin.py`)

---

# Technology Stack

## Current

- Python 3
- SQLAlchemy ORM + SQLite (`database/app.db`)
- CSV / Excel processing (`openpyxl`, `xlrd`)
- FastAPI (`backend/`) -- REST API wrapping `core/services/*`
- JWT authentication + bcrypt password hashing (`backend/app/auth/`)
- React + Vite (`frontend/`) -- Phase 1 UI (Login, Dashboard, Vendor
  Management, Inventory Import)

## Planned (later phases)

- PostgreSQL (if/when SQLite's single-writer model becomes a bottleneck)
- Chart.js / Recharts (Vendor Performance dashboard, Phase 5)
- Role-based access control UI (Admin / Purchase Team / Warehouse / Manager)
- Pandas (only if a future module's data-shaping needs outgrow the current
  per-row ingestion approach)

---

# Goal

Build a centralized inventory and order fulfillment system that:

- Consolidates vendor inventories.
- Automatically matches customer orders to vendors.
- Generates vendor-specific purchase orders.
- Tracks delivery status and shortages.
- Identifies alternative vendors for pending quantities.
- Measures vendor fulfillment accuracy over time.
- Reduces manual effort, improves order fulfillment, and provides actionable insights into vendor performance.