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

### How a column is understood (four layers, cheapest first)

The system does **not** hunt for one fixed header name. Every vendor spells things differently (`Part Number`, `Product`, `Article`, `SKU`, `Material Code`, or nothing at all), so understanding happens in layers:

| # | Layer | What it uses | Cost |
|---|---|---|---|
| 1 | **Known header names** (`column_detector.INVENTORY_*_HEADERS`) | header text matched against curated aliases | free, instant |
| 2 | **Learned formats** (`core/services/format_memory.py`) | a header layout this system has already been taught | free, instant |
| 3 | **Data-driven inference** (`core/ingestion/column_inference.py`) | the **actual values** in each column | free, instant |
| 4 | **AI rescue** (`backend/app/ai/fallback.py`) | a model reading a sample of the file | one API call, then remembered by layer 2 |

**Layer 3 is what makes unknown headers work.** It profiles every column's values and asks what the data behaves like, not what it is called:

- **part number** — alphanumeric codes, nearly all distinct, no sentences (`ABC12345`, `1654600Q1FMK`)
- **description** — words and spaces, repeats across rows (`Bearing Assembly`)
- **quantity** — numbers, mostly whole, non-negative, values repeat freely (`10`, `0`, `250`)

Header text still matters, but only as a score hint and as a **veto**: any column whose name contains price / MRP / rate / value / amount / tax / discount can never become quantity. On top of that, columns are compared **against each other** — a column that stays at a fixed ratio of a price column (e.g. `Balance` = a steady 86 % of `MRP`) is a disguised price, not stock, and is rejected however "quantity-like" its name sounds. When nothing honest survives, layer 3 declines and layer 4 decides: **the system never guesses stock**.

The same profiling also finds the real header row in files whose header names are all unrecognised (`detect_header_row_by_data`), skipping company/date metadata rows above the table — and a candidate row containing bare numbers is never mistaken for a header.

### Dual part-number columns (`find_secondary_part_columns`)

Some vendor files carry **two part numbers per row for the same physical item** — e.g. Maruti DMS exports with `Part Num` + `Root Part Num`, or `Part Number` + `Old Part No` / `OEM Part Number`. The importer:

- stores the row ONCE under the primary number (stock is never double-counted);
- registers every distinct secondary number as a `PartAlias` of the same Part;
- keeps both columns verbatim in `raw_data`, so the Google Sheet / workbook exact copies show them unchanged.

Matching is **alias-aware end to end**: Vendor Comparison (`compare_vendors`' offer index) and allocation locking (`vendor_selection_service._matchable_part_numbers`) both resolve an ordered number through the Part graph — so a customer may write **either** number (any spelling, any special characters) and hits the same inventory row and the same reservation ledger. Ordering 20 by the root number leaves only 6 for the next customer ordering by the primary number.

Spacer rows (a `' '` row between the header and the first data row, common in dealer "Part search Details" exports) are skipped; a blank row **after** data still ends the table as before.

---

## 4. Outputs After a Vendor Import

- **Google Sheet** (`backend/app/integrations/google_sheets/sync_service.py`): the vendor's worksheet — named by **Vendor Code** (`MA_CT`), never the filename — is overwritten with an **EXACT COPY of the vendor's own file**: original columns, original order, original values (MRP/Rate/dates included, nothing filtered). Built from `inventory_import_service.get_active_raw_table()`. Your hand-maintained hub tabs are never touched.
- **Consolidated `Vendor_Inventory.xlsx`** (`core/services/vendor_inventory_workbook.py`): one tab per vendor (same exact-copy rule, same code-named tabs), sent to `WHATSAPP_ADMIN_PHONE_NUMBER`. **Debounced** — a batch of many vendor files produces ONE workbook ~20s after the batch goes quiet (`inventory_output.request_consolidated_send`). A vendor with no active inventory gets no tab.
- The workbook and Sheet are **outputs only** — nothing ever reads them back; the database is the single source of truth.
- **WhatsApp notification mirror** (`backend/app/integrations/whatsapp/notification_forwarder.py`): the toasts worth knowing about away from the web UI — import success/failure (with vendor/customer name, sender and reason), Sheet failures, Gmail poll errors — are also sent as WhatsApp texts (✅/⚠️/❌/ℹ️) to every number in `WHATSAPP_ADMIN_PHONE_NUMBER`. `WHATSAPP_FORWARD_NOTIFICATIONS=false` turns it off.
  - **One message per file, not four.** Events that only make sense in the web UI are published with `mirror=False` (`broker.publish`) and never reach WhatsApp: "workbook sent to WhatsApp" / "allocation report sent" (the file itself is already in the chat) and successful Google Sheet syncs — the sheet result is folded into the import's own message as `Google Sheet: updated`. Failures of those same operations DO reach WhatsApp, since they need action.
  - **File sends are optional**: `WHATSAPP_SEND_WORKBOOK=false` / `WHATSAPP_SEND_ALLOCATION_REPORT=false` keep the chat text-only. Both downloads live on the web:
  - **Vendor Inventory → Download Workbook** (`GET /api/inventory/workbook`) — all vendors, one worksheet each.
  - **Download on any Import History row** (`GET /api/inventory/imports/{id}/export`) — just that vendor's batch as its own file, named `ND_CT_stock_13_import_42.xlsx`. Works for older/superseded batches too. **On a FAILED row the same button serves the ORIGINAL file the sender uploaded** (nothing was stored to export), so the row you most want to inspect is one click away; `ImportHistoryOut.can_download` tells the UI when a file genuinely exists.
- **Failed files are pushed to WhatsApp** (`integrations/whatsapp/failed_file.py`, `WHATSAPP_SEND_FAILED_FILE`): when an import FAILS or needs review, the original file is sent to every admin number with a caption naming the vendor/customer, sender and reason. Covers WhatsApp and Gmail sources — so a bad file arrives in the chat immediately, and survives the server's ephemeral disk.

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
**When allocation runs** (important): automatically **once per customer order, at import time** (batched — see `allocation_batch`), and whenever someone clicks **Auto-Select** on the web. Orders arriving in one batch are allocated **in arrival order**, each committing before the next, so every customer consumes stock the previous one left. A later vendor upload never re-runs a full allocation — saved `VendorSelection` rows stand until a human clicks Auto-Select (which clears and recomputes that order).

**Auto top-up on new stock** (`core/services/rules/topup.py`, `TOPUP_ON_NEW_STOCK`): a vendor uploading fresh stock **adds** to recent orders that are still short — never moves, reduces or re-points an allocation that already exists, so anything already communicated to a vendor stays valid.

```
order line: requested 10, allocated 4 (APEX)   -> short 6
BHARAT uploads 5                               -> +5 BHARAT  (APEX's 4 untouched)
CARBO uploads 50                               -> +1 CARBO   (never more than requested)
```
Only orders newer than `TOPUP_WINDOW_DAYS` (default 7) qualify; every write goes through the same `upsert_selection` guards and row lock, so two orders can never share the same stock.

**Output** (`integrations/whatsapp/topup_output.py`): WhatsApp receives a **`vendor_reallocation_<timestamp>.xlsx`** — the same shape as the allocation report, one worksheet per affected order (titled `O12 Karol Bagh`, headed *"Customer Order 12 — Karol Bagh (file: kb.xlsx) — updated from new vendor stock"*), each showing that order's COMPLETE current allocation, old vendors and new together. The caption names the vendor and the affected orders. The part-by-part text list stays web-only (too long to read on a phone). Nothing is sent at all when there was nothing to fill.

**Which sheet is whose** (`allocation_batch._order_identity`): each worksheet in the batch workbook is titled `O<order id> <Customer Name>` (Excel caps titles at 31 chars) and its **first row spells out the identity in full** — `Customer Order 12 — Karol Bagh Auto Spares & Sons  (file: kb.xlsx)` — with the column headers below it. Orders whose customer was never identified (Gmail attachments carry no Customer Code) fall back to `Order_<id>` plus the same in-sheet heading with the source file name. The WhatsApp caption lists the same `Order N — Customer` mapping.

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

## 8. WhatsApp Conversation Reference — what to write in the bot

Who is texting matters: the bot behaves differently for the **Founder/admin numbers** (`WHATSAPP_ADMIN_PHONE_NUMBER`), **registered vendor/customer numbers** (the number registry), and **any other number**.

### 8a. Founder / admin commands (only from an admin number)

| You write | What happens |
|---|---|
| `register` (also accepted: `contacts`, `update numbers`, `update contacts`, `update vendor numbers`) | Bot replies "Send the contact list Excel now…". Your NEXT Excel is taken as a vendor contact list. |
| *(then send the Excel)* — one row per vendor: Vendor Name + WhatsApp number(s) | Adds NEW vendors + numbers, and UPDATES numbers of existing vendors (each listed vendor's numbers are replaced by that row; a number owned by another vendor is moved). Bot replies with exactly what changed. |
| file + caption `contacts` (or `register`) | Same contact-list import in one step — no text needed first. |
| `send reminder` (also: `send reminders`, `reminder`, `remind vendors`) | Reminder template goes to ONLY the vendors still pending today; bot replies naming who was nudged. All submitted → "🎉 All registered vendors have already submitted." |
| `vendor` / `customer` / `invoice` + files | The classic upload flow below — the admin uploads on behalf of any party, so captions/commands still apply to admin numbers. |

Automatic messages you receive without asking: every import result (✅/⚠️/❌ with vendor name), the consolidated workbook captioned `Updated by: <vendor names>`, allocation reports captioned `Order N — <customer name>`, the 11:00 daily summary (`Received X / Y` + pending list), and (when enabled) "Morning stock request sent to N vendors."

### 8b. Registered vendor numbers (added via `register` / the seed script)

| The vendor sends | What happens |
|---|---|
| an Excel file — ANY filename, NO caption, NO command | imports as THAT vendor's stock instantly (the number is the identity). Reply: "✅ Stock received successfully. 245 item(s) imported." |
| a broken/unreadable file | saved formats + AI analysis are tried first; if all fail: "❌ We could not read this file. Please send an Excel with Part Number and Quantity columns." (Founder gets full technical detail.) |
| the same file twice | "ℹ️ This stock file was already received earlier. Nothing changed." |
| a PDF | verified as that vendor's INVOICE against allocations |
| any text (`hi`, `good morning sir`, even `vendor`) | ignored — texts from registered numbers are never commands or names |
| a caption on the file | ignored — the registry always wins; a stray caption can never misfile stock |

Registered **customer** numbers work the same with Excel = customer order ("✅ Order received successfully…", auto-allocation follows); PDFs get "please send Excel".

### 8c. Unregistered numbers (the classic flow — unchanged)

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

## 8b. File Inbox (web)

**Every file ever received — WhatsApp, Gmail, manual upload — with a Download button for the EXACT bytes the sender uploaded.** (`backend/app/api/routes/documents.py`, `frontend/src/pages/FileInboxPage.jsx`)

- Filter by status: Failed / Needs review / Duplicates / Processed…
- Each row: received time, file name, source, type, sender, status, exact failure reason.
- **Download a FAILED file to open it yourself** and see what the vendor actually sent — no need to ask them to re-share.
- Honest availability: files live on the app server's disk, which Render clears on restart/redeploy — rows whose bytes are gone show a disabled Download button instead of erroring. (`IncomingDocument.stored_path`; the resolver checks `uploads/incoming|processed|failed/`.)

---

## 8c. Founder Command Centre (web) — Phase 1

**One screen answering "what happened today, what's short, what needs me"** (`backend/app/api/routes/command_centre.py`, `frontend/src/pages/CommandCentrePage.jsx`, nav: **Command Centre**). Everything is a live DB aggregate (never the Sheet/workbook outputs), and every card clicks through to the page holding the records.

- **KPI strip**: vendors expected/received/pending today · orders + lines today · qty ordered/allocated/short + fill rate · at-risk orders (7d) · live remaining stock (imported − reserved, same rule the allocation engine enforces) · active parts/vendors · POs today/MTD · delivery outstanding (reuses Delivery Tracking's computation) · invoice mismatches (7d) · files today.
- **Action Required**: import failures (file + sender + exact reason), order shortages (customer + short qty + age), vendors who haven't sent stock (with last-submission date), invoice discrepancies, failed PO emails — errors first.
- **Stock vs Demand table** (spec §8): per short part — vendor stock, reserved, live remaining, demand, allocated, short, gap, and which vendors carry it.
- Phase 1 shows **quantities and counts, not money** — price data is absent from most vendor files, and an untraceable figure must not be shown (spec §28). Finance KPIs come with the finance phase.

**Phase 2 additions** (same page, with a Today / 7 / 30 / 90-day period selector):

- **Procurement Funnel** (spec §5): Vendor Stock → Orders → Allocation → POs → Invoices → Delivered, quantity bars + stage-to-stage conversion %, each stage clickable.
- **Control Towers** (spec §7, §11, §12): Customer Orders (fully/partially/un-allocated, awaiting PO, still-short ageing 0-1h…24h+, customer-wise fill rate) · Purchase Orders (status, fully/partially/not supplied — completeness from the same Delivery Tracking source — unsupplied ageing 0-1d…15d+) · Deliveries (ordered/delivered/short, vendor-wise & part-wise short supply, daily delivery series).
- **Vendor Scorecard + Stock-Trust** (spec §9, §10): per vendor — declared → allocated → invoiced → delivered → short chain, fulfilment %, trust % (did supply match declaration?), 30-day upload discipline, and a composite score /100 using the spec's weights **renormalised over metrics that have data** (price competitiveness and due-date timeliness are omitted until that data exists — a score is never faked; inactive vendors are omitted, not zeroed).
- **Trends** (spec §21): daily ordered-vs-allocated lines and stock-received/files-failed bars over the selected window.

**Phase 3 additions:**

- **Part Intelligence** (spec §18, nav: **Part Intelligence**, `GET /api/command-centre/part-intelligence?q=`): search ANY spelling of a part (aliases, root/OEM numbers, special characters ignored — the allocation engine's own matching) → canonical number + every known alias, per-vendor declared/reserved/live stock, price & MRP (★ marks the best price), selected-vendor flags, delivered/short history, last upload date, and 30-day demand/allocated/short.
- **Price Leakage / Finance-lite** (spec §14–15, Command Centre panel, `GET /api/command-centre/price-leakage`): purchase value of priced allocations, best-available-price comparison per selected allocation, potential leakage rows (part · selected vendor/₹ · best vendor/₹ · qty · leakage). **Coverage honesty built in**: every rupee figure states what % of allocated quantity actually carried a price — unpriced allocations are counted and disclosed, never silently valued at ₹0 (spec §28).
- **Audit Trail** (spec §24, nav: **Audit Log**, `core.models.AuditLog` + `backend/app/services/audit_service.py`): append-only record of every MANUAL, management-impacting action — manual vendor select/deselect (with before/after values and the acting user), Danger-Zone purges, founder WhatsApp contact-registry updates. Recorded on the same transaction as the action itself; automatic pipeline activity stays in Import History / File Inbox.
- **Deferred with reason**: role-based web visibility (§25) waits until staff accounts exist; the `role` claim already travels in the JWT. Vendor portal/login is ON HOLD per the Founder (Dealer Portal integration question — Anik).

**Founder-clarification additions** (from the recorded Q&A):

- **PO distribution on WhatsApp** (`integrations/whatsapp/po_output.py`, `WHATSAPP_SEND_PO` / `WHATSAPP_SEND_PO_TO_VENDOR`): each generated PO's workbook is sent to — the **vendor's registered number** (a vendor only ever receives THEIR OWN PO, by construction: one PO per vendor, addressed per-PO), every **purchase-team member**, the **number that originally sent the order** over WhatsApp, and the **Founder/admin number(s)** — deduplicated. Vendor number not registered → internal recipients only, and the toast says so. Caveat: WhatsApp's 24h window applies to free-form document sends; a vendor who submitted stock that day has it open (the normal case).
- **Purchase-team registry** (`register team` on WhatsApp → Excel of Name + Number; REPLACES the list; audited): these members receive every PO, and their sends are tracked.
- **The overdue rule** (Founder's definition): a PO is **OVERDUE when its vendor's invoice has not been uploaded within `PO_INVOICE_DUE_HOURS` (default 24) of PO generation** — the invoice upload is what tells the system the goods arrived. Surfaced in the PO Control Tower (`overdue` count) and as a red `po_overdue` alert naming the PO, vendor and age; clears the moment the vendor's invoice is imported.
- **Team Activity panel** (spec §16 as the Founder actually wants it, `GET /api/command-centre/team-activity`): number-wise WhatsApp activity per sender — files/orders/stock counts, failures, last activity — labelled from the team list, vendor/customer registry and admin numbers. Founder-only by construction (single web user).

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
