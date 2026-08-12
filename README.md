# Vendor Inventory & Order Fulfillment Automation

A vendor inventory sourcing and order-fulfillment system for an auto-parts trading business (business timezone: IST). Vendors send stock files, customers send order files, vendors later send invoices — all over WhatsApp, Gmail or the web app — and the system consolidates inventory, allocates every order across vendors, generates reports and purchase orders, verifies invoices, tracks deliveries and scores vendor reliability.

**The one-line mental model:**

> A file arrives (WhatsApp / Gmail / manual upload) → staged & classified → imported by the matching `core/services` importer into ONE shared database → customer orders are matched against EVERY vendor's live remaining stock → automatic vendor selection reserves stock customer-by-customer → allocation reports go out on WhatsApp → invoices are verified against allocations and feed Delivery Tracking + Vendor Performance.

The project has **two front doors sharing one brain**:

- **`core/`** — pure business logic + the database models. No web code, importable on its own.
- **Web app** — FastAPI backend (`backend/`) + React frontend (`frontend/`) wrapping the same `core/` services. Production: frontend on Vercel, backend on Render, database on Neon Postgres (SQLite locally).

---

## 1. Quick Start (Web App)

Two terminals, both left running:

```powershell
# Terminal 1 -- backend (run from the PROJECT ROOT, never from backend\)
cd D:\Downloads\pythonscript
venv\Scripts\activate
pip install -r backend\requirements.txt          # first time only
python -m backend.scripts.create_admin --username admin --password <8+ chars>   # first time only
python -m uvicorn backend.app.main:app --reload --port 8000
```

```powershell
# Terminal 2 -- frontend
cd D:\Downloads\pythonscript\frontend
npm install                                      # first time only
copy .env.example .env                           # first time only
npm run dev                                      # open http://localhost:5173
```

All configuration lives in `backend/.env` (see `backend/.env.example` for every variable with documentation). Production reads the same variables from Render's Environment tab. `GET /api/version` on the deployed backend reports exactly which git commit production is running.

---

## 2. How a File Travels Through the System

Every document — regardless of channel — passes through the same engine:

```
arrival (WhatsApp / Gmail / manual upload)
   │  backend/app/workers/document_worker.py   (WhatsApp)
   │  backend/app/workers/email_worker.py      (Gmail)
   │  backend/app/services/inventory_service.py (manual)
   ▼
staging.save_incoming_bytes()        file saved under uploads/incoming/<source>/
   ▼
processor.process_document()         backend/app/services/document_processor/processor.py
   ▼
detector.classify()                  WHICH importer should run?
   │    WhatsApp  -> decided by the sender's routing command (vendor/customer/invoice)
   │    Gmail     -> spreadsheet = Customer Order, PDF = Vendor Invoice
   │    Manual    -> the page the user uploaded from
   ▼
dispatcher.dispatch()                backend/app/services/document_processor/dispatcher.py
   │    resolves vendor/customer identity, then calls the core importer
   ▼
core importer                        core/services/inventory_import_service.py
   │                                 core/services/customer_order_service.py
   │                                 core/services/vendor_invoice_verification_service.py
   ▼
ONE shared database                  core/models.py
   ▼
post-commit outputs                  toasts, Google Sheet, consolidated workbook,
                                     automatic vendor selection, WhatsApp reports
```

Success notifications are published **only after the database transaction has committed** (`document_worker.py` / `email_worker.py`) — never before.

---

## 3. Vendor Inventory Flow (WhatsApp)

### 3a. Registered numbers — the permanent identity layer (`integrations/whatsapp/registry.py`)

**A vendor's (or customer's) own WhatsApp number can be registered once; from then on the NUMBER alone is the identity:**

```
WhatsApp number  ->  Vendor (or Customer)  ->  Vendor/Customer Code
917XXXXXXXXX     ->  MAHINDRA              ->  MA_CT
```

- The vendor just sends the file — **no command, no caption, no filename rules, no grouping window**. `stock.xlsx`, `final.xlsx`, `abc123.xlsx` all import as MAHINDRA.
- A stray caption is **ignored** for registered numbers (the registry always wins) — it can never create or misfile a vendor. Texts ("good morning sir") are ignored too.
- Vendor numbers: spreadsheet → Vendor Inventory, PDF → Vendor Invoice. Customer numbers: spreadsheet → Customer Order (feeds automatic vendor selection exactly as today).
- Multiple numbers per party are supported; each number belongs to exactly ONE party (DB-enforced) — a number never sends both vendor and customer files.
- The sender gets a simple reply (✅ imported / ⚠️ partial / ❌ could-not-read); the admin gets the full technical detail through the notification mirror.
- Unregistered numbers are untouched — they keep the command/caption flow below. The Founder/admin number can never be registered as a party (it uploads on behalf of many vendors).
- Bulk-register from a contact list: `python -m backend.scripts.register_vendor_numbers contacts.xlsx [--dry-run]`.
- **Founder-managed over WhatsApp** (`integrations/whatsapp/contact_import.py`): an admin texts `register` (or captions a file `contacts`) and sends an Excel of Vendor Name + WhatsApp number(s). The list is AUTHORITATIVE: each listed vendor's numbers are REPLACED, a number owned by another vendor is re-pointed, rows sharing one number are ONE vendor (first row wins, no duplicate vendor), unknown names are onboarded with a code — and the bot replies with exactly what changed. A column headed "updated …" wins over an old PHONE column.
- **Several admin numbers**: `WHATSAPP_ADMIN_PHONE_NUMBER` is comma-separated — every listed number receives all founder-facing messages (workbook, allocation reports, mirrored notifications, daily summary) and may use the admin commands.
- **Part-number matching ignores ALL special characters** (`column_detector.normalise_part_number`): a vendor's `DM-BP/1001$` and a customer's `DMBP1001` are the same part for comparison/mapping/allocation — only letters and digits count.
- **Google Sheet daily reset** (`sync_service.reset_sheet_for_new_day`, `GOOGLE_SHEETS_DAILY_RESET_*`): every morning, vendor tabs without a same-day upload are removed — the Sheet only ever shows today's stock. Hand-made tabs are never touched.

**Daily cycle on top of the registry** (`integrations/whatsapp/daily_stock.py`, all times IST via `workers/scheduler.py`):

| When | What |
|---|---|
| `WHATSAPP_DAILY_REQUEST_TIME` (09:00) | Approved template ("please share your stock") to every registered vendor number; needs `WHATSAPP_DAILY_REQUEST_ENABLED=true` + an approved Meta template |
| all day | files auto-import by number; sheet/workbook update as usual |
| `WHATSAPP_DAILY_SUMMARY_TIME` (11:00) | "📊 Received: X / Y + pending list" to the admin number (plain text, no template) |
| admin texts `send reminder` | reminder template to still-pending vendors only, then a confirmation listing who was nudged |
| `WHATSAPP_AUTO_REMINDER_TIME` (optional) | the same reminder, automatically |

"Submitted today" = an `InventoryImport` since IST midnight with status COMPLETED / COMPLETED_WITH_ERRORS / SUPERSEDED — a FAILED attempt keeps the vendor on the pending list.

### 3b. Unregistered numbers — supplied-name flow

**Vendor identity comes from the NAME the sender supplies — never from the filename.** The same vendor may send `stock.xlsx`, `August_Final.xlsx`, `abc.xlsx`: all belong to one vendor record with one permanent Vendor Code.

```
sender texts "vendor"                commands.parse_command() -> command_store.set_command()
sender sends file                    caption = vendor name  (e.g. "MAHINDRA")
   │
   ├─ caption present  ─────────────► that name IS the vendor
   ├─ no caption, but a name was
   │  supplied in the last 10 min ──► grouped under the SAME vendor automatically
   │                                  (vendor_memory.recall — grouping window)
   └─ no caption, no memory ────────► file is HELD (pending_vendor_files) and the
                                      bot ASKS: "Which vendor is this from?"
                                      The next text = the name; all held files import.
   ▼
dispatcher._resolve_or_onboard_vendor(name)
   │    existing name (case-insensitive)  -> same vendor_id, same vendor_code, always
   │    new name                          -> vendor created + code generated ONCE
   │                                         (vendor_code_service.generate_vendor_code:
   │                                          "Shree Balaji Motors" -> SBM_CT; collisions
   │                                          get longer name-derived stems, never _2)
   ▼
inventory_import_service.run_import(vendor_id, file, session)
   1. size/extension checks, sha256 content hash
   2. header detection anywhere in the file (metadata rows above are skipped)
      -- real-world aliases: Part No/PartNo/Material Code/SKU...,
         Quantity/Current Stock/Current St/Closing Stock/Balance...  (column_detector.py)
      -- money columns (MRP, Rate, Price, Closing Value, Float Stock) can NEVER
         become quantity
   3. DUPLICATE check: same content as the vendor's active import -> skipped
   4. row loop: blank part / unparseable qty / NEGATIVE qty -> rejected per-row
      with a reason in Import History; parts resolved into the canonical Part
      master (part_resolution_service.resolve_part -- race-safe: two files
      sharing a part number can import concurrently without crashing)
   5. ZERO valid rows -> the import FAILS loudly; it never silently replaces
      the vendor's previous good stock
   6. supersession: the new batch becomes ACTIVE, the previous one SUPERSEDED
      (history keeps every upload, identifiable by original filename)
```

**AI rescue (`AI_FALLBACK_ENABLED`)** — when the deterministic parser cannot recognize the columns at all (`backend/app/ai/fallback.py`):

```
deterministic FAILED
   -> the model (NVIDIA-hosted, AI_MODEL) reads a token-bounded sample
   -> it may ONLY propose a column mapping ("part_number"='Code', quantity='Value')
   -> strict validator: money columns rejected, confidence floor
      (core/services/normalized_validation.py)
   -> cross-check: every row the model read must exist in the file under that
      mapping -- one hallucinated value refuses the rescue
   -> run_import re-reads the WHOLE file deterministically with the mapping
      (all values come from the file, never from the model)
```

If everything fails, the file stays FAILED with the real reason in the toast and Import History.

---

## 4. Outputs After a Vendor Import

- **Google Sheet** (`backend/app/integrations/google_sheets/sync_service.py`): the vendor's worksheet — named by **Vendor Code** (`MA_CT`), never the filename — is overwritten with an **EXACT COPY of the vendor's own file**: original columns, original order, original values (MRP/Rate/dates included, nothing filtered). Built from `inventory_import_service.get_active_raw_table()`. Your hand-maintained hub tabs are never touched.
- **Consolidated `Vendor_Inventory.xlsx`** (`core/services/vendor_inventory_workbook.py`): one tab per vendor (same exact-copy rule, same code-named tabs), sent to `WHATSAPP_ADMIN_PHONE_NUMBER`. **Debounced** — a batch of many vendor files produces ONE workbook ~20s after the batch goes quiet (`inventory_output.request_consolidated_send`). A vendor with no active inventory gets no tab.
- The workbook and Sheet are **outputs only** — nothing ever reads them back; the database is the single source of truth.
- **WhatsApp notification mirror** (`backend/app/integrations/whatsapp/notification_forwarder.py`): every toast the web UI shows — import success/failure, workbook and Sheet updates, allocation outcomes, Gmail poll errors — is also sent as a WhatsApp text (✅/⚠️/❌/ℹ️ + the same detail lines) to `WHATSAPP_ADMIN_PHONE_NUMBER`. Best-effort on its own thread; `WHATSAPP_FORWARD_NOTIFICATIONS=false` turns it off.

---

## 5. Customer Order Flow

```
Gmail (backend/app/workers/email_worker.py):
   -- only mail from GMAIL_ALLOWED_SENDERS is read (everything else untouched)
   -- only mail received TODAY (IST) is processed (GMAIL_PROCESS_TODAY_ONLY)
   -- only attachments starting with GMAIL_ATTACHMENT_PREFIX (e.g. purchase_order)
      are extracted -- one match alone, several matches all
   -- each extracted file is imported under GMAIL_SAVE_ATTACHMENT_AS (fixed name);
      duplicate protection stays content-based, so different POs under the same
      name import separately and exact re-sends are skipped
WhatsApp: text "customer", then send order file(s)

   ▼
customer_order_service.run_customer_order_import()
   -- header detection understands metadata blocks (PO ID, "SKU: 7", "Lines: 7",
      "Q-ty: 8") and NEVER uses them as quantities; only the real line-item
      QTY column counts
   -- quantity aliases: Quantity/QTY/Requested Quantity/Requested Qty/Order Qty/
      Required Qty; part aliases include Part Number/Part No/PartNo/Material Code
   -- a file with NO quantity column -> NEEDS_REVIEW, no rows invented
```

---

## 6. Vendor Comparison, Automatic Selection & Inventory Consumption

**The stock mechanism (the heart of the system):**

```
VendorInventory.quantity_available   = the vendor's imported stock (never edited)
VendorSelection                      = the reservation LEDGER (one row per allocation)
live remaining                       = quantity_available - SUM(active reservations)
                                       computed fresh in SQL on every read
                                       (vendor_stock_service.remaining_quantity)
```

Customer-by-customer consumption follows automatically: Customer 1's allocation reduces what Customer 2 can see and get.

```
Example: V01 has P-1001 x10, V02 has P-1001 x5
  Customer 1 wants 7  -> V01=7            remaining V01=3, V02=5
  Customer 2 wants 6  -> V01=3 + V02=3    remaining V01=0, V02=2   (sees 8, not 15)
  Customer 3 wants 4  -> V02=2, short 2   remaining 0  -- nothing invented
```

- `vendor_comparison_service.compare_vendors_for_order()` — every vendor holding the part, showing live REMAINING stock.
- `rules/engine.run_automatic_vendor_selection()` — Own-Stock vendors first (`OWN_STOCK_VENDOR_NAME`, comma-separated — e.g. `Bijwasan,Mansarovar`; among them, biggest available stock first), then the `combination` strategy: vendors ranked by stock, each draw capped at live remaining, splitting across vendors as needed; partial fulfilment allowed (never all-or-nothing).
- `vendor_selection_service.upsert_selection()` — takes a database row lock BEFORE computing remaining, so two orders processed at the same instant can never over-allocate (stock 10 + two orders of 6 = 6+4, never 12 — verified on production Postgres).
- Unfulfilled lines always show `Selected Qty 0` with the reason ("Insufficient vendor stock (short N)") — never blank.

**Automation ("Combined" mode, `backend/app/integrations/whatsapp/allocation_batch.py`):** every successfully imported customer order is queued; after order imports go quiet (~20s), the engine auto-selects each order IN ARRIVAL ORDER (each customer consumes stock before the next) and ONE consolidated Excel — one worksheet per order (`Order_22`, `Order_23`…) — is sent to the Founder's WhatsApp. No clicks needed. (`WHATSAPP_AUTO_ALLOCATION_ENABLED`, WhatsApp rejects ZIP files, hence a multi-sheet xlsx.)

The manual **Auto-Select** button still works and additionally sends the allocation email (`ALLOCATION_REPORT_EMAIL`) and generates Purchase Orders.

---

## 7. Invoices, Delivery Tracking & Vendor Performance

An invoice is the vendor's **claim** of what he supplied. It is verified against what the system **ordered from him** (his allocations) — never against his stock list:

```
1. ABC shares stock list      -> "I HAVE X=50"        (inventory import)
2. Auto-Select reserves       -> "WE ORDERED X=5"     (VendorSelection)
3. ABC sends goods + invoice  -> "I SENT X=5, Y=1"    (PDF via WhatsApp "invoice")
4. vendor_invoice_verification_service compares 3 vs 2:
      MATCHED / SHORT_SUPPLY / EXTRA_SUPPLY / MISSING_PART / UNEXPECTED_PART
```

Invoice PDFs are parsed by `invoice_extraction_service.py` (tables or text lines; the vendor is read from a "Vendor:/Supplier:" label and must resolve to a known vendor).

**Every verified invoice line is also mirrored into delivery records** (`VendorDeliveryItem`) — so the invoice alone drives:

- **Delivery Tracking tab**: Ordered vs Delivered vs Short per vendor+part, status COMPLETE / PARTIAL / NOT_DELIVERED, Daily/Monthly charts (`delivery_tracking_service.py`).
- **Vendor Performance pages**: who reliably delivers what he was allocated — chronic SHORT_SUPPLY means the vendor's stock lists can't be trusted.

A separate manual delivery-file upload exists on the Delivery Tracking tab for warehouse-side goods-receipt checking (columns: Vendor, Part Number, Delivered Qty, optional Delivery Date), but it is optional — invoices already feed the tracking.

---

## 8. WhatsApp Conversation Reference

| You send | System does |
|---|---|
| `vendor` | next file(s) = vendor inventory. Reply asks you to put the vendor name in the caption or send it after the file. |
| file + caption `MAHINDRA` | imports for MAHINDRA (existing code reused / new vendor onboarded once) |
| file, no caption | grouped under the vendor named in the last 10 min; otherwise held + asked |
| plain text after held file(s) | treated as the vendor name; all held files import |
| `customer` | next file(s) = customer orders (persistent command) |
| `invoice` | next file(s) = invoice PDFs |
| any other text | instruction reply listing the commands |

The **grouping window** (`WHATSAPP_GROUPING_WINDOW_MINUTES`, default 10): all files from the same number within the window keep the same command and vendor automatically; every file restarts the window; a new caption switches vendors; after expiry the conversation starts fresh.

---

## 9. Settings Page

- Change password; Integration Status (WhatsApp health, Gmail poll state, Sheets test).
- **Danger Zone — Delete File Data** (`backend/app/services/data_purge_service.py`): one-click purge with four scopes — All / Vendor files / Customer files / Invoice files. Double confirmation (dialog + typed DELETE). Master data always survives: vendors & customers keep their codes, users, WhatsApp/Gmail/Sheets settings are never touched.

---

## 10. Key Environment Variables (`backend/.env` / Render)

| Group | Variables |
|---|---|
| Database | `DATABASE_URL` |
| Auth | `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `JWT_SECRET_KEY` |
| WhatsApp | `WHATSAPP_ENABLED`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ADMIN_PHONE_NUMBER`, `WHATSAPP_FORWARD_NOTIFICATIONS`, `WHATSAPP_GROUPING_WINDOW_MINUTES`, `WHATSAPP_WORKBOOK_DEBOUNCE_SECONDS`, `WHATSAPP_AUTO_ALLOCATION_ENABLED`, `WHATSAPP_ALLOCATION_BATCH_DEBOUNCE_SECONDS` |
| Daily stock cycle | `WHATSAPP_DAILY_REQUEST_ENABLED`, `WHATSAPP_DAILY_REQUEST_TIME`, `WHATSAPP_DAILY_SUMMARY_ENABLED`, `WHATSAPP_DAILY_SUMMARY_TIME`, `WHATSAPP_AUTO_REMINDER_TIME`, `WHATSAPP_STOCK_REQUEST_TEMPLATE`, `WHATSAPP_REMINDER_TEMPLATE`, `WHATSAPP_TEMPLATE_LANGUAGE` |
| Gmail | `GMAIL_ENABLED`, `GMAIL_AUTH_MODE=oauth`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` (one matching trio!), `GMAIL_ALLOWED_SENDERS`, `GMAIL_ATTACHMENT_PREFIX`, `GMAIL_SAVE_ATTACHMENT_AS`, `GMAIL_PROCESS_TODAY_ONLY` |
| Google Sheets | `ENABLE_GOOGLE_SHEETS_SYNC`, `GOOGLE_SHEET_ID` (credentials shared with Gmail) |
| AI | `AI_PROVIDER=nvidia`, `AI_MODEL`, `NVIDIA_API_KEY`, `AI_FALLBACK_ENABLED`, `AI_SHADOW_MODE`, `AI_TIMEOUT_SECONDS` |
| Business | `OWN_STOCK_VENDOR_NAME`, `ALLOCATION_REPORT_EMAIL`, `PURCHASE_TEAM_EMAIL` |

To mint a fresh Google refresh token (Gmail + Sheets scopes together): put the OAuth Desktop client JSON at `oauth_client.json` and run `python generate_google_token.py`; then update the trio locally AND on Render (`print_render_google_env.py` prints the exact values to copy).

---

## 11. Folder Map

```
core/                       pure business logic -- no web imports
  models.py                 all SQLAlchemy models (one shared DB)
  ingestion/                column_detector (aliases, header detection), excel/csv readers
  services/
    inventory_import_service.py      vendor stock import + raw-table export
    customer_order_service.py        order import
    vendor_code_service.py           permanent vendor codes
    customer_code_service.py         permanent customer codes
    part_resolution_service.py       canonical Part master (race-safe)
    vendor_comparison_service.py     order vs every vendor, live remaining
    vendor_stock_service.py          reservation ledger math
    vendor_selection_service.py      allocations + export workbooks (row-locked)
    rules/                           selection strategies (combination)
    vendor_inventory_workbook.py     consolidated multi-tab workbook
    invoice_extraction_service.py    PDF -> lines
    vendor_invoice_verification_service.py  invoice vs allocations + delivery mirror
    delivery_tracking_service.py     ordered vs delivered aggregation
    vendor_performance_tracking_service.py  reliability metrics
    normalized_validation.py         the AI-output gate
backend/app/
  api/routes/               REST endpoints (thin wrappers)
  services/document_processor/      staging -> detector -> dispatcher -> processor
  workers/                  document_worker (WhatsApp), email_worker (Gmail), scheduler
  integrations/             whatsapp/ (client, commands, memory, outputs), gmail/, google_sheets/
  ai/                       provider registry, NVIDIA provider, fallback (rescue), shadow
  notifications/            in-app toast broker + emitters
frontend/src/               React pages (Dashboard, Vendor Inventory, Customer Orders,
                            Vendor Comparison, Purchase Orders, Vendor Invoices,
                            Delivery Tracking, Vendor Performance, Settings)
```

---

## 12. Guarantees Worth Knowing

- The filename never decides vendor identity; vendor codes are permanent and never regenerated.
- Money columns can never become quantities — deterministic parser AND AI validator both enforce it.
- Negative stock rows are rejected per-row; zero-row imports fail loudly and never wipe good stock.
- Duplicate files (same content) are skipped; changed files supersede cleanly with full history.
- Stock is never double-allocated: the reservation ledger + row locking survive concurrent orders (verified against production Postgres).
- Notifications tell the truth: success only after commit, partial imports say Imported/Rejected with reasons, invoice toasts say Lines Matched/Discrepancies.
- The Google Sheet and workbook are exact copies of vendor files, regenerated from the database — never used as inputs.
- Purging file data never deletes vendors, customers, codes, users or integration settings.
