Implement a lightweight WhatsApp confirmation/reply system for the existing document processing flows.

IMPORTANT:
- Do not modify Gmail authentication.
- Do not modify Google Sheets OAuth or Sheets sync.
- Do not modify vendor selection logic.
- Do not change the existing Vendor Inventory → consolidated Excel → WhatsApp workflow.
- Do not redesign the invoice parser/importer.
- Keep all existing import/business logic unchanged.
- Only add/update WhatsApp user-facing confirmation messages.

CURRENT ISSUE:
When the user sends:

Invoice

the bot currently replies:

"Got it — now upload your Invoice file (Excel)."

This is incorrect because Invoice files are PDFs.

CHANGE 1: Correct the WhatsApp instruction messages

Vendor:
"Got it — now upload your Vendor file (Excel)."

Customer:
"Got it — now upload your Customer file (Excel)."

Invoice:
"Got it — now upload your Invoice file (PDF)."

Unknown command / file without a command should continue using the existing routing instruction.

CHANGE 2: Send a WhatsApp result message after processing

After the existing document processing finishes, send a short WhatsApp reply to the same user who uploaded the file.

For a successful Vendor Inventory import:

"✅ Vendor Inventory imported successfully.
Vendor: {vendor_name}
Records Imported: {records_imported}"

For a successful Customer Order import:

"✅ Customer Order imported successfully.
Source: WhatsApp
Order Lines: {order_lines}"

For a successful Vendor Invoice import:

"✅ Vendor Invoice imported successfully.
Invoice: {invoice_number}
Vendor: {vendor_name}"

Use the actual values available from the existing processing result. Do NOT invent values.

CHANGE 3: NEEDS_REVIEW

If processing finishes with NEEDS_REVIEW, send:

"⚠️ {Document Type} needs review.
Reason: {clear_existing_reason}"

Use the actual reason returned by the existing processing pipeline.

For example:

"⚠️ Vendor Invoice needs review.
Reason: Required invoice information could not be extracted from the PDF."

Do not falsely report a successful import.

CHANGE 4: FAILED

If the existing processing result is FAILED, send:

"❌ {Document Type} import failed.
Reason: {existing_error_reason}"

Use the actual error/reason from the processing result.

CHANGE 5: Invoice PDF specifically

For:

Invoice
+
PDF

the expected flow is:

WhatsApp
→ Invoice command
→ PDF received
→ existing VENDOR_INVOICE processing
→ existing invoice parser/importer
→ WhatsApp result message

Do not require any new token or external service.

IMPORTANT:
The current random/test PDF may legitimately result in NEEDS_REVIEW because it may not contain the invoice fields required by the existing importer. Do not change invoice parsing just to force a success response.

The goal of this task is only to make the WhatsApp user clearly understand whether processing succeeded, needs review, or failed.

FAILURE ISOLATION:
If sending the WhatsApp confirmation itself fails, it must NOT change the import result or roll back the database transaction. Log the delivery failure and keep the original import result.

DUPLICATE MESSAGES:
Do not send both a generic success message and a second success message for the same document. There should be one clear final WhatsApp result message per processed file.

TEST:

TEST 1:
Send:
Vendor
+ Vendor Excel

Expected:
- Existing Vendor import works.
- Existing consolidated Vendor_Inventory.xlsx workflow remains unchanged.
- WhatsApp receives one success confirmation.

TEST 2:
Send:
Customer
+ Customer Excel

Expected:
- Existing Customer import works.
- No Vendor_Inventory.xlsx generated.
- WhatsApp receives one success confirmation.

TEST 3:
Send:
Invoice
+ PDF

Expected:
- Bot says "Got it — now upload your Invoice file (PDF)."
- PDF is routed to existing Vendor Invoice importer.
- If valid invoice data is extracted → success WhatsApp message containing actual Invoice Number and Vendor.
- If data cannot be extracted → NEEDS_REVIEW WhatsApp message with actual reason.
- No Vendor Inventory workbook generated.

TEST 4:
Send an invalid/unsupported document after Invoice.

Expected:
- No false success.
- Clear NEEDS_REVIEW/FAILED WhatsApp response according to the existing processing result.

TEST 5:
Verify:
invoice
INVOICE
Invoice

all continue to route to Vendor Invoice.

IMPORTANT VERIFICATION:
Do not consider the task complete based only on compilation.

Report:
1. Exact files changed.
2. Exact WhatsApp messages implemented.
3. What was code-tested.
4. What was live-tested.
5. What could not be live-tested.
6. Confirm that Gmail, Google Sheets, Vendor Selection, and Vendor Inventory workbook generation were not changed.


create a button on ui to clear all files from database

VENDOR INVENTORY OUTPUT CONTRACT

Vendor_Inventory.xlsx must remain a multi-sheet workbook.

Each current vendor must have exactly one worksheet.

The canonical vendor_code is the vendor identity and must be used consistently.

Example:

MA_CT -> MAHINDRA
DE_CT -> DELHI
BI_CT -> BIJVASAN

The Excel workbook must contain:

MA_CT
DE_CT
BI_CT
...

Each worksheet must contain ONLY that vendor's latest/current inventory.

When a vendor uploads a new inventory:
- replace that vendor's worksheet completely
- preserve every unrelated vendor worksheet unchanged

When a vendor has no current/valid inventory:
- remove that vendor's worksheet completely
- never leave an empty/stale worksheet

Never create duplicate vendor worksheets.

The database remains the source of truth.
Never use the existing Excel workbook as the source of truth.

FUTURE GOOGLE SHEETS COMPATIBILITY:

The same vendor-inventory generation contract must be usable for Google Sheets later.

When valid Google OAuth/refresh-token credentials are available, the Google Spreadsheet must mirror the same structure as Vendor_Inventory.xlsx:

Database
  -> vendor_code
  -> vendor name
  -> latest inventory
  -> one tab per vendor

Excel:
  MA_CT
  DE_CT
  BI_CT

Google Spreadsheet:
  MA_CT
  DE_CT
  BI_CT

Do not design Excel and Google Sheets as two different business workflows.

Keep the workbook/spreadsheet generation logic based on the same DB source-of-truth and canonical vendor_code.

For now:
- Excel output is required and must work independently.
- Google Sheets must remain optional and must not block inventory processing.
- If Google OAuth is unavailable/invalid, Excel generation and WhatsApp delivery must still work normally.
- Do not fabricate or bypass Google credentials.

Later, when the refresh token is correctly configured:
- update/sync the same vendor tabs
- replace only changed vendor tabs
- preserve unrelated vendor tabs
- remove tabs for vendors with no current inventory
- never duplicate vendor tabs.

TEST:

Initial:
DE_CT
BI_CT
MA_CT

Update MA_CT:
Expected:
DE_CT unchanged
BI_CT unchanged
MA_CT completely replaced

Remove BI_CT:
Expected:
DE_CT remains
MA_CT remains
BI_CT is completely removed

Verify that the Excel workbook structure and future Google Spreadsheet structure are identical in terms of vendor tabs and vendor_code identity.



GOOGLE SHEETS VENDOR TAB IDENTITY REQUIREMENT

This is a design/consistency requirement for the Google Sheets integration.

DO NOT implement or modify Google Sheets authentication/OAuth in this task.
The current Google OAuth credentials are still being fixed separately.

However, verify and document the future Google Sheets implementation so that it uses the EXACT SAME canonical vendor identity as the Excel Vendor_Inventory.xlsx workflow.

CURRENT EXCEL STRUCTURE:

Vendor_Inventory.xlsx

    MA_CT
    DE_CT
    BI_CT
    ...

Where:

    MA_CT -> MAHINDRA
    DE_CT -> DELHI
    BI_CT -> BIJVASAN

The canonical identity is vendor_code.

FUTURE GOOGLE SHEETS STRUCTURE MUST BE:

Google Spreadsheet

    MA_CT
    DE_CT
    BI_CT
    ...

NOT:

    MAHINDRA
    DELHI
    BIJVASAN

REQUIREMENTS:

1. vendor_code must be the canonical identifier for vendor worksheets/tabs.

2. Google Sheets worksheet/tab names must use vendor_code, exactly like Excel.

3. Do not use vendor display name as the worksheet identity.

4. Vendor display names can change in the future. vendor_code should remain the stable identity.

5. The same vendor must never result in two tabs because of a vendor-name change.

Example:

Current:

    MA_CT -> MAHINDRA

If the vendor display name later changes to:

    MAHINDRA AUTO PARTS

The Google Sheet must still use:

    MA_CT

It must NOT create:

    MAHINDRA AUTO PARTS

while leaving MA_CT behind.

6. Vendor replacement behaviour must be identical to the Excel workflow.

If MA_CT uploads new inventory:

    MA_CT -> completely replace/update MA_CT

    DE_CT -> unchanged
    BI_CT -> unchanged

7. If a vendor has no current/valid inventory:

    remove its worksheet completely

Do not leave an empty or stale tab.

8. Never create duplicate vendor tabs.

9. The database remains the source of truth.

The Google Spreadsheet must NOT be treated as the source of truth.

The logical flow is:

    Database
        ↓
    Current vendor inventory
        ↓
    vendor_code
        ↓
    Output
      ├── Vendor_Inventory.xlsx
      └── Google Spreadsheet (future)

10. Excel and Google Sheets must use the same vendor inventory representation and the same vendor_code identity.

11. Do not create a separate Google-specific vendor mapping unless absolutely required by the Google Sheets API. If an API-specific mapping is required, it must still resolve to the canonical vendor_code.

12. Do not modify OAuth credentials, refresh-token handling, Gmail scopes, or Google authentication in this task.

13. Do not change the current Excel implementation unless required to preserve this identity contract.

14. Since Google OAuth is currently unavailable, do not claim that live Google Sheets behaviour has been tested.

VERIFY:

- Find where Google Sheets currently determines worksheet/tab names.
- Confirm whether it currently uses vendor.name instead of vendor_code.
- Confirm where Excel gets its worksheet name.
- Verify both ultimately use the same DB vendor identity.
- If the Google Sheets code is currently using vendor name, document the exact location and the minimal change required for the future Google Sheets phase.
- Do NOT make that change now unless explicitly requested.
- Confirm that changing the vendor display name in the future would not create a second worksheet once this requirement is implemented.

FINAL RESPONSE:

Report:

1. Current Excel worksheet identity
2. Current Google Sheets worksheet identity
3. Exact source-code location responsible for each
4. Whether they currently match
5. What must change in the future Google Sheets phase
6. Confirmation that no OAuth/authentication code was changed
7. Confirmation that no Excel/vendor-selection/inventory business logic was changed
8. Any remaining risks

DO NOT COMMIT.
DO NOT PUSH.