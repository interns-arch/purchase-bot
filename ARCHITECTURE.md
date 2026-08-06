# Vendor Inventory & Order Fulfillment — Architecture & Roadmap

**Historical design document — describes the state before the web app
existed.** This document analyzes the original CLI-only implementation
(Module 2: Order Matching Engine) against the full business workflow, and
lays out the plan that the FastAPI/React web app (built afterward) was
based on. Its "no code has been written yet" framing below and the
`vendors`/`users`/etc. schema sketch in §4 describe that starting point,
not the current state — see `Readme.md`'s "Web Application" section for
what was actually built, and `DEPLOYMENT.md` for current deployment
details. Kept as-is for the design rationale (why single-vendor-per-line
allocation was chosen, why Postgres over SQLite eventually, etc.), most of
which is still accurate.

---

## 1. Current Architecture

The system today is a **single stateless script**, not an application:

- `raw_files/*.csv` are scanned fresh on every run and loaded into an
  in-memory dict (`part_index: normalized_part_number -> [SourceRow]`).
- `input.csv` is read as "the order" and matched one line at a time.
- Matching rule: pick **one** source row whose quantity is `>=` the requested
  quantity (smallest sufficient row wins). If no single row covers the full
  request, it's reported as `PART_FOUND_BUT_QUANTITY_NOT_SUFFICIENT` — nothing
  is partially allocated.
- Output is a flat CSV; nothing is persisted between runs, and vendor
  identity is inferred purely from the file name.

It's a solid, well-written column-detection + matching algorithm
(locale-tolerant headers, decimal-safe quantities, encoding/dialect
sniffing) — but it has no data model, no history, and no way to plug in the
other six modules described in the README.

---

## 2. Flaws & Missing Components

- **No persistence** — every run recomputes from scratch; there's no audit
  trail, no order history, no way to compute vendor accuracy over time
  (needed for Module 7).
- **Vendor is a filename, not an entity.** Renaming a vendor file silently
  breaks provenance. There's no vendor master with contact info, terms, or
  an ID that other modules (PO, delivery, performance) can key off of.
- **No partial-fulfillment tracking.** The matching rule requires one row to
  cover the whole request, and when no single row qualifies the shortfall is
  reported but not carried forward anywhere. *(Decision: single-vendor-per-line
  allocation is being kept intentionally for Phase 2 — see §7. The remaining
  quantity is meant to be picked up later by Gap Analysis (Module 5) and
  Alternative Vendor Recommendation (Module 6) once deliveries are recorded,
  not by splitting the allocation itself. Split allocation across vendors is
  deferred to a future version, only if the business needs it.)*
- **No canonical part catalog.** Different vendors may use different codes
  for the same product; there's no alias/cross-reference layer, so "same
  product, different vendor SKU" can't be recognized.
- **CSV-only.** README explicitly says vendors send Excel too —
  `.xlsx/.xls` isn't supported yet.
- **No order/customer concept.** `input.csv` is an anonymous batch, not a
  trackable customer order with a lifecycle (new → allocated → PO issued →
  delivered → dispatched).
- **No feedback loop.** Price, MRP, and vendor reliability are passed
  through as inert columns — they aren't used to choose the *best* vendor
  when several could fulfill an item.
- **Silent data quality issues.** Invalid quantities parse to `0` with no
  visible flag; skipped files are only printed to console, not retained
  anywhere queryable.
- **No API, UI, auth, or concurrency control** — it's a one-shot CLI tool,
  so nothing else in the README (PO generation, delivery tracking,
  dashboards) has anything to attach to yet.

---

## 3. Improvements for Scalability / Production-readiness

- Move to a **layered service architecture**: ingestion → canonical DB
  (PostgreSQL) → business services (matching, PO generation, delivery
  tracking, gap analysis, vendor scoring) → REST API (FastAPI) → React
  frontend.
- **Snapshot-based inventory imports** — each vendor file import creates a
  timestamped snapshot rather than overwriting in place, so gap analysis
  and vendor performance have real history to compute against.
- **Port matching onto the DB, keeping single-vendor-per-line allocation** —
  each order line is still assigned to exactly one vendor row that can cover
  the requested quantity (as today), now persisted as an `allocations` row
  with a status of `FULFILLED` / `PARTIAL` / `UNFULFILLED` instead of just a
  CSV status string. No split-across-vendors logic in this phase (see §7).
- **Canonical Part Master + vendor alias table** so cross-vendor matching
  survives differing part codes.
- Background job runner (Celery/RQ, or FastAPI background tasks to start)
  for file imports and large matching runs so uploads don't block the
  request thread.
- **Structured logging + import-error table** instead of console prints, so
  skipped rows are queryable, not lost.
- RBAC (admin/ops/viewer), audit log on allocations/PO status changes.
- Containerize (Docker Compose: API + Postgres + Redis), Alembic
  migrations, CI running unit tests against sample vendor files.
- Index on `(vendor_id, part_id)` and normalized part number; cache hot
  lookups in Redis if vendor catalogs get large.

---

## 4. Database Schema (recommended core tables)

```
vendors(id, name, contact_info, payment_terms, active, created_at)
users(id, name, email, role, password_hash)

parts(id, canonical_part_number, brand, description, category, uom)
part_aliases(id, part_id FK, vendor_id FK, vendor_part_number)   -- cross-reference

inventory_imports(id, vendor_id FK, file_name, imported_at, status, row_count)
vendor_inventory(id, vendor_id FK, part_id FK NULL, vendor_part_number,
                  quantity_available, price, mrp, import_id FK, raw_data JSONB)

customers(id, name, contact_info, address)
orders(id, customer_id FK, order_date, status)
order_items(id, order_id FK, part_id FK, quantity_requested, status)

allocations(id, order_item_id FK, vendor_id FK, vendor_inventory_id FK,
            quantity_allocated, status)
            -- Phase 2: at most one allocation row per order_item
            -- (single vendor, quantity_allocated <= quantity_requested).
            -- status = FULFILLED | PARTIAL | UNFULFILLED.
            -- Schema allows multiple rows per order_item so a future
            -- split-allocation enhancement doesn't require a migration.

purchase_orders(id, vendor_id FK, po_number, status, created_at)
purchase_order_items(id, po_id FK, allocation_id FK, part_id FK,
                      quantity_ordered, unit_price)

deliveries(id, po_id FK, delivery_date, reference)
delivery_items(id, delivery_id FK, po_item_id FK, quantity_delivered)

audit_log(id, entity, entity_id, action, user_id, timestamp, diff)
```

Gap analysis (`ordered - delivered`) and vendor accuracy
(`delivered/ordered × 100`) are computed as views/queries over
`purchase_order_items` + `delivery_items`, not stored as separate mutable
tables — keeps them always consistent with source data.

---

## 5. APIs per Module

- **Module 1 — Inventory Import:** `POST /vendors`,
  `POST /vendors/{id}/inventory/import`, `GET /imports/{id}/status`,
  `GET /vendors/{id}/inventory`, `POST /parts/aliases`
- **Module 2 — Order Matching:** `POST /orders`, `POST /orders/{id}/match`,
  `GET /orders/{id}/matches`
- **Module 3 — PO Generator:** `POST /orders/{id}/generate-pos`,
  `GET /purchase-orders`, `GET /purchase-orders/{id}`,
  `GET /purchase-orders/{id}/export`
- **Module 4 — Delivery Tracking:** `POST /purchase-orders/{id}/deliveries`,
  `GET /purchase-orders/{id}/deliveries`
- **Module 5 — Gap Analysis:** `GET /orders/{id}/gap-analysis`,
  `GET /gap-analysis/summary`
- **Module 6 — Alternative Vendor Recommendation:**
  `GET /order-items/{id}/alternative-vendors`,
  `POST /order-items/{id}/reallocate`
- **Module 7 — Vendor Performance:** `GET /vendors/{id}/performance`,
  `GET /vendors/performance/ranking`
- Cross-cutting: `POST /auth/login`, RBAC middleware, `/reports/*`

---

## 6. Project Structure

```
backend/
  app/
    core/        (config, security, logging)
    db/          (session, base, alembic/)
    models/      (vendor.py, part.py, order.py, allocation.py, purchase_order.py, delivery.py, user.py)
    schemas/     (pydantic request/response models)
    api/v1/endpoints/  (vendors.py, inventory.py, orders.py, purchase_orders.py, deliveries.py, gap_analysis.py, vendor_performance.py, auth.py)
    services/    (inventory_import_service.py, matching_engine.py, po_generator.py, delivery_service.py, gap_analysis_service.py, alternative_vendor_service.py, vendor_performance_service.py)
    ingestion/   (csv_reader.py, excel_reader.py, column_detector.py — evolved from ordermatching.py)
    workers/     (celery_app.py, tasks.py)
    tests/
  requirements.txt
  Dockerfile
frontend/
  src/pages/ (Dashboard, VendorInventory, Orders, PurchaseOrders, Deliveries, GapAnalysis, VendorPerformance)
  src/components/, src/api/
infra/
  docker-compose.yml, .env.example
```

The existing `ordermatching.py` column-detection logic (`normalise_header`,
`normalise_part_number`, encoding/dialect sniffing) is genuinely reusable —
it should be lifted into `ingestion/column_detector.py` rather than
rewritten.

---

## 7. Development Phases

| Phase | Scope |
|---|---|
| 0 | Repo skeleton, Docker Compose (Postgres+Redis), Alembic, FastAPI shell, CI |
| 1 | Module 1: vendor CRUD, file import service (CSV+Excel), snapshot history, import-error table |
| 2 | Module 2: rebuild matching engine against DB; **keep single-vendor-per-line allocation** (unmatched/partial quantity flows to Phases 5–6, not split within Phase 2) |
| 3 | Module 3: PO generation grouped by vendor, PO export (PDF/Excel) |
| 4 | Module 4: delivery recording against PO items, partial/multiple deliveries |
| 5 | Module 5: gap analysis queries + dashboard view, SLA-age flagging |
| 6 | Module 6: alternative-vendor ranking (price/reliability/lead time) + reallocation flow |
| 7 | Module 7: vendor accuracy aggregation, performance dashboard (Chart.js/Recharts) |
| 8 | Hardening: auth/RBAC, audit log, upload validation, background jobs for big imports, monitoring, deploy pipeline |

Each phase is independently shippable and testable before moving to the
next.

**Locked decision (2026-07-29) — superseded, see 2026-07-31 below:** Phase 2
keeps the existing allocation behavior — each order line is assigned to a
single vendor able to cover the requested quantity. If no vendor can, the
line is marked `PARTIAL` or `UNFULFILLED` rather than split across vendors.
The remaining/pending quantity is picked up after deliveries are recorded,
by Gap Analysis (Phase 5) and Alternative Vendor Recommendation (Phase 6).
Split allocation across vendors within the matching step itself is deferred
indefinitely and only revisited as an optional enhancement if the business
requires it.

**Redesign (2026-07-31):** Phase 2 no longer auto-selects a vendor at all.
`order_matching.py` (Module 2) now only *searches* every vendor's active
inventory for each order line and writes a Vendor Comparison Report
(`output/vendor_comparison_report.xlsx`) listing **every** vendor that
stocks the part — vendor name, part description, available quantity, MRP,
sale price, discount (where the source file provides them), and a per-vendor
stock status (`Available` / `Partial` / `Out of Stock` / `Not Found`). It
never picks a vendor or decides a fulfilled quantity.

The workflow is now:

```
Customer Order
    -> Search All Vendor Inventories        (order_matching.py)
    -> Generate Vendor Comparison Report    (output/vendor_comparison_report.xlsx)
    -> Vendor Selection                     (manual or rule-based -- NOT YET BUILT)
    -> Generate Purchase Order              (po_generator.py)
    -> Vendor Delivery Upload                (delivery_import.py)
    -> Compare Ordered vs Delivered Quantity (gap_analysis.py)
    -> Gap Analysis
    -> Vendor Performance Dashboard          (vendor_performance.py)
```

Vendor Selection is a deliberately separate, not-yet-built module. Possible
future rules — lowest MRP, lowest sale price, highest available quantity,
vendor performance score, fastest delivery history, manual selection by the
purchase team, or a combination — can all be implemented later purely by
adding a new module that reads the comparison report/service output; the
inventory-search module above (`vendor_comparison_service.py`) never needs
to change to support them.

Consequence: `po_generator.py` (and therefore `delivery_import.py`,
`gap_analysis.py`, `alternative_vendor.py`, `vendor_performance.py`,
`summary_report.py`) has no valid input until Vendor Selection exists and
produces a chosen-vendor-per-line dataset in the shape `po_generator.py`
expects. `run_pipeline.py` was updated to stop after `order_matching.py`
until that module is built (see its comments). The previous
`output/matching_output.csv` was archived to
`output/matching_output.csv.legacy` so it can't be silently consumed by
`po_generator.py` in its old, now-obsolete format.

New/changed code for this redesign:
- `core/services/vendor_comparison_service.py` (new) — pure business logic:
  searches `get_master_inventory()`'s offers for every order line and
  returns every matching vendor, never choosing one.
- `core/services/inventory_import_service.py` — `run_import` now also
  detects and stores `price`/`mrp` columns (previously always `NULL` despite
  the model supporting them); `get_master_inventory` now also returns each
  vendor offer's `raw_data` and source `inventory_file` name so MRP/sale
  price/discount/description can be resolved even for vendor files that
  don't use the exact recognized header names (same raw-data fallback
  pattern `alternative_vendor_service.py` already used for price).
- `core/ingestion/column_detector.py` — added `PRICE_HEADERS`, `MRP_HEADERS`,
  `DISCOUNT_HEADERS`, `DESCRIPTION_HEADERS` and `find_optional_column()` for
  these non-required columns.
