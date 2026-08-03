We have finalized the business workflow. Please update the application accordingly.

=====================================================
1. REMOVE "DELIVERY" DOCUMENT TYPE
=====================================================

The "DELIVERY" document type is incorrect.

Remove it completely from the Document Inbox filter.

Also remove the "UNKNOWN" document type.

The final document types should only be:

- Vendor Inventory
- Customer Order
- Vendor Invoice

There should never be a Delivery or Unknown option.

=====================================================
2. ADD VENDOR INVOICE MODULE
=====================================================

Remove the "Document Inbox" page from the sidebar.

Instead add a new page:

Vendor Invoice

This page will be used for manually uploading Vendor Invoice files.

Supported formats:

- PDF
- Excel
- CSV

This module replaces the current Document Inbox functionality.

=====================================================
3. VENDOR INVOICE WORKFLOW
=====================================================

Vendor Inventory
↓

Customer Order

↓

Vendor Comparison

↓

Vendor Selection

↓

Export Vendor Allocation Excel

↓

Vendor sends Invoice

↓

Upload Vendor Invoice

↓

System extracts:

Vendor Name

Vendor Part Number

Quantity Delivered

Invoice Number

Invoice Date (if available)

↓

Store the delivery information

↓

Update Delivery Tracking automatically

↓

Update Vendor Performance automatically

=====================================================
4. DELIVERY TRACKING
=====================================================

Delivery Tracking should NOT require manual data entry.

It should automatically calculate data using:

Vendor Allocation
+
Vendor Invoice Upload

Display:

- Ordered Quantity
- Delivered Quantity
- Short Quantity
- Complete
- Partial
- Pending

=====================================================
5. VENDOR PERFORMANCE
=====================================================

Vendor Performance should also update automatically.

Calculate:

- Total Allocated Quantity
- Delivered Quantity
- Fulfillment %
- Short Supply
- Vendor Ranking
- Delivery Accuracy

Everything should come from actual imported data.

=====================================================
6. REMOVE DOCUMENT INBOX
=====================================================

The Document Inbox page is no longer required.

Delete:

- Sidebar item
- Page
- Navigation
- Routes

Replace it with Vendor Invoice.

=====================================================
7. UPDATED SIDEBAR
=====================================================

Dashboard

Vendor Inventory

Customer Orders

Vendor Comparison

Vendor Invoice

Delivery Tracking

Vendor Performance

Settings

=====================================================
8. IMPORTANT
=====================================================

Delivery Tracking and Vendor Performance must no longer display "Coming Soon".

Implement both modules using real data from:

- Vendor Inventory
- Customer Orders
- Vendor Comparison
- Vendor Invoice Upload

The dashboards should update automatically whenever a new Vendor Invoice is imported.

Do not use dummy data or placeholder cards.



















Review and improve the current inventory matching logic.

After testing with the uploaded vendor files and customer order, I found the following issues that need to be corrected.

=========================================================
1. AUTO-DETECT FILE DELIMITERS
=========================================================

Some vendor files are comma-separated.

Some vendor files are tab-separated.

Some may even use semicolons.

Currently, a tab-separated file saved as ".csv" is being read as a single column and is ignored by the matching process.

Update the importer to automatically detect the delimiter instead of assuming commas.

The importer should correctly read:

- CSV (comma)
- TSV (tab)
- Semicolon-separated files

If a file cannot be parsed, log the error and continue processing the remaining files.

=========================================================
2. INCLUDE EVERY VALID VENDOR IN MATCHING RESULTS
=========================================================

Currently the output mostly returns one matching vendor or only a status.

Instead, if multiple vendors have the same part, include ALL matching vendors.

Example

Customer wants

Part A
Qty = 10

Vendor A has 50

Vendor B has 20

Vendor C has 5

The output should contain all vendors.

=========================================================
3. BETTER OUTPUT FORMAT
=========================================================

Instead of only showing

MATCHED

or

PART_FOUND_BUT_QUANTITY_NOT_SUFFICIENT

generate a detailed output.

Columns

Customer Part Number

Customer Quantity

Vendor Name

Vendor File

Vendor Available Quantity

Can Fulfill (Yes/No)

Status

=========================================================
4. QUANTITY STATUS
=========================================================

Determine status using these rules.

If no vendor has the part

Status

PART_NOT_FOUND

---------------------------------------------------------

If vendor quantity >= requested quantity

Status

CAN_FULFILL

---------------------------------------------------------

If vendor has the part but quantity is smaller

Status

INSUFFICIENT_QUANTITY

=========================================================
5. DO NOT LEAVE VENDOR INFORMATION EMPTY
=========================================================

Currently rows with

PART_FOUND_BUT_QUANTITY_NOT_SUFFICIENT

have blank Vendor/File information.

This should never happen.

If the part exists, always include

Vendor Name

Vendor File

Available Quantity

even if the quantity is insufficient.

=========================================================
6. ADD MATCH SUMMARY
=========================================================

Print a summary after processing.

Example

Total Customer Parts

Matched Parts

Parts Not Found

Parts With Insufficient Quantity

Total Vendors Scanned

Total Vendor Files Processed

=========================================================
7. SAVE IMPROVED OUTPUT
=========================================================

Generate a new

matching_output.csv

with complete information.

Example

Customer Part

Customer Qty

Vendor

Vendor File

Vendor Qty

Can Fulfill

Status

100

1

Vendor A

vendor_a.csv

50

Yes

CAN_FULFILL

100

1

Vendor B

vendor_b.csv

30

Yes

CAN_FULFILL

110

2

Vendor C

vendor_c.csv

1

No

INSUFFICIENT_QUANTITY

108

1

-

-

-

-

PART_NOT_FOUND

=========================================================
8. CODE QUALITY
=========================================================

Keep the existing project structure.

Reuse existing functions where possible.

Do not break the current workflow.

The script should still run as

python ordermatching.py

and automatically process all vendor files inside

raw_files/

and the customer order file

input.csv

Finally, explain what changes were made and why they improve the accuracy and usefulness of the matching process.



