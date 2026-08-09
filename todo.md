# Forensic Fix Phase — Implementation TODO

## Fixes approved (1-6, plus notification flow)

### 1. Notification/transaction ordering (PRIORITY 3)
- [ ] Move `publish_document_result` outside the DB transaction in `document_worker.py`
- [ ] Ensure one final result notification per document

### 2. Vendor Inventory WhatsApp message (PRIORITY 1 + 3)
- [ ] `inventory_output.py`: success message only after workbook generation real success
- [ ] Expose partial results (Imported/Rejected/Reason) for PROCESSED_WITH_ERRORS
- [ ] Prevent duplicate success messages

### 3. Customer code collision (PRIORITY 2)
- [ ] Make `customer_code_service.generate_customer_code` robust (multi-letter, deterministic, hash fallback)
- [ ] Race-safe onboarding

### 4. Description normalization (PRIORITY 4)
- [ ] Add `name`, `partname`, `partdescr`, `partdescription`, `itemname`, `productname` to `DESCRIPTION_HEADERS`

### 5. Multi-vendor split fulfilment (PRIORITY 6/8)
- [ ] Remove all-or-nothing skip in `rules/engine.py` to allow partial allocation

### 6. Selected Qty (PRIORITY 7)
- [ ] `vendor_selection_service._export_row_cells`: `None` → `0`

## Not changing (per approval)
- Priority 5 Float Stock — unresolved business decision
- Priority 10 VS_CT — unresolved, production source unavailable
- Gmail — config-only issue

## Tests to run
- [ ] Customer code: aman != amit
- [ ] Multi-vendor: 1654600Q1FMK (3+2=5), J9022003 (4+2=6/7)
- [ ] Selected Qty: no allocation -> 0
- [ ] Description: MAHINDRA/DELHI/BIJVASAN
- [ ] Notification ordering
- [ ] Existing regression suite
