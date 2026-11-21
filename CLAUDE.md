# ProcureHub: CarTrends purchase bot

Read this first. It is the working context for anyone (or any Claude session) picking up this project:
what the bot does, where it runs, how to deploy, the rules the team set, what changed and when,
and what is still open. Last updated **17 Nov 2026**.

> Contains internal phone numbers and vendor ids. Keep the repo private. No password, token or key is
> written here. They live only in `backend/.env` on the server.

---

## 1. What the bot does

ProcureHub is CarTrends' purchase bot (FastAPI backend + React frontend + Postgres, Docker on AWS).

1. **Vendor stock**: vendors send stock sheets on WhatsApp (official number, or their WhatsApp group via
   Whapi). The bot imports them (`vendor_inventory`), pushes stock to the Dealer Portal, keeps Google Sheet tabs.
2. **Portal procurement** (the main flow now): read Dealer Portal **Active Procurement** every 10 min,
   ask the brand's vendors in their WhatsApp groups, rank the replies, make the **PO-MST** on the Portal,
   push it to **Odoo**, send the PO PDF to Shubham.
3. **Vendor onboarding**: "new vendor" over WhatsApp, approvals, then `POST /users/vendor/create` on the Portal.
4. **Live dashboard**: `/active-procurement` page: parts by brand, vendors per brand, who was asked and who
   replied, best answer, winner, vendor delivery record.

### Portal procurement flow (backend/app/portal_procurement/)
```
Portal GET /procurement/active (last 7 days)  --poll every 10 min-->  portal_demand_lines (DISCOVERED)
  -> round time (10:00, 13:00, 16:00, 19:00 IST) -> enquire(): one AdvanceOrder per brand (source="dealer_portal")
  -> advance_orders engine asks each brand vendor IN HIS WHATSAPP GROUP ONLY (step by step:
     "part hai kya with pcs?" -> MRP -> discount % -> days)
  -> ranking -> winner -> PortalAllocation -> knockoff -> PO-MST on Portal -> (approval) -> Odoo -> PDF to Shubham
```
Every round-only behaviour is gated by `_rounds_on(order)` (portal orders only), so old sales-bot orders are unchanged.

### Rules the user set (16 Nov 2026), all implemented and live
- **Rounds:** first 10:00, last 19:00, every 3 h (`rounds.slots_for`). A round missed overnight is not run next morning.
  New lines between rounds wait for the next round.
- **Brand only:** a vendor only gets parts of his brands (Maruti parts only to Maruti vendors).
- **Groups only:** vendors are asked in their WhatsApp group, never one to one. Vendor with no group → not asked.
- **Asks:** at most 3 times per part (first ask + reminders one round apart). Still silent → Prateek sir is told
  ("🔕 ... 3 baar poochne par bhi reply nahi kiya") and that vendor is not asked about that part again.
- **Who can win:** a reply over **7 days** can't win (asked again next round). Below **2% margin** can't win.
- **Ranking:** TAT band (1, 3, 7, 15 days) first → **brand-wise order from the dashboard's Vendors page**
  (`vendor_brands.priority`, 20 Nov 2026; `portal_priority.json` overrides it when set) → terms tier → bigger discount
  → **tie** (exact days, then delivery history: on-time %, else reply %) → net → id. TAT beats priority, priority beats discount.
- **All replies below 2% margin** → Prateek sir gets numbered answers ("🟠 Vendor choose kijiye — C78 ...");
  he replies `C78 1` or `C78 cancel` (bare `1`/`cancel` works when only one choice waits).
- **Stock sheet:** if the vendor's stock sheet **from today** shows the part, he is NOT asked whether he has it.
  Sheet gives pcs + MRP (else Portal MRP) + discount (from sheet price vs MRP) + days (from a TAT/lead/days column).
  Whatever is missing (discount and/or days) is asked, **only that**, one at a time. Complete sheet = his reply.
  Ranked with everyone else at the end of the round.
- **Several parts to one vendor (18 Nov 2026):** the group question comes with a CSV
  (`advance_orders/enquiry_csv.py`): Part No | Qty Needed (bot) | Available Qty | Available (Yes/No) | Discount % |
  Days (TAT) (vendor). His filled file (CSV or xlsx) sent back in the group is his answer
  (`document_worker._enquiry_answer_file` → `service.handle_enquiry_file`, checked before every other file route).
  MRP comes from the Portal. Only what he left empty for a part he HAS is asked afterwards (discount / days).
  A typed answer still works. **One part: asked as before** (pcs → MRP → discount → days). `ADVANCE_ORDER_ASK_CSV=false` turns it off.
- **Delivery record:** promised date = Portal `arrival_date`, else today + TAT. Received = Portal `qty_received > 0`.
- **PO PDF goes to Shubham only** (`PORTAL_PROCUREMENT_NOTIFY_NUMBERS=917355374975`). Never to vendors.

---

## 2. Hard rules (never break)

- **Vendors never see PO-GRP / PO-MST numbers or the PO PDF.** Never tell vendors about the Dealer Portal.
  Guard: `whapi._for_vendors` / `strip_po_refs` in `backend/app/integrations/whatsapp/whapi.py`.
  The Meta one-to-one path has no guard → never put a vendor number in the PO-MST notify list.
- **Portal Active Procurement is the only demand source.** The sales bot never asks the purchase bot.
- **Whapi / groups:** no bulk actions. Named groups only (`WHAPI_ACTIVE_GROUPS`), add one at a time, only when
  the user says. Never bulk-add members. Rajkumar's number was **banned** on 2 Nov 2026 after bulk group adds.
  Whapi runs on Alam sir's number (919217030408).
- **Never add enquiry vendors to `whatsapp_registered_numbers`** (that triggers the daily stock_request template).
- **Pilot:** don't widen the pilot or switch `PORTAL_PROCUREMENT_MODE=live` without the user's approval.
- **Production:** back up before every deploy; rebuilds need the user's go-ahead; never touch other containers
  (~27 on the box), Caddy, or the `procurehub-watcher-1` container.
- **Secrets:** never print, log, commit or repeat passwords, tokens, JWTs, Cloudflare secrets, Portal creds.
  Don't retry 401/403 automatically. Never type the Portal admin password yourself: the user puts it in `.env`.
- **Git:** commit only when asked. No "Co-Authored-By: Claude" trailer. Push only to remote **`purchasebot`**
  (github.com/interns-arch/purchase-bot), never `origin`. Branch: `feature/case1-vendor-stock`.
- Yash (8800556388) must not be added back to any alert list.
- Write user-facing WhatsApp text in simple Hinglish, as the existing messages do.

---

## 3. Server and deploy

- **EC2** `52.66.83.8`, code at `/home/ubuntu/procurehub`. SSH: `ssh -i ~/.ssh/cartrends-ec2.pem ubuntu@52.66.83.8`
  (the `.pem` key must be copied to the new PC by hand, never through git/chat).
- Containers: `procurehub-backend-1`, `procurehub-frontend-1`, `procurehub-db-1`, `procurehub-watcher-1` (leave alone).
- Frontend nginx :4008 proxies `/api` to backend on 127.0.0.1:4010. Webhook:
  `https://partsexpressonline.com/api/whatsapp/webhook` (Meta) and `/api/whatsapp/webhook/whapi` (Whapi), through the
  shared Caddy (`problem-app-caddy-1`, don't edit without asking).
- The server's own git checkout is **stale**: ignore its git status; compare files instead.

### Deploy steps
```bash
# 1. back up the files you will change
ssh -i ~/.ssh/cartrends-ec2.pem ubuntu@52.66.83.8 "cd /home/ubuntu/procurehub && tar czf backups/<name>-<date>.tgz <files>"
# 2. upload each file (strip CR)
tr -d '\r' < path/file.py | ssh -i ~/.ssh/cartrends-ec2.pem ubuntu@52.66.83.8 "cat > /home/ubuntu/procurehub/path/file.py"
# 3. rebuild (frontend only if frontend changed)
ssh ... "cd /home/ubuntu/procurehub && docker compose -f docker-compose.yml build backend frontend && docker compose -f docker-compose.yml up -d --no-deps backend frontend"
```
An `.env` change needs only `up -d --no-deps backend`. **Check before deploying** that every module the uploaded files
import exists on the server (see the mrp_list incident in section 6).

### Checks
```bash
# local
AI_REPLY_READER_ENABLED=false PYTHONPATH=. PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/check_portal_procurement.py
cd frontend && npx vite build --outDir /tmp/ph-build-check
# in the container
docker exec -w /app -e PYTHONPATH=/app -e PYTHONIOENCODING=utf-8 -e AI_REPLY_READER_ENABLED=false procurehub-backend-1 python backend/scripts/check_portal_procurement.py
```
Must end with `ALL PASSED`. ("WhatsApp down" tracebacks in its log are deliberate.) `check_advance_orders` fails at
section [2] on old code too: not a regression.

Scripts that talk to the Portal need the creds passed in without printing them:
```bash
set -a && eval "$(grep -E "^PORTAL_PROCUREMENT_(USERNAME|PASSWORD)=" backend/.env)" && set +a && \
docker exec -i -w /app -e PYTHONPATH=/app -e PORTAL_PROCUREMENT_USERNAME -e PORTAL_PROCUREMENT_PASSWORD procurehub-backend-1 python - < /tmp/script.py
```
For a script piped into the container, copy it to the server first (`cat local.py | ssh ... 'cat > /tmp/x.py; docker exec -i ... python - < /tmp/x.py'`).
Keep any waiting loop short and bounded.

---

## 4. Live settings (17 Nov 2026; no secrets)

`backend/.env` on the server:
- `PORTAL_PROCUREMENT_ENABLED=true`, `PORTAL_PROCUREMENT_MODE=pilot`, `PORTAL_PROCUREMENT_NOTIFY_NUMBERS=917355374975` (Shubham)
- Defaults in `backend/app/portal_procurement/config.py`: poll 10 min, rounds on, round_hours 3, max_asks 3,
  max_tat_days 7, round_start 10:00, round_end 19:00, min margin 2%, sheet_today_only true,
  `PORTAL_PROCUREMENT_SHEET_TAT_DAYS` unset (= ask days when the sheet has none), days window 7.
- `ADVANCE_ORDER_ESCALATION_NUMBERS=919999492550` (Prateek sir), `ADVANCE_ORDER_VENDOR_HOURS=09:00-21:00`,
  `ADVANCE_ORDER_QUOTE_FANOUT=50`, ask mode = steps (default), `PURCHASE_APPROVAL_MODE=true`.
- `WHAPI_ENABLED=true`, `WHAPI_VENDOR_GROUP_MODE=same`, `WHAPI_ACTIVE_GROUPS` = 10 groups incl. the testing groups.

**Pilot file** `/app/uploads/portal_pilot.json` (re-read every run, no restart needed). Keys: `lines`, `parts`,
`brands`, `vendors`, `skip_lines`:
```json
{"lines": [14920], "parts": ["6M5Z15A022A"], "skip_lines": [14501], "vendors": [72, 110, 145]}
```
- vendors 72 = Ark Automotive (group "Testing Ark", POC = Shubham 917355374975),
  110 = Divine Production (CT_DP, group 120363414110125727@g.us, POC 9122781913),
  145 = S And S Ventures (CT_SAS, group 120363416933145586@g.us, POC 9873261929; moved off vendor 176 "Ark Automotive TEST").
  Prateek sir is in the testing groups.
- `skip_lines: [14501]`: older real order PO-GRP-2026-1060 for the same part, kept out at the user's request.

### People
Prateek sir 919999492550 (escalations, choices, admin) · Alam sir 919217030408 (admin, Whapi number) ·
Shubham 917355374975 (PO PDF, Ark contact) · NK Jain sir 9810238966 (approver) · Rajkumar, Seema, Ujjwal (purchase team).

---

## 5. Code map (what changed on 16 Nov 2026)

| File | What |
|---|---|
| `backend/app/portal_procurement/service.py` | poll, enquire (pilot filter incl. `skip_lines`, brands), `_approval_state` (unwraps `approval` key), margin check, `poll_job` round gate, receipts |
| `backend/app/portal_procurement/rounds.py` | **new**: slots, round_due/mark_round, why_not, vendor record + ties, `decide_line`, Prateek sir choice (`offer_choice`/`handle_reply`), `tell_silent`, `take_from_sheet` + `_sheet_days`, promise/receipts |
| `backend/app/portal_procurement/live.py` | **new**: data for the dashboard (`GET /api/portal-procurement/live`) |
| `backend/app/portal_procurement/config.py` | rounds/sheet settings, `pilot()` with brands + skip_lines |
| `backend/app/portal_procurement/models.py` | `PortalRound` table; demand `choice_*`; allocation `promised_date`, `received_at` |
| `core/db.py` | `_SCHEMA_UPGRADES` for the new columns |
| `backend/app/advance_orders/service.py` | round hooks: `_rounds_on`, `_wait_minutes`, `_followup_plan`, groups only, sheet first in `_advance`, preset step question in `_send_queued` |
| `backend/app/advance_orders/steps.py` | one-question-at-a-time flow (pcs → mrp → discount → eta) |
| `backend/app/advance_orders/ranking.py`, `quotes.py` | `tie` in sort key; `apply_winner(allowed=)` |
| `backend/app/workers/document_worker.py` | `rounds.handle_reply` before escalation; `_mrp_list_file` (see section 6) |
| `frontend/src/pages/ActiveProcurementPage.jsx` | dashboard page, route `/active-procurement`, nav "Active Procurement" |
| `backend/scripts/check_portal_procurement.py` | sections [1]–[19]; [19] = rounds, sheet, ties, Prateek sir, delivery, live view |

---

## 6. Change log

- **2 Nov:** Rajkumar's number banned on Whapi after bulk group adds. MRP-list feature written, off, not deployed.
- **3 Nov:** Whapi back on Alam sir's number, 8 named groups.
- **7 Nov:** sales-bot data-entry robot switched off; **12 Nov** ProcureHub calls Portal `vendor/create` directly.
- **12 Nov:** Portal procurement built (pilot). Auto vendor group creation on.
- **13 Nov:** step-by-step vendor questions; PO-ref guard for vendor groups.
- **14 Nov:** 2% margin floor; customer MRP/discount/price stored.
- **16 Nov:**
  - removed "Source PO-GRP" line from PO-MST messages;
  - testing groups Divine + S And S mapped and added;
  - Ford test #1 PO-GRP-2026-1091 → PO-MST-2026-0081 (Divine) → Odoo P01497;
  - fixed approval detection (Portal returns `{"approval": {...}}`);
  - built rounds + dashboard + ties + Prateek sir choice + delivery record;
  - round times 10–19; stock-sheet rule; then "ask only the missing discount/days";
  - `skip_lines` in pilot; part 6M5Z15A022A added to pilot;
  - Ford test #2 **PO-GRP-2026-1112** (line 15039) → sent 18:20 to the 3 testing groups → Ark (5 days, 12%) and
    Divine (1 day, 4%) replied; S And S's question closed at the user's request → **Divine won** →
    **PO-MST-2026-0082** → Odoo **P01511** (marked completed, no approval step seen).
  - Backups on server: `no-pogrp-line-20261116.tgz`, `approval-shape-20261116.tgz`, `rounds-20261116.tgz`,
    `sheet-rounds-20261116.tgz`, `skip-lines-20261116.tgz`, pilot `uploads/portal_pilot.before-*-20261116.json`.

- **18 Nov:** enquiry CSV for multi-part questions, **deployed** (merged into the server's document_worker so the Tez code stays); backup `enquiry-csv-full-20261118.tgz`. `check_whapi` has 3 failures on the server that predate this.
- **19 Nov, deployed** (backup `meeting-rules-20261119.tgz`, container checks ALL PASSED): rules from the procurement meeting (Speaker 1's version):
  - Portal `discount_percent` per vendor (`GET /procurement/active/{id}/vendors`) read before each round
    (`rounds.prefetch_dealer_discounts`); a vendor with a discount set there is not asked the discount (0 = ask).
  - Priority list `uploads/portal_priority.json` `{"vendors": [...]}`: inside one TAT band a listed vendor beats a
    bigger discount (`ScoredQuote.priority_rank`). TAT band still first. No file = unchanged.
  - 6 vendor-hours with no usable answer → Prateek sir (`rounds.escalate_stale`): `AO-12 vendor <naam> <mobile>`
    saves a new vendor on the brand list (asked only once he has a group) or `AO-12 cancel`.
  - Vendor quote below the customer's discount → "⚠️ Kam discount" to Prateek sir (`rounds.check_low_discount`).
  - Evening report 20:00 IST to Prateek sir, text + Excel (`portal_procurement/reporting.py`).
  - Parts as a file to vendors: NOT added -- the 18 Nov enquiry CSV (live) already does it; a Portal discount
    also fills a blank discount in the returned CSV (`steps.fill_known`).
  - Local copy synced with the server first (18 Nov CSV deploy was made from another session); merged
    files only ADD to the server copies. Local backup of the old files: `..\local-backup-20261119\`.
  - `scheduler.py`: the MRP-list job import is guarded (`mrp_list` is not on the server).
  - Checks: [20] = server's CSV section, [21] = these rules; [19] now expects the 6-hour escalation.
- **20 Nov, deployed** (backup `approval-card-on-hi-20261120.tgz`): vendor approval outside the 24-hour window.
  Only the `vendor_approval_request` template reaches the approver; the details after it are blocked by WhatsApp
  (131049 — they go in the MARKETING `purchase_bot_update` template). VR-26: Prateek sir said "Hi" and got the
  staff help menu. Now `approvals.deliver_waiting_cards`: any message from an approver sends him the full card
  (details + buttons) of every waiting vendor he has not seen (`answers["_card_to"]`); "hi"/"ok"/"details"
  is answered by the card only. Check: `check_vendor_approval_buttons.py` [6b].
  Idea (needs Meta approval): add "Details ke liye HI likhiye" to the `vendor_approval_request` template.
- **20 Nov, deployed** (backup `no-order-notice-20261120.tgz`): "✅ Customer Order imported successfully" (the
  Boodmo order mail `Bodmoo_order.xlsx` from purchase@boodmo.com, ~09:00 and ~15:25) no longer goes to WhatsApp
  (`emitters.py`, `mirror=False`); still a toast on the web UI. Failed / with-errors imports are still sent.
- **20 Nov, deployed** (backups `vendor-portal-link-20261120.tgz`, `dealer_portal_accounts.before-link-20261120.json`):
  vendors made by the bot on the Portal are now LINKED. The id `vendor/create` returns is NOT the dealer id
  (NIRMAL: 8592 there, dealer 9116); the dealer id comes from `GET /users/` by username
  (`portal_vendor_api.dealer_id_for`). On create, `vendor_onboarding/portal_link.link_vendor` saves the stock map
  (CONFIRMED -- also what `portal_procurement.links.resolve` reads for PO-MSTs) + the login in the accounts file
  (vendors = [vendor_code], `push_stock_after` = now). Registration ref is now `DP-<dealer id>`.
  Backfill `backend/scripts/link_created_vendors.py` (dry run; `--apply`): one test login per vendor, then link.
  Linked: HPP 9210, SRG 9180, OSR 9209, PSA 9283, UMI 9279, TAI 9310 (NOS 9116 was already).
  **VR-24 M/S S S S INTARNATIONAL not linked:** its mobile is on an older login `OM_3_AND_SONS` (dealer 8857)
  the bot did not make -- a person must confirm it is the same firm and connect it on the Dealer Portal page.
  `check_vendor_approval_buttons.py` now blanks the Portal admin login + accounts file (it reached the real
  Portal when run in the container; nothing was created).
- **20 Nov, deployed** (backup `vendor-link-job-20261120.tgz`): the backfill is now `portal_link.backfill()` and runs
  every day at 21:30 IST (scheduler job `vendor_portal_link`, `VENDOR_PORTAL_LINK_TIME`), for a vendor whose dealer id
  could not be read at creation (slow `GET /users/`). A vendor that needs a person is sent to the approvers once,
  only if approved in the last 3 days. All bot-created vendors have their login saved; only VR-24 waits for a person.
- **20 Nov, deployed (frontend only)** (backup `inbox-redesign-20261120.tgz`): Chats and Groups are ONE inbox
  (`ChatsPage.jsx`): filters All / Vendors / Team / Customers / Groups (`?filter=`), the group activity feed behind
  "Bot log", `/groups` redirects to `/chats?filter=groups`, `GroupsPage.jsx` removed. Minimal shell: plain side menu,
  one palette (`--ink`, `--line`, `--wash`, `--accent` in `index.css`), no shadows/gradients/emoji. `Layout bare`
  = full-height page without the title bar. Light theme kept.
- **20 Nov, deployed** (backup `brand-priority-20261120.tgz`): the brand-wise vendor order on the Vendors page now
  picks the Portal winner among equally fast vendors (`rounds.fill_ties`: `priority_rank = vendor_priority`), ahead of a
  bigger discount; equal positions still go by discount. Page text updated. Checks [21] "dashboard order".
  Ford pilot order then: Ark #2, Divine #3, S And S #4.
- **21 Nov: Portal vendor-brand MAPPING and DISCOUNT are two separate records** (Portal team's guide
  `Desktop\THIRD_PARTY_VENDOR_BRAND_MAPPING_INTEGRATION.md`):
  - **Mapping** (who appears for a brand; decides Portal vendor options / PO-MST): `GET /masterpurchase/vendor-mappings`
    (504 rows: mapping_id, dealer_id, dealer_name, vendor_group_id, vendor_group_name, brand), `POST .../vendor-mappings/upload`
    CSV `Dealer Name,Brand`. Brand must be the EXACT master brand (`GET /parts/brands`), e.g. `HARISONS` not `HARISON`.
  - **Discount**: `GET /masterpurchase/vendor-brand-discounts`, `.../export`, `.../template`, `POST .../upload` CSV
    `Vendor Name,Brand,Discount%` (UPSERT by dealer + brand; also sets stock.discount). A discount alone does NOT map.
  - Vendor create can take `"brands": [...]` once the Portal's enhanced contract is live (returns dealer_id + brand_mappings).
  - Partap Singh: mapping 923 (Harisons Group, 99) and discount HARISONS 40.32 exist; bot's vendor_brands row 117 renamed
    HARISON -> HARISONS. My wrong discount row **HARISON (id 390) is still on the Portal -- a Portal admin must remove it**
    (no delete API). TROB: mapping 922 MAHINDRA exists, no discount yet. Backups: `backups/vendor_brand_discounts.before-*-20261121.csv`,
    `backups/vendor_mappings.before-psa-20261121.json`.
- **21 Nov, deployed** (backups `vendor-brands-20261121.tgz`, `vendor_mappings/vendor_brand_discounts.before-brands-20261121.json`;
  container checks ALL PASSED; backfill applied: Portal mappings 504 -> 507 (Om Sai Ram FORD, HPP CASTROL, UMI AFTERMARKET),
  discounts 131 -> 132 (Om Sai Ram FORD 8%), nothing removed; bot list + HPP CASTROL, UMI AFTERMARKET, TROB MAHINDRA).
  Live Portal brand names: MARUTI (bot keeps MARUTI SUZUKI internally), LIQUIMOLI (NIRMAL's "Liqui Moly" left unmapped --
  the backfill maps exact/alias/plural only; fuzzy needs a person). Vendor brands on creation:
  - Form: "Brands supplied" is checked against the Portal brand list (`portal_brands.cached_master`, 6 h cache) in
    `conversation._check_brands`: a misspelt/alias/unknown brand is shown with the Portal's name ("harison → HARISONS")
    and saved only after OK (`answers["_brand_check"]`, offered via the question's `suggest`) or a corrected reply.
    Portal list unreadable -> as typed. Discount is now asked RIGHT AFTER brands, naming them, as plain numbers in
    order ("12" / "12, 10"; "Ford - 8" still works). Changing brands drops old discounts. Question numbers shifted by one.
  - Creation: `portal_brands.apply_for_vendor` -> Portal mapping upload, then discount upload, each read back, then the
    bot's vendor_brands (Portal names, end of the brand order). Approver message gets "Brands: ...".
  - Backfill `backend/scripts/map_vendor_brands.py` (dry run; `--apply`). Live dry run: map Om Sai Ram FORD (+8%),
    HPP CASTROL, UMI AFTERMARKET; bot list + TROB MAHINDRA. `check_vendor_followups` has 5 failures on HEAD too.

---

## 7. Open items (as of 17 Nov 2026)

1. ~~Server has work that is not on GitHub~~ **Resolved 19 Nov:** the 17 Nov Tez ERP work and the server's
   `document_worker.py` (with `_tez_reply`) are committed and pushed with the 19 Nov meeting rules. The repo now
   matches the server for every deployed file. Still: always diff a file against the server before uploading it.
   (The 16 Nov vendor-file ImportError was fixed on the server on 17-18 Nov; stock imports run again.)
2. PO-MST-2026-0075 and 0076 still pending approval on the Portal.
3. PO-MST-0081 PDF goes to Shubham when he next messages the bot (24-hour rule).
4. Everything deployed up to 19 Nov is committed (`feature/case1-vendor-stock`). Commit only when the user asks.
5. Monthly MRP list: off, not deployed; needs a group channel and the Portal team's answer on an MRP field.
6. Next steps the user may want: widen the pilot (more parts / `"brands": ["FORD"]` / live mode, ask first).

### Known limitations
- A vendor reply without a part number goes to his **newest** open question (two lists open at once).
- Sheet price is assumed to include GST.
- Two Portal lines for the same part can both count the same sheet stock.
- Lines older than the 7-day Portal window stop updating but stay open on the dashboard.
- Dashboard shows open lines always, finished/closed lines for 3 days; the DB keeps everything forever.

---

## 8. Local setup on a new PC

1. Copy the whole `PURCHASE BOT` folder (keep the path `Desktop\PURCHASE BOT` if possible).
2. Delete and recreate `.venv` (`python -m venv .venv && .venv/Scripts/pip install -r requirements.txt`) and
   `frontend/node_modules` (`npm install`).
3. Copy `~/.ssh/cartrends-ec2.pem` by USB.
4. Copy Claude memory: `C:\Users\<user>\.claude\projects\<project-folder>\memory\` (open the project once in
   Claude Code first to see the folder name it creates, then copy the files in).
5. Port 8000 on the old PC was used by an unrelated Django project. Don't kill it if it's there.

More background: `ARCHITECTURE.md`, `DEPLOYMENT.md`, `DEPLOY_AWS.md`, `todo.md`.
