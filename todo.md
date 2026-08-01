
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