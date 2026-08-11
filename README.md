# Vendor Inventory & Order Fulfillment Automation

A vendor inventory sourcing and order-fulfillment system for an auto-parts trading business. Vendors send inventory files (Excel/CSV); customers send order files. The system consolidates all vendor inventory, matches every customer order against every vendor, helps the purchase team select vendors, generates purchase orders, verifies vendor invoices, tracks deliveries and shortfalls, and scores vendor reliability — replacing a fully manual Excel process.

**The one-line mental model:**

> A file arrives (manual upload / WhatsApp / Gmail) → staged and classified → dispatched to the matching `core/services` importer → written to one shared database → surfaced through Vendor Comparison → Vendor Selection (Own-Stock-first + rules) → Allocation email + Purchase Orders → Invoice/Delivery verification → Delivery Tracking + Vendor Performance → Dashboard.

The project has **two front doors sharing one brain**:

- **`core/`** — the brain: pure business logic + the database, importable on its own.
- **Web app** — FastAPI backend (`backend/`) + React frontend (`frontend/`) wrapping the *same* `core/` services. This is the live product (frontend → Vercel, backend → Render, DB → Neon Postgres in production; SQLite locally).
- **Legacy CLI scripts** at the repo root (`inventory_import.py`, `order_matching.py`, …) — the older file-in/file-out pipeline, still runnable, sharing the same database.

No business logic was rewritten for the web app: uploading a file through the browser and running `python inventory_import.py` are two different front doors to the same back room.

---

## 1. Quick Start (Web App)

The web app is two separate long-running processes — **you need two terminals open at the same time**, both left running while you use the app:

- **Terminal 1** runs the backend (FastAPI/uvicorn).
- **Terminal 2** runs the frontend (Vite dev server).

They're independent processes talking over HTTP (frontend calls `http://127.0.0.1:8000`), not one launching the other — "run backend then frontend" really means "start backend, leave it running, then in a *second* terminal start frontend."

**Important:** every backend command must be run from the **project root** (`D:\Downloads\pythonscript`), not from inside `backend\`. Running `python -m backend.scripts.create_admin` while your current directory is `backend\` fails with `ModuleNotFoundError: No module named 'backend'` — Python resolves `-m backend.x.y` relative to the current directory, and there is no `backend\backend\` folder. `cd ..` back to the project root first.

### Terminal 1 — Backend

```powershell
cd D:\Downloads\pythonscript          # project root, NOT backend\
venv\Scripts\activate                 # if not already active
pip install -r backend\requirements.txt   # first time only (installs root requirements.txt too)

# First time only -- creates the admin account:
python -m backend.scripts.create_admin --username admin --password <choose one, 8+ chars>

# Start the API and leave this terminal running:
python -m uvicorn backend.app.main:app --reload --port 8000
```

Leave this window open. `--reload` restarts the server automatically when you edit backend code, but the process itself must stay running.

### Terminal 2 — Frontend

Open a **new** terminal window/tab (don't close Terminal 1):

```powershell
cd D:\Downloads\pythonscript\frontend
npm install                           # first time only
copy .env.example .env                # first time only -- points at http://127.0.0.1:8000
npm run dev
```

Open the URL it prints (default `http://localhost:5173`) and log in with the admin account you created above.

### Everyday use after first-time setup

The install/`create_admin` steps are one-time. After that:

```powershell
# Terminal 1
cd D:\Downloads\pythonscript
venv\Scripts\activate
python -m uvicorn backend.app.main:app --reload --port 8000

# Terminal 2
cd D:\Downloads\pythonscript\frontend
npm run dev
```

To reset the admin password later (from the project root; the backend does *not* need to be running): `python -m backend.scripts.create_admin --username admin --reset`

### Backend configuration (`backend/.env`)

Read by `backend/app/core/config.py` (git-ignored — never committed). Note: this is `backend/.env`, **not** `venv/.env` — the virtualenv directory is never read by the app.

| Variable | Purpose | Default |
|---|---|---|
| `JWT_SECRET_KEY` | Session signing key. Generate a real one for anything beyond local dev — without it a random key is used and every restart invalidates existing sessions. | *(random per start)* |
| `JWT_EXPIRE_MINUTES` | Session lifetime | `480` |
| `CORS_ORIGINS` | Allowed frontend origin(s) | `http://localhost:5173` |
| `UPLOAD_DIR` | Where uploads are stored (git-ignored) | `uploads/inventory/` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | One-time admin bootstrap — only takes effect while zero user accounts exist; otherwise use `create_admin.py --reset` | *(unset)* |
| `DATABASE_URL` | Set → Postgres (production); unset → local SQLite `database/app.db` | *(unset)* |

Automation and email variables are listed in [§6](#6-environment-variables-for-automation) below. See `backend/.env.example` for the full annotated list and `DEPLOYMENT.md` for production deployment, integration setup walkthroughs, and the schema migration script (`backend/scripts/migrate_schema_updates.py`) for databases that pre-date the automation changes.

---

## 2. The Business Workflow

The manual process this replaces: multiple vendors share inventory in Excel/CSV files that are hand-consolidated into a master stock list shared with customers; customer orders are hand-split across vendors; vendors often under-deliver, and shortfalls, alternative vendors, and vendor reliability are all tracked by hand. The four core problems: slow manual inventory matching across thousands of parts, undetected short deliveries, no automatic search for alternative vendors for pending quantities, and no measurement of which vendors habitually advertise stock they can't supply.

The app is organized around that workflow, not around database tables:

```
Upload Vendor Inventory  ->  Upload Customer Order  ->  Vendor Comparison
                                                          |
                        (purchase team reviews and selects vendors manually,
                         OR runs the automatic vendor-selection rule engine --
                         Own Stock is always tried first, then either path can
                         split the remainder across several external vendors
                         when no single vendor covers the full qty)
                                                          |
        Vendor Selection -> Export Selected Vendors (Excel); if this was an
        AUTOMATIC selection: the same Excel is emailed to
        ALLOCATION_REPORT_EMAIL, then one Purchase Order is generated per
        selected vendor (saved + downloadable, optionally emailed to
        PURCHASE_TEAM_EMAIL for internal review -- never sent to a vendor)
                                                          |
                   Vendor sends an invoice/delivery for what they supplied
                                                          |
   Upload Delivery File, OR Vendor Invoice PDF (auto-extracted + verified
   against the Vendor Selection for short/extra/missing/unexpected supply)
        ->  Delivery Tracking  ->  Vendor Performance
```

### Step by step

1. **Vendor Inventory Import** — manual upload (`POST /api/inventory/imports`) or WhatsApp → `core/services/inventory_import_service.run_import`: reads the file (`core/ingestion/csv_reader.py` / `excel_reader.py`), auto-detects Part Number / Quantity / Price / MRP columns (`column_detector.py`), resolves each row to a canonical `Part` (`part_resolution_service.py`), writes an `InventoryImport` snapshot (marked `is_active`; older ones deactivated) + `VendorInventory` rows, skips unchanged re-uploads via SHA-256 (`core/hashing.py`), then fires the Google Sheets sync. Vendor identity comes from the **Vendor Code** in the filename (see §4); a brand-new vendor's first file is onboarded by company name and gets a generated code.

2. **Customer Order Import** — manual upload, Gmail, or WhatsApp-with-`Customer` → `customer_order_service.run_customer_order_import` → `CustomerOrder` + `CustomerOrderItem` (deduped by file content).

3. **Vendor Comparison** (the heart of the app) — `GET /api/vendor-comparison/{order_id}` → `vendor_comparison_service.compare_vendors_for_order`: pulls every vendor's active stock and, for each ordered part, lists **every** vendor that stocks it (vendor name, part description, vendor part number, brand, available qty, MRP, sale price, discount, source file) with a per-vendor stock status: `Available` (qty ≥ requested) / `Partial` (some stock, not enough) / `Out of Stock` (carries the part, zero qty) / `Not Found` (no vendor carries it). It deliberately **chooses no vendor** — it only reports. Own-stock offers are flagged here. Exportable to Excel; the CLI's `order_matching.py` and the web export button call the same `to_workbook()` for identical output.

4. **Vendor Selection** — manual or automatic; both write the same `VendorSelection` rows, and a single order line can be split across several vendors when no one vendor covers the full quantity.
   - **Manual:** `PUT /api/vendor-selection/{order_id}/items/{item_id}/vendors/{vendor_id}` → `vendor_selection_service.upsert_selection`. Saves the allocation; you can export it yourself. No emails, no POs.
   - **Automatic:** `POST /api/vendor-selection/{order_id}/auto-select?strategy=` → `core/services/rules/engine.py`. **Own Stock is always allocated first** regardless of strategy; only the remaining shortfall is handed to the chosen strategy (`highest_quantity` / `minimum_vendors` / `combination`), which only ever sees external vendors — the strategies themselves are unmodified.

5. **After automatic selection only** — two best-effort follow-ups (an email failure is logged and **never** fails the API call):
   1. The Vendor Allocation Excel is emailed to `ALLOCATION_REPORT_EMAIL` (internal only; skipped silently if unset). Subject: `Vendor Allocation Report - Customer Order <Order Number>`; body: order number, file, date, total vendors selected, total parts, per-vendor summary; attachment: the `.xlsx`.
   2. **One Purchase Order per selected vendor** is generated (`purchase_order_generation_service.py` → `VendorPurchaseOrder` + items), **stored in the database and always downloadable** from the Purchase Orders page regardless of any email setting. Each PO carries the company details from env, vendor name + code, customer order number, PO number and date, and per line the part number, vendor part number, and quantity. If `ENABLE_PO_EMAIL=true` and `PURCHASE_TEAM_EMAIL` is set, each PO is *also* emailed there — each PO tracks its own email status (`EMAILED`/`EMAIL_FAILED`) independently, and a **Resend Email** button (`POST /api/purchase-orders/{id}/resend-email`) retries per-PO without re-running selection. Resend targets `PURCHASE_TEAM_EMAIL` only and works even with `ENABLE_PO_EMAIL=false`, since clicking it is an explicit request. **POs are never sent to vendors** (vendor-direct delivery is a deferred future phase). Both follow-ups reuse the already-configured Gmail integration — IMAP/App-Password SMTP or OAuth Gmail API mode, no extra credentials.

6. **Vendor supplies → Invoice or Delivery file**
   - **Vendor Invoice PDF** (WhatsApp/email or manual upload) → `invoice_extraction_service.py` (pdfplumber; extraction is separate from verification so an OCR fallback can be added later) + `vendor_invoice_verification_service.py`: extracts vendor name / part numbers / quantities, compares against that vendor's current Vendor Selection, classifies each line (**matched / short / extra / missing / unexpected supply**), and mirrors matched/short/extra lines into the same `VendorDeliveryItem` rows a delivery upload would produce — so Delivery Tracking and Vendor Performance update with zero changes to either.
   - **Delivery file** (manual upload) → `vendor_delivery_service.py` → `VendorDeliveryImport` + `VendorDeliveryItem`.

7. **Delivery Tracking** — `GET /api/delivery-tracking` → `delivery_tracking_service.py`: ordered vs. delivered vs. pending, computed from Vendor Selection + delivery data. Pending is **always computed** as `Ordered − Delivered` (floored at 0), never stored as a mutable column.

8. **Vendor Performance** — `GET /api/vendor-performance` → `vendor_performance_tracking_service.py`: fulfillment % (`Delivered / Ordered × 100`), delivery accuracy, and ranking per vendor, from the same delivery data. Low performers can be flagged for review.

**Cross-cutting: Dashboard** — `GET /api/dashboard` → `dashboard_service.py`: active vendors, files imported, customer orders, parts matched/not found (from the latest order's comparison), last import time, recent activity.

---

## 3. Web App Modules

| Module | What it does | Backend routes | Frontend page |
|---|---|---|---|
| Login | Username/password, JWT session | `POST /api/auth/login`, `/logout`, `/me`, `/change-password` | `LoginPage` |
| Dashboard | Top-level stats + recent activity | `GET /api/dashboard` | `DashboardPage` |
| Vendor Inventory | Upload one/many CSV/Excel files, progress, validation errors, import history (with Vendor Code), vendor-wise viewer | `POST/GET /api/inventory/imports`, `GET .../{id}/errors`, `POST .../confirm`, `.../cancel` | `VendorInventoryPage` |
| Customer Orders | Upload order file, history, items/errors | `POST/GET /api/customer-orders`, `GET .../{id}/items`, `.../{id}/errors` | `CustomerOrdersPage` |
| Vendor Comparison | Every vendor per ordered part; search/filter/sort/paginate; Excel export | `GET /api/vendor-comparison/{order_id}`, `.../export` | `VendorComparisonPage` |
| Vendor Selection | Manual pick or auto-select rule engine; export allocation | `GET /api/vendor-selection/{order_id}`, `PUT/DELETE .../items/{item_id}/vendors/{vendor_id}`, `POST .../auto-select`, `GET .../export` | (reached from `VendorComparisonPage`) |
| Purchase Orders | One PO per vendor after auto-selection; download; optional internal email + resend | `GET /api/purchase-orders`, `.../{id}/export`, `.../{id}/lines`, `POST .../{id}/resend-email` | `PurchaseOrdersPage` |
| Vendor Invoice Verification | PDF extraction + verification against Vendor Selection | `POST/GET /api/vendor-invoices/imports`, `GET .../{id}/lines` | `VendorInvoicesPage` |
| Delivery Tracking | Ordered vs. delivered vs. short | `POST/GET /api/deliveries/imports`, `GET .../{id}/errors`, `GET /api/delivery-tracking` | `DeliveryTrackingPage` |
| Vendor Performance | Fulfillment %, accuracy, ranking | `GET /api/vendor-performance`, `.../{vendor_id}` | `VendorPerformancePage` / `VendorPerformanceDetailPage` |
| Integrations | WhatsApp / Gmail / Google Sheets status + test connection | `GET /api/integrations/{whatsapp,gmail,google-sheets}/status`, `POST .../test-connection`; `GET/POST /api/whatsapp/webhook` (Meta-facing) | `IntegrationStatusPage` (via Settings) |
| Settings | Account info, change password | (reuses `/api/auth/*`) | `SettingsPage` |

**Deliberate absences:** there is no vendor CRUD screen (no `vendors.py` route, no `VendorsPage`, no `vendors.js`) — vendors are auto-created and auto-coded from their first uploaded file, never managed manually. A read-only "Document Inbox" page was removed as unused; the underlying tracking (`backend/app/documents/` — the `IncomingDocument` model recording every upload's lifecycle and dedupe by WhatsApp/Gmail message-id) is untouched and still used internally by every channel.

**Purchase Orders here vs. the legacy CLI:** the web app's `VendorPurchaseOrder`/`VendorPurchaseOrderItem` are a distinct, newer concept keyed off `VendorSelection` — separate from the legacy CLI's PO-based matching pipeline (`core.services.purchase_order_service`, untouched).

---

## 4. Key Concepts

### Vendor Code

Every vendor's **permanent identifier** is its Vendor Code — not a WhatsApp number, since all vendors message the same shared WhatsApp Business number and a sender's phone number can never tell them apart (`core/services/vendor_code_service.py`).

**Format:** first two letters of the vendor's name, uppercased, plus `_CT`; colliding names get a numeric suffix (`AR_CT`, `AR_CT_2`, `AR_CT_3`, …). E.g. Arvind Auto Parts → `AR_CT`, North End → `NO_CT`, Lumax → `LU_CT`.

**Onboarding flow:**

1. A vendor's *very first* file may be named with their real company name (e.g. `Arvind Auto Parts.xlsx`) — the system auto-creates the vendor and auto-generates + permanently stores its code. The code is shown in the upload result and the import history's "Vendor Code" column, so your team can hand it to the vendor.
2. From the **second** upload onward the vendor prefixes their filename with the code, e.g. `AR_CT_Inventory.xlsx` — identical for manual uploads and WhatsApp attachments.
3. A filename carrying a code-shaped prefix that matches no vendor (typo, never-assigned code) is **rejected** with a clear error rather than silently imported under the wrong vendor.

`Vendor.whatsapp_number` still exists as contact metadata (a new vendor's first WhatsApp inventory message links their number to the auto-created record), but it is not what identifies which vendor sent a file.

### Own Stock priority (Bijvasan)

The company's own stock is a vendor named **`Bijvasan`**. Any vendor whose name matches is automatically treated as own stock the moment its inventory is uploaded — no database flag to set, no special UI, no extra step.

Matching is **case-insensitive** and **whole-word**, so a slight rename doesn't silently switch priority off: `Bijvasan`, `BIJVASAN`, `Bijvasan Warehouse`, `Main Bijvasan Depot` all count; an unrelated name merely containing the letters (`Bijvasannual Traders`) does not.

During Automatic Vendor Selection, own stock is **always allocated first**, whatever the strategy; only the remaining shortfall goes to the strategy, which only sees external vendors. Example — customer needs 100 pcs; Bijvasan 40, Vendor A 35, Vendor B 50:

```
Bijvasan -> 40   (own stock, taken first)
remaining 60 split across Vendor A + Vendor B by the chosen strategy
```

If Bijvasan alone covers the request (Bijvasan 120, customer 100), the result is `Bijvasan -> 100` and no external vendor is used.

Configurable via `OWN_STOCK_VENDOR_NAME` (default `Bijvasan`; blank disables name-based detection). The `Vendor.is_own_stock` DB flag also still works — a vendor is own stock if EITHER matches. See `core/services/own_stock.py`.

### WhatsApp command routing

Both Vendor Inventory **and** Customer Order files arrive on the same shared WhatsApp Business number, so before sending a file the sender texts a one-word routing command:

```
Vendor     -> the next file is imported as Vendor Inventory
Customer   -> the next file is imported as a Customer Order
```

How it works:

1. The latest valid command is remembered **per WhatsApp number** (`whatsapp_pending_commands` table — one row per number, so many users can interact at once without interfering).
2. That number's next Excel file runs the matching **existing** import workflow unchanged (Vendor Inventory still resolves the vendor from the filename's Vendor Code).
3. After the file is processed — success or failure — the stored command is **cleared**; every file needs a fresh command.
4. A file with **no** prior command is **not** imported; the app replies with the instruction to send `Vendor` or `Customer` first.
5. Any other unrecognised text gets the same instruction reply and is otherwise ignored.

This is a thin routing layer in front of the unchanged imports — `backend/app/integrations/whatsapp/commands.py` is the command registry, so adding `Invoice`/`Purchase Order`/etc. later is a one-line change there. The app sends **short text replies only** (command prompts/confirmations, via the same Graph API token within WhatsApp's 24h service window) — it never sends business documents or anything to a vendor. Gmail-based Customer Order import needs no command.

---

## 5. How Files Enter the System

Three channels, all converging on **one** Document Processing Engine, so automation and a human clicking "Upload" are indistinguishable to everything downstream:

| Channel | Entry point | Trigger |
|---|---|---|
| **Manual upload** (web) | route → `backend/app/services/{inventory,customer_order,delivery,invoice}_service.py` | a logged-in user clicking Upload |
| **WhatsApp** | `POST /api/whatsapp/webhook` → `document_worker` (FastAPI `BackgroundTasks`) | Meta pushes an attachment |
| **Gmail** | scheduler → `email_worker.poll_gmail_inbox` | the periodic poll |

### The Document Processing Engine (the spine)

`backend/app/services/document_processor/processor.py` — `process_document(source, file_path, metadata, session)` runs the same steps for every file:

```
save bytes → record IncomingDocument (RECEIVED)      [staging.py, documents/service.py]
   → validate (extension/size)                        [validator.py]
   → classify (decide document_type)                  [detector.py]
   → dispatch (call the right core import)            [dispatcher.py]
   → set status (PROCESSED / FAILED / NEEDS_REVIEW / SKIPPED_DUPLICATE)
   → move file to processed/ or failed/               [staging.py]
   → return ProcessingResult (type, vendor, rows, status, message)
```

**Classification is driven by the source** — the key routing rule:

- **MANUAL** → the explicit `document_type_hint` from the endpoint the user clicked (Inventory / Customer Order / Delivery / Invoice).
- **WHATSAPP** → the sender's stored text command (`Vendor` / `Customer`, §4).
- **EMAIL (Gmail)** → file format: spreadsheet → Customer Order, PDF → Vendor Invoice. (The Gmail poll downloads each unread message's Excel attachments, dropping the trailing two when more than two are attached.)

**Dispatch** maps the decided type to the *existing* `core/services` function — it never reimplements logic:

- `VENDOR_INVENTORY` → `inventory_import_service.run_import` (+ auto-onboard a new vendor by filename if no code)
- `CUSTOMER_ORDER` → `customer_order_service.run_customer_order_import`
- `DELIVERY` → `vendor_delivery_service.run_vendor_delivery_import`
- `VENDOR_INVOICE` → `vendor_invoice_verification_service.run_invoice_verification`

### Google Sheets sync

`backend/app/integrations/google_sheets/sync_service.py`: after every inventory import (manual or WhatsApp), pushes that vendor's active inventory to its own worksheet in a shared Google Sheet. Event-triggered from the inventory dispatch path, not polled. Authenticates with the **same OAuth credentials as Gmail** (no service account). Failures are logged and never break the import.

### Notifications

`backend/app/notifications/`: workers `publish()` an event to an in-memory buffer after each import/sync; the frontend polls `GET /api/notifications` every 5s (`IntegrationNotifications.jsx`) and shows toasts. No DB — transient only.

---

## 6. Environment Variables for Automation

All opt-in; with everything left unset/false the app runs fully in manual mode — selection and PO download still work, only the automatic channels/emails are off. All documented in `backend/.env.example`; setup walkthroughs in `DEPLOYMENT.md`.

| Variable | Purpose | Default |
|---|---|---|
| `WHATSAPP_ENABLED` | Enable the WhatsApp receive webhook | `false` |
| `GMAIL_ENABLED` / `ENABLE_EMAIL_AUTOMATION` | Enable the Gmail poll (`GMAIL_POLL_INTERVAL_SECONDS` sets the interval) | `false` |
| `ENABLE_GOOGLE_SHEETS_SYNC` | Enable per-vendor Google Sheets sync after inventory imports | `false` |
| `OWN_STOCK_VENDOR_NAME` | Vendor name treated as own stock (allocated first); blank disables name-based detection | `Bijvasan` |
| `ALLOCATION_REPORT_EMAIL` | Internal address for the post-auto-selection Allocation Report; unset = email skipped | *(unset)* |
| `ENABLE_PO_EMAIL` | `true` to also email each generated PO to `PURCHASE_TEAM_EMAIL` (POs are generated + downloadable either way) | `false` |
| `PURCHASE_TEAM_EMAIL` | Internal address POs are emailed to — never a vendor | *(unset)* |
| `COMPANY_NAME` / `COMPANY_ADDRESS` / `COMPANY_PHONE` / `COMPANY_EMAIL` | Printed on every generated Purchase Order | *(unset)* |

The Allocation Report email and PO generation have no separate on/off switch — they run whenever Automatic Vendor Selection runs (only the internal PO email is gated by `ENABLE_PO_EMAIL`).

---

## 7. Architecture & Folder Map

```
pythonscript/
├── core/                         THE BRAIN -- shared, DB-backed business logic
│   ├── db.py                     Engine/session; SQLite local, Postgres if DATABASE_URL set
│   ├── models.py                 ALL SQLAlchemy tables (vendors, inventory, orders, POs, ...)
│   ├── hashing.py                SHA-256 of a file -> duplicate-import detection
│   ├── logging_setup.py          Shared logging configuration
│   ├── ingestion/                csv_reader, excel_reader, column_detector, types
│   └── services/                 The actual operations:
│       ├── inventory_import_service.py       vendor inventory import
│       ├── part_resolution_service.py        row -> canonical Part
│       ├── vendor_service.py / vendor_code_service.py / own_stock.py
│       ├── vendor_comparison_service.py      compare_vendors(_for_order) + to_workbook
│       ├── customer_order_service.py         persists uploaded customer orders
│       ├── vendor_selection_service.py       multi-vendor allocation per order line
│       ├── rules/                            auto-selection strategies + engine
│       │                                     (highest_quantity, minimum_vendors,
│       │                                     combination -- engine applies allocations
│       │                                     via the same upsert_selection manual uses)
│       ├── purchase_order_generation_service.py   web-app POs from VendorSelection
│       ├── invoice_extraction_service.py     PDF extraction (pdfplumber)
│       ├── vendor_invoice_verification_service.py verify vs. VendorSelection, mirror
│       │                                          into VendorDeliveryItem
│       ├── vendor_delivery_service.py        delivery uploads (web app)
│       ├── delivery_tracking_service.py      ordered vs delivered vs pending
│       ├── vendor_performance_tracking_service.py fulfillment %, accuracy, ranking
│       ├── dashboard_service.py              read-only aggregation for the Dashboard
│       └── purchase_order_service.py, delivery_import_service.py,
│           gap_analysis_service.py, alternative_vendor_service.py,
│           vendor_performance_service.py     (legacy CLI pipeline services)
│
├── backend/                      FastAPI WEB APP (thin wrapper over core/)
│   ├── requirements.txt
│   ├── scripts/                  create_admin.py, migrate_schema_updates.py
│   └── app/
│       ├── main.py               Entry point: builds app, lifespan (bootstrap + scheduler)
│       ├── core/config.py        Reads backend/.env (JWT, CORS, company info, flags)
│       ├── database/session.py   get_db() request-scoped session over core.db
│       ├── auth/                 JWT login, bcrypt, User/RevokedToken models --
│       │                         deliberately separate from business logic
│       ├── api/routes/           One thin router per feature: validate input, call
│       │                         core/services, shape JSON. No business rules here.
│       ├── schemas/              Pydantic request/response models (API layer only;
│       │                         never imported by core/services)
│       ├── services/             Framework glue (save upload to disk, then call core),
│       │   └── document_processor/  staging -> validate -> classify -> dispatch
│       ├── documents/            IncomingDocument model + lifecycle service (internal
│       │                         upload-status bookkeeping shared by every channel)
│       ├── integrations/         whatsapp/ (Cloud API client, webhook, commands,
│       │                         command_store, parser, outbound -- short text replies
│       │                         only), gmail/ (IMAP + OAuth clients behind one
│       │                         interface, mailer), google_sheets/ (sync service)
│       ├── workers/              document_worker.py (WhatsApp path), email_worker.py
│       │                         (Gmail poll), scheduler.py (in-process APScheduler --
│       │                         no separate worker process/queue to deploy)
│       └── notifications/        In-memory toast broker + emitters (no DB)
│
├── frontend/src/                 React (Vite) SPA
│   ├── api/                      axios client + one file per resource -- the UI never
│   │                             talks to the database directly, only this layer
│   ├── context/                  AuthContext (JWT in localStorage), ToastContext
│   ├── components/               Layout (workflow-ordered sidebar + logout), Modal,
│   │                             StatusPill, EmptyState, ProtectedRoute,
│   │                             DashboardFilterBar, IntegrationNotifications
│   └── pages/                    One page per workflow step (see module table, §3)
│
├── inventory_import.py, order_matching.py, po_generator.py,
│   delivery_import.py, gap_analysis.py, alternative_vendor.py,
│   vendor_performance.py, summary_report.py, run_pipeline.py,
│   ordermatching.py              Legacy CLI pipeline (see §9)
├── input.csv                     CLI customer order input
├── raw_files/                    CLI vendor inventory inputs
├── delivery_files/               CLI vendor delivery inputs
├── output/  charts/              CLI-generated reports and charts
├── database/app.db               Local SQLite -- shared by CLI and web app
├── tests/                        pytest unit tests + fixtures
├── requirements.txt              Root Python dependencies
└── render.yaml / DEPLOYMENT.md   Deploy config / docs
```

**Design decisions worth knowing:**

- **No `repositories/` layer** — `core/services/*` already combines the repository + service roles for these simple CRUD/import operations; an empty pass-through wrapper would be indirection with no behavior. One can be introduced per-module later if real query complexity appears.
- **Multi-user readiness without building it:** the `User` model has a `role` column (default `"admin"`) that nothing enforces yet — adding Admin / Purchase Team / Warehouse / Manager permissions later means reading that column in a dependency, not changing `core/services/*` or the schema. Auth lives entirely under `backend/app/auth/`, never inside a business service, so SSO or multi-tenant auth can be swapped in without touching vendor/inventory logic.
- **`backend/app/core/` is config only** — not to be confused with the root-level `core/` business package.

### Boot sequence

`python -m uvicorn backend.app.main:app` →

1. `backend/app/core/config.py` loads `backend/.env` (JWT secret, CORS, company details, `ENABLE_*` flags).
2. `main.py` builds the FastAPI app, adds CORS, registers the routers, and defines a **lifespan** that:
   - runs `_bootstrap_admin_if_configured()` — creates the admin user *only if* zero users exist and `ADMIN_USERNAME`/`ADMIN_PASSWORD` are set;
   - runs `start_scheduler()` — an in-process APScheduler that polls Gmail every `GMAIL_POLL_INTERVAL_SECONDS`, **only if `GMAIL_ENABLED`**. (Google Sheets isn't polled — it's event-triggered; WhatsApp is webhook-driven.)
3. `core/db.py` picks the database: `DATABASE_URL` set → Postgres; unset → local `database/app.db`. Tables are created via `Base.metadata.create_all` on first session.

The frontend is a static SPA; it talks to the backend only through `VITE_API_BASE_URL` (`frontend/src/api/client.js`), which also attaches the JWT from localStorage; `ProtectedRoute` guards pages.

---

## 8. Tests

```bash
pytest
```

Run from the project root. Prerequisites for everything in this repo: Python 3.11+ (developed on 3.13), Node.js for the frontend, and pip — no separate database server; SQLite is created automatically on first run.

---

## 9. Legacy CLI Pipeline

The older, pre-web pipeline at the repo root. It calls the **same `core/services`** and shares `database/app.db` — but it is file-in/file-out (reads `raw_files/`, `input.csv`, `delivery_files/`; writes `output/*.xlsx` + `charts/*.png`) and has no UI.

**Status: only steps 1–2 are wired up end-to-end in the CLI.** The vendor-selection step between them was built as part of the *web app* (manual + automatic, §2 step 4), not for the CLI — so the CLI's steps 3–8 currently have no valid input. `order_matching.py` writes `output/vendor_comparison_report.xlsx` (every vendor listed, none chosen), while `po_generator.py` still expects the old chosen-vendor-per-line `output/matching_output.csv` that nothing produces anymore (the old file was archived to `output/matching_output.csv.legacy`).

```bash
python inventory_import.py     # 1. import every vendor file in raw_files/
python order_matching.py       # 2. -> output/vendor_comparison_report.xlsx

# --- CLI vendor selection: not built (exists only in the web app) ---

python po_generator.py         # 3. chosen vendor per line -> Purchase Orders
python delivery_import.py      # 4. import every file in delivery_files/
python gap_analysis.py         # 5. ordered vs delivered -> output/gap_report.xlsx
python alternative_vendor.py   # 6. -> output/alternative_vendor_report.xlsx
python vendor_performance.py   # 7. -> output/vendor_dashboard.xlsx + charts/*.png
python summary_report.py       # 8. -> output/summary_report.xlsx
```

Or run steps 1–2 in one go: `python run_pipeline.py` — a convenience wrapper that runs its `scripts` list in order, printing a header before each and stopping on any non-zero exit. Steps 3–8 are commented out in that list (with a note why) rather than deleted, so re-enabling them once CLI vendor selection exists is a one-line change.

Notes:

- Put vendor inventory files in `raw_files/`, the customer order at `input.csv`, and delivery files in `delivery_files/` (sample fixtures included — see below). `output/`, `charts/`, and `database/` are created automatically; their contents are git-ignored, so a fresh clone starts empty until you run the pipeline.
- Ordering, once CLI vendor selection exists: 1–2 before 3; 4 before 5–7; 8 any time after 4 (it aggregates from the database, not from another report's file).
- Every script is independently runnable and re-runnable:
  - **`po_generator.py`** creates one `PurchaseOrder` per vendor from MATCHED/PARTIAL lines with auto-numbered PO numbers (`PO001`, `PO002`, …); re-running against an unchanged input is a no-op.
  - **`delivery_import.py`** scans every `.csv`/`.xlsx`/`.xlsm`/`.xls` in `delivery_files/`, auto-detects Vendor / PO Number / Part Number / Delivered Quantity columns (case- and spacing-insensitive, e.g. "PO No", "Delivered Qty"), and validates each row against the vendor, the PO, and the part actually ordered on that PO. Invalid rows are logged to `delivery_import_errors` and skipped; valid rows become `DeliveryItem` records. Re-importing an unchanged file is skipped as a duplicate.
  - **`gap_analysis.py`**, **`alternative_vendor.py`**, **`vendor_performance.py`**, **`summary_report.py`** all compute directly from the database (`purchase_order_items` + `delivery_items`) — none depends on another report's output file.

### Sample / test data

- `raw_files/VendorD_Spares.csv` — a small supplementary fixture (not one of the original three vendor files) added so `alternative_vendor.py` has a real cross-vendor match to demonstrate for part `111`; it doesn't change the original vendors' data.
- `delivery_files/` — samples covering all three gap statuses (fully / partially / not delivered), plus `bad_delivery_example.csv`, which deliberately contains one row for every validation failure `delivery_import.py` can raise (unknown vendor, unknown PO, PO/vendor mismatch, unknown part, part not on that PO, invalid quantity, blank fields) so you can see the error logging in action.

### `ordermatching.py` (original prototype)

The original standalone prototype for order matching, written before the database-backed architecture existed. It still runs on its own (no database: scans `raw_files/` CSVs in memory, matches `input.csv`, writes `matching_output.csv`), but it is **not** part of the pipeline — use `order_matching.py` for current work.

---

## 10. Technology Stack & Roadmap

**Current:** Python 3 · SQLAlchemy + SQLite (Postgres/Neon in production via `DATABASE_URL`) · `openpyxl`/`xlrd` for CSV/Excel · `pdfplumber` for invoice PDFs · FastAPI + JWT/bcrypt (`backend/`) · React + Vite (`frontend/`) · APScheduler (in-process Gmail poll) · Meta WhatsApp Cloud API (receive files, send short text replies only) · Gmail (IMAP or OAuth) · Google Sheets sync.

**Not yet in scope (later phases):**

- CLI-side vendor selection (re-enabling CLI pipeline steps 3–8)
- Vendor-direct PO delivery (today POs are internal-only, never sent to vendors)
- Real-time stock updates
- Persisted multi-order customer order tracking lifecycle in the web UI
- Role-based access control (the `User.role` column exists; nothing enforces it yet)
- Multi-user self-service account management (schema supports it; accounts are created via `backend/scripts/create_admin.py`)
- PostgreSQL locally / Pandas / chart libraries — only if and when current approaches become bottlenecks

---

## Goal

A centralized inventory and order fulfillment system that consolidates vendor inventories, matches customer orders to vendors automatically, generates vendor-specific purchase orders, tracks delivery status and shortages, finds alternative vendors for pending quantities, and measures vendor fulfillment accuracy over time — reducing manual effort and providing actionable insight into vendor performance.
