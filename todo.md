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