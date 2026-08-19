CarTrends · Founder Procurement Command Centre
What the specification asks for, and what the system actually does
Every one of the 28 sections in Sir's specification, checked against the running code rather than against our notes. Verified line by line in the backend API, the database models and the React pages.

The verdict
0 sections fully complete
21 partly built
7 not started
~60% of individual requirements met

The pattern is clean and worth stating plainly. Everything the system measures in quantities is built and working — demand, allocation, shortages, deliveries, vendor trust. Everything measured in rupees is missing, because purchase orders and invoices carry no price column in the database at all.

The second gap is drill-down, which Sir marked non-negotiable. Numbers are clickable, but every click lands on an unfiltered list instead of the exact records behind that number.

Section by section

§3 Top KPI strip
All 12 cards exist and are clickable. PO Value, Delivery Outstanding and Short Supply show quantities where Sir asked for value.
Partial

§4 Red alert / action centre
5 of 7 alert types. Missing: Delivery Overdue, Manual Decision Required. Order Shortage does not name the part; Invoice Discrepancy says "needs review" instead of the actual mismatch type we already know.
Partial

§5 Procurement funnel
6 of 8 stages. Vendor Comparison and Closure missing. No leakage highlighting, and the first conversion divides ordered quantity by declared stock, which is not a real conversion.
Partial

§6 Vendor inventory control
Missing stock staleness, single-vendor vs multi-vendor part analysis, and the 7/30/90-day submission heatmap (only one 30-day number exists).
Partial

§7 Customer order control tower
4 of 7 statuses. New, Awaiting Vendor, Delivered and Closed do not exist. Customer-wise fill rate and part-wise shortage are calculated and sent to the browser but never shown.
Partial

§8 Live stock & reservations
All six columns correct. But it lists only parts that are already short, capped at 25 rows — so it cannot answer "show me the reservation position of this healthy part".
Partial

§9 Vendor performance scorecard
Score uses 4 of 6 weights, rescaled — so fulfilment and trust count 38.5% each instead of 25%. Delivery timeliness and price competitiveness contribute nothing. No lead time, no on-time %, no 7/30/90 trend.
Partial

§10 Vendor stock trust score
Computed per vendor only. Sir's example is per part and vendor ("declared 100 of this part, delivered 35") — that combination cannot be produced today.
Partial

§11 Purchase order control tower
No approval, release-to-vendor or acknowledgement states exist. All four value figures missing. Ageing buckets only count POs with zero deliveries, so partly-supplied POs never age.
Partial

§12 Delivery control tower
Strong, except Overdue Deliveries is absent (nothing records a promised date). Part-wise short supply and the daily trend are computed but never drawn here.
Partial

§13 Vendor invoice control
4 of 7 clean. "Pending" is not a status; "Value/Price Issue" is impossible with no price read from invoices. A part number we simply could not read is reported as "vendor sent something we never ordered" — which blames the vendor for our parsing problem.
Partial

§14 Procurement financial dashboard
Not built. No purchase value today or MTD, no outstanding PO value, no delivered-not-invoiced, no average purchase price.
Not built

§15 Price leakage / saving
The best-built section: selected vendor, selected price, best available price and leakage, with honest coverage reporting. Only the variance column is missing from the screen.
Partial

§16 Purchase team performance
0 of 8. Our Team Activity panel counts WhatsApp files each number sent — not orders handled, POs raised, PO value or response time per person. No business record stores which user did the work.
Not built

§17 Dependency & risk
Not built. No vendor concentration, no repeat-offender logic, and none of the four risk flags (High Dependency, Single Source, Low Stock, Unreliable Supplier) exist anywhere.
Not built

§18 Part intelligence search
Strongest area — part plus aliases, every vendor, declared/reserved/live remaining, demand, allocated, short, prices with best price marked. Missing category; brand is fetched but not shown; "selected vendor" means ever-selected rather than for this order.
Partial

§19 System health
Not on the Command Centre at all — WhatsApp, Gmail and Sheets status live on a separate Settings page. Database health is hard-coded "ok" without testing the database. No AI-fallback or notification health.
Partial

§20 File flow & data quality
Inbox, failure reasons and original-file download all work. Missing: source breakdown, duplicate count on screen, and sender resolved to a vendor or customer name.
Partial

§21 Trends & analytics
2 of 10 series charted. No MTD, no custom date range. And the period selector does not reach four panels — pick 90 days and the whole KPI strip, alerts, shortage list and scorecard silently stay on their own fixed windows.
Partial

§22 Navigation menu
10 of 15. Missing: Allocation Control, Procurement Finance, Purchase Team, Exceptions/Action Centre, Reports.
Partial

§23 Drill-down — marked non-negotiable
Clicks pass no filter. "Order 412 is short" opens the full order list. Shortage rows and scorecard rows are not clickable at all. No order-lifecycle view exists. Only two real drills work: order → comparison, and vendor → vendor history.
Not built

§24 Audit trail
Seven actions recorded with before/after and actor. Missing: automatic allocation, all PO actions including email resend, and import status changes. The reason is generated by the system, never asked of the person.
Partial

§25 Role-based visibility
No roles enforced; the role field exists but nothing reads it. Effectively one web user. Deliberate — Sir asked us to hold off on extra users and the vendor portal.
On hold

§26 Technical principles
7 of 11 fully met — database is the source of truth, Sheets is output only, the reservation ledger and row-locking are intact, part aliases and vendor codes preserved. Gaps: no role filtering, a few timestamps still print UTC, and drill-down IDs are absent from API responses.
Partial

§28 Exception-driven, not static charts
Alerts do lead the page, but they cannot be acknowledged, assigned or resolved — so "already handled" and "still broken" look identical. Credit where due: no business logic was duplicated in the frontend, and no number is shown as authoritative unless the database can prove it.
Partial

§27 — Sir's ten acceptance questions
The specification says the build is complete only when the founder can answer all ten from the application. Four are answerable today.

How much customer demand came in today? YES
How much can we fulfil? YES
Where exactly is the shortage? PARTLY
Which vendor is responsible? PARTLY
How much have we purchased? PARTLY
How much PO and delivery value is outstanding? NO
Are vendors delivering what they promise? YES
Are we buying at competitive prices? PARTLY
What requires my intervention? YES
Is the automation working correctly? PARTLY

Five root causes behind almost every gap
These are not dashboard problems. No amount of front-end work closes them — each needs a decision and a database change.

01 Purchase orders carry no price
A PO line stores only a quantity. This single fact removes §14 entirely and most of §11, plus the value halves of §3, §9 and §27.

02 Invoices carry no price either
We read part number and quantity from vendor invoices, never rate. So "Value/Price Issue" and any invoice-side money reconciliation cannot exist.

03 Nothing records a promised delivery date
No due date anywhere in the database. That blocks delivery timeliness (20% of the vendor score), average lead time, overdue deliveries, and late-delivery risk flags.

04 No record says which person did the work
Orders and POs have no user attached. All eight of Sir's purchase-team KPIs are unbuildable until they do.

05 Pages cannot receive a filter
No page accepts a filter from a link, and alerts and shortage rows do not carry the IDs of what they describe. This is why drill-down — the one thing marked non-negotiable — does not work.

What I would fix first
Ordered by value per day of work. Everything in the first group needs no new data and no decision from Sir — the numbers already exist and are simply not reaching the screen.

1. Show the numbers we already calculate
Customer fill rate, part-wise shortage, the daily delivery trend, price variance, duplicate file count and the fill-rate trend are all computed, sent to the browser, and then thrown away.
Cost: no new data
2. Make the period selector reach every panel
Right now choosing 90 days leaves the KPI strip, alerts, shortage list and scorecard on different windows while the header claims 90 days. That is a misleading screen, not just a gap.
Cost: no new data
3. Fix the PO ageing buckets
They currently count only POs with zero deliveries, so a PO stuck half-supplied for three weeks never appears as old.
Cost: no new data
4. Put System Health on the Command Centre
The data already exists on the Settings page. Moving it answers Sir's tenth acceptance question.
Cost: no new data
5. Real drill-down
Add record IDs to alerts and shortage rows, let pages accept a filter from a link, and make shortage and vendor rows clickable. This is the specification's non-negotiable.
Cost: 2–3 days
6. Risk flags and dependency view (§17)
Single Source, Low Stock and Unreliable Supplier are all computable from data we already hold. Vendor concentration by value needs price first.
Cost: 2 days
7. MTD and custom date range
Currently only Today / 7 / 30 / 90 exist.
Cost: 1 day
8. Price on purchase orders and invoices
Unlocks the whole financial half of the specification. Needs Sir's decision first: where does the rate come from — the vendor's stock file, or entered when the PO is raised?
Cost: needs decision
9. Promised delivery date on POs
Unlocks delivery timeliness, lead time, overdue deliveries and late-supplier flags. Needs a rule: is it a fixed number of days, or per vendor?
Cost: needs decision

Two questions only Sir can answer
Where do prices come from? Today a rate exists only when a vendor happens to include a Rate column in their stock file, and it is never used for allocation. Until rates arrive reliably — or we capture them when raising the PO — every rupee figure in the specification stays out of reach.

When is an order finished? The system has no concept of an order being delivered or closed. Without it, four of the seven order statuses, the funnel's closing stage, and any true order-lifecycle view cannot be built.

Audited 18 August 2026 against the running code — backend API, database models and web pages — not against project notes. Items marked "on hold" reflect Sir's own instructions, not oversights.