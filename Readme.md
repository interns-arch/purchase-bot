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
| **Web Application** (Login, Dashboard, Vendor Management, Vendor Inventory, Customer Orders, Vendor Comparison, Settings) | **Completed** -- see [Web Application](#web-application) below |

---

# Web Application

**Added 2026-08-01, redesigned around the real workflow 2026-08-01.** The
CLI tool above (`core/`, the module scripts) is being wrapped in a web
application -- FastAPI backend + React frontend -- one module at a time,
per the migration plan. **No business logic was rewritten**: the web app
calls the exact same `core/services/*` functions the CLI scripts call,
against the exact same SQLite database (`database/app.db`). Uploading a
file through the browser and running `python inventory_import.py` are two
different front doors to the same back room.

The application is not just "manage vendor inventory" -- its purpose is to
help the purchase team find which vendors can supply the parts a customer
ordered. The UI is organized around that workflow, not around database
tables:

```
Upload Vendor Inventory  ->  Upload Customer Order  ->  Vendor Comparison
                                                              |
                                     (purchase team reviews, selects vendors)
                                                              |
                              Purchase Orders (later phase) -> Delivery Upload
                              (later phase) -> Vendor Performance (later phase)
```

Vendor Selection, Purchase Order Generation, Delivery Upload, Gap Analysis,
Vendor Performance, and WhatsApp Integration will wrap the remaining
`core/services/*` modules the same way, in later passes -- their sidebar
entries already exist today, labeled "Coming Soon."

## Modules implemented so far

| Module | What it does | Backend routes | Frontend page |
|---|---|---|---|
| Login | Username/password, JWT-based session | `POST /api/auth/login`, `/logout`, `/me`, `/change-password` | `LoginPage` |
| Dashboard | Active vendors, files imported, customer orders uploaded, parts matched/not found (from the latest order's comparison), last import time, recent activity | `GET /api/dashboard` | `DashboardPage` |
| Vendor Management | Add / Edit / Disable / View vendor | `GET/POST /api/vendors`, `GET/PATCH /api/vendors/{id}`, `POST /api/vendors/{id}/disable` | `VendorsPage` |
| Vendor Inventory | Upload one or many CSV/Excel files, progress, validation errors, import history, vendor-wise inventory viewer | `POST /api/inventory/imports`, `GET /api/inventory/imports`, `GET /api/inventory/imports/{id}/errors`, `POST .../confirm`, `POST .../cancel`, `GET /api/inventory/vendors/{id}/items` | `VendorInventoryPage` |
| Customer Orders | Upload the customer's order file, order history, view items/errors -- becomes the input to Vendor Comparison | `POST/GET /api/customer-orders`, `GET .../{id}/items`, `GET .../{id}/errors` | `CustomerOrdersPage` |
| Vendor Comparison | **The heart of the app.** Pick a customer order, see every vendor that can supply each part (search/filter/sort/paginate), export to Excel | `GET /api/vendor-comparison/{order_id}`, `GET .../export` | `VendorComparisonPage` |
| Settings | Account info, change password | (reuses `/api/auth/*`) | `SettingsPage` |
| Purchase Orders / Delivery Upload / Vendor Performance | Sidebar placeholders ("Coming Soon") for later phases | -- | `ComingSoonPage` |

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
    api/routes/       Thin FastAPI routers (vendors, inventory,
                      customer_orders, vendor_comparison, dashboard) --
                      validate input, call core/services, shape output.
                      No business rules live here.
    services/         Framework-specific glue only (e.g. saving an
                      uploaded file to disk before handing the path to
                      `core.services.inventory_import_service.run_import`
                      or `customer_order_service.run_customer_order_import`).
  scripts/
    create_admin.py   One-time CLI to create/reset the admin account --
                      deliberately not an API endpoint in Phase 1.
frontend/
  src/
    api/              axios client + one file per resource (auth, vendors,
                      inventory, customerOrders, vendorComparison,
                      dashboard) -- the UI never talks to the database
                      directly, only this layer.
    context/           AuthContext (JWT in localStorage) + ToastContext
                       (success/error notifications).
    components/        Layout (workflow-ordered sidebar nav + logout),
                       Modal, StatusPill, EmptyState, ProtectedRoute.
    pages/              LoginPage, DashboardPage, VendorsPage,
                        VendorInventoryPage, CustomerOrdersPage,
                        VendorComparisonPage, SettingsPage, ComingSoonPage.
```

Future WhatsApp automation is designed to slot in without any UI change:
today a human uploads a file through the browser; later, a background
worker (not built yet) would download a WhatsApp attachment, detect the
sender, classify it as Vendor Inventory / Customer Order / Delivery, and
call the exact same `core.services.*` import functions the upload
endpoints call today (`inventory_import_service.run_import`,
`customer_order_service.run_customer_order_import`, and a future delivery
equivalent). Only *how a file arrives* changes -- the endpoints, services,
and UI stay the same either way.

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
│       └── dashboard_service.py      new -- read-only aggregation for the
│                                     web Dashboard
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
│   ├── scripts/create_admin.py
│   └── app/
│       ├── main.py
│       ├── auth/               JWT auth -- independent of core/services
│       ├── core/config.py
│       ├── database/session.py
│       ├── schemas/
│       ├── api/routes/         vendors.py, inventory.py, customer_orders.py,
│       │                       vendor_comparison.py, dashboard.py
│       └── services/           inventory_service.py, customer_order_service.py
│                                (upload-handling glue, not business logic)
│
└── frontend/                   React (Vite) web app (see "Web Application")
    └── src/
        ├── api/                auth, vendors, inventory, customerOrders,
        │                       vendorComparison, dashboard, client
        ├── context/
        ├── components/
        └── pages/               LoginPage, DashboardPage, VendorsPage,
                                  VendorInventoryPage, CustomerOrdersPage,
                                  VendorComparisonPage, SettingsPage,
                                  ComingSoonPage
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