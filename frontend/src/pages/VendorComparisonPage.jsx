import { useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";
import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { listCustomerOrders } from "../api/customerOrders";
import { getVendorComparison } from "../api/vendorComparison";
import {
  autoSelectVendors,
  downloadSelectedVendorsExport,
  listVendorSelections,
  removeVendorSelection,
  selectVendor,
} from "../api/vendorSelection";

const PAGE_SIZE_OPTIONS = [25, 50, 100];

const COLUMNS = [
  { key: "customer_part_number", label: "Customer Part Number" },
  { key: "requested_quantity", label: "Requested Quantity", numeric: true },
  { key: "vendor_name", label: "Vendor Name" },
  { key: "vendor_part_number", label: "Vendor Part Number" },
  { key: "vendor_available_quantity", label: "Available Quantity", numeric: true },
];

function StatCard({ label, value }) {
  return (
    <div className="stat-card">
      <p className="stat-card__label">{label}</p>
      <p className="stat-card__value">{value}</p>
    </div>
  );
}

export function VendorComparisonPage() {
  const toast = useToast();
  const location = useLocation();
  const [orders, setOrders] = useState([]);
  const [orderId, setOrderId] = useState(location.state?.orderId || "");
  const [comparison, setComparison] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState("customer_part_number");
  const [sortDir, setSortDir] = useState("asc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [isExporting, setIsExporting] = useState(false);
  const [selections, setSelections] = useState([]);
  const [selectingKey, setSelectingKey] = useState(null);
  const [quantityDrafts, setQuantityDrafts] = useState({});
  const [isAutoSelecting, setIsAutoSelecting] = useState(false);

  useEffect(() => {
    listCustomerOrders()
      .then((data) => {
        setOrders(data);
        if (!orderId && data.length > 0) {
          setOrderId(String(data[0].id));
        }
      })
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load customer orders.")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!orderId) return;
    setIsLoading(true);
    getVendorComparison(orderId)
      .then(setComparison)
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load vendor comparison.")))
      .finally(() => setIsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderId]);

  useEffect(() => {
    if (!orderId) {
      setSelections([]);
      return;
    }
    listVendorSelections(orderId)
      .then(setSelections)
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load vendor selections.")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderId]);

  useEffect(() => {
    setPage(1);
  }, [search, orderId]);

  // Each customer part (order_item_id) can have allocations from several
  // vendors at once -- selecting/removing one vendor never affects another
  // vendor's allocation for the same part, or any other part's selections.
  const selectionsByItem = useMemo(() => {
    const map = {};
    for (const selection of selections) {
      const list = map[selection.customer_order_item_id] || [];
      list.push(selection);
      map[selection.customer_order_item_id] = list;
    }
    return map;
  }, [selections]);

  function toggleSort(key) {
    if (sortKey === key) {
      setSortDir((current) => (current === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  const filteredRows = useMemo(() => {
    if (!comparison) return [];
    const query = search.trim().toLowerCase();

    let rows = comparison.rows;
    if (query) {
      rows = rows.filter(
        (row) =>
          row.customer_part_number.toLowerCase().includes(query) ||
          (row.vendor_name || "").toLowerCase().includes(query) ||
          (row.vendor_part_number || "").toLowerCase().includes(query)
      );
    }

    const column = COLUMNS.find((c) => c.key === sortKey);
    const sorted = [...rows].sort((a, b) => {
      const aVal = a[sortKey];
      const bVal = b[sortKey];
      if (aVal == null && bVal == null) return 0;
      if (aVal == null) return 1;
      if (bVal == null) return -1;
      if (column?.numeric) return aVal - bVal;
      return String(aVal).localeCompare(String(bVal));
    });
    if (sortDir === "desc") sorted.reverse();
    return sorted;
  }, [comparison, search, sortKey, sortDir]);

  const totalPages = Math.max(1, Math.ceil(filteredRows.length / pageSize));
  const pageRows = filteredRows.slice((page - 1) * pageSize, page * pageSize);

  function allocatedForItem(orderItemId, excludeVendorId) {
    const list = selectionsByItem[orderItemId] || [];
    return list
      .filter((s) => s.vendor_id !== excludeVendorId)
      .reduce((sum, s) => sum + s.quantity_selected, 0);
  }

  function defaultQuantityFor(row) {
    const remaining = Math.max(
      0,
      row.requested_quantity - allocatedForItem(row.order_item_id, row.vendor_id)
    );
    return Math.min(remaining, row.vendor_available_quantity ?? 0);
  }

  async function handleAllocate(row) {
    const key = `${row.order_item_id}-${row.vendor_id}`;
    const draft = quantityDrafts[key];
    const quantity = draft != null && draft !== "" ? Number(draft) : defaultQuantityFor(row);
    if (!quantity || quantity <= 0) {
      toast.error("Enter a quantity greater than 0.");
      return;
    }
    setSelectingKey(key);
    try {
      const selection = await selectVendor(orderId, row.order_item_id, row.vendor_id, {
        quantitySelected: quantity,
      });
      setSelections((prev) => [
        ...prev.filter(
          (s) => !(s.customer_order_item_id === row.order_item_id && s.vendor_id === row.vendor_id)
        ),
        selection,
      ]);
      toast.success(`Allocated ${quantity} of ${row.customer_part_number} to ${row.vendor_name}.`);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not save vendor selection."));
    } finally {
      setSelectingKey(null);
    }
  }

  async function handleRemove(row) {
    const key = `${row.order_item_id}-${row.vendor_id}`;
    setSelectingKey(key);
    try {
      await removeVendorSelection(orderId, row.order_item_id, row.vendor_id);
      setSelections((prev) =>
        prev.filter(
          (s) => !(s.customer_order_item_id === row.order_item_id && s.vendor_id === row.vendor_id)
        )
      );
      toast.success(`Removed ${row.vendor_name} from ${row.customer_part_number}.`);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not remove vendor selection."));
    } finally {
      setSelectingKey(null);
    }
  }

  async function handleAutoSelect() {
    if (!orderId) return;
    setIsAutoSelecting(true);
    try {
      await autoSelectVendors(orderId);
      const refreshed = await listVendorSelections(orderId);
      setSelections(refreshed);
      toast.success("Vendors selected automatically.");
    } catch (error) {
      toast.error(extractErrorMessage(error, "Automatic vendor selection failed."));
    } finally {
      setIsAutoSelecting(false);
    }
  }

  async function handleExportSelected() {
    if (!orderId) return;
    setIsExporting(true);
    try {
      await downloadSelectedVendorsExport(orderId, `selected_vendors_order_${orderId}.xlsx`);
      toast.success("Export downloaded.");
    } catch (error) {
      toast.error(extractErrorMessage(error, "Export failed."));
    } finally {
      setIsExporting(false);
    }
  }

  const hasSelections = selections.length > 0;

  return (
    <Layout title="Vendor Comparison">
      <section className="panel">
        <div className="panel__header">
          <h2>Choose Customer Order</h2>
        </div>

        {orders.length === 0 ? (
          <EmptyState
            title="No customer orders yet"
            description="Upload a customer order first, then come back here to compare vendors."
          />
        ) : (
          <div className="toolbar" style={{ marginBottom: 0 }}>
            <select
              className="field__input toolbar__search"
              value={orderId}
              onChange={(event) => setOrderId(event.target.value)}
            >
              {orders.map((order) => (
                <option key={order.id} value={order.id}>
                  {order.file_name} — {new Date(order.created_at).toLocaleString()}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn--secondary"
              onClick={handleAutoSelect}
              disabled={isAutoSelecting || !orderId}
            >
              {isAutoSelecting ? "Selecting…" : "Auto-Select Vendors"}
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={handleExportSelected}
              disabled={isExporting || !hasSelections}
            >
              {isExporting ? "Exporting…" : "Export Selected Vendors"}
            </button>
          </div>
        )}
      </section>

      {isLoading ? (
        <div className="page-loading">Comparing vendors…</div>
      ) : comparison ? (
        <>
          <div className="stat-grid">
            <StatCard label="Customer Order Items" value={comparison.summary.customer_order_items} />
            <StatCard label="Parts Matched" value={comparison.summary.matched_items} />
            <StatCard label="Parts Not Found" value={comparison.summary.not_found_items} />
          </div>

          <section className="panel">
            <div className="panel__header">
              <h2>Vendor Comparison Report</h2>
            </div>

            <div className="toolbar">
              <input
                className="field__input toolbar__search"
                type="search"
                placeholder="Search part number or vendor…"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
              <select
                className="field__input"
                style={{ maxWidth: 160 }}
                value={pageSize}
                onChange={(event) => {
                  setPageSize(Number(event.target.value));
                  setPage(1);
                }}
              >
                {PAGE_SIZE_OPTIONS.map((size) => (
                  <option key={size} value={size}>
                    {size} rows / page
                  </option>
                ))}
              </select>
            </div>

            {filteredRows.length === 0 ? (
              <EmptyState title="No matching rows" description="Try a different search." />
            ) : (
              <>
                <div className="table-scroll">
                  <table className="data-table">
                    <thead>
                      <tr>
                        {COLUMNS.map((column) => (
                          <th key={column.key}>
                            <button
                              type="button"
                              className="link-button"
                              style={{ color: "inherit", textDecoration: "none", fontWeight: 600 }}
                              onClick={() => toggleSort(column.key)}
                            >
                              {column.label}
                              {sortKey === column.key ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                            </button>
                          </th>
                        ))}
                        <th>Allocated</th>
                        <th>Select Vendor</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pageRows.map((row, index) => {
                        const key = `${row.order_item_id}-${row.vendor_id}`;
                        const rowSelection = (selectionsByItem[row.order_item_id] || []).find(
                          (s) => s.vendor_id === row.vendor_id
                        );
                        const totalAllocated = allocatedForItem(row.order_item_id, null);
                        const draftValue =
                          quantityDrafts[key] ?? (rowSelection ? rowSelection.quantity_selected : defaultQuantityFor(row));

                        return (
                          <tr key={`${row.customer_part_number}-${row.vendor_name}-${index}`}>
                            <td>{row.customer_part_number}</td>
                            <td>{row.requested_quantity}</td>
                            <td>{row.vendor_name || "—"}</td>
                            <td>{row.vendor_part_number || "—"}</td>
                            <td>{row.vendor_available_quantity ?? "—"}</td>
                            <td>
                              {row.vendor_id != null
                                ? `${totalAllocated} / ${row.requested_quantity}`
                                : "—"}
                            </td>
                            <td>
                              {row.vendor_id != null && (
                                <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                                  <input
                                    type="number"
                                    className="field__input"
                                    style={{ width: 90 }}
                                    min="0"
                                    step="any"
                                    value={draftValue}
                                    onChange={(event) =>
                                      setQuantityDrafts((prev) => ({ ...prev, [key]: event.target.value }))
                                    }
                                  />
                                  <button
                                    type="button"
                                    className={"btn " + (rowSelection ? "btn--secondary" : "btn--ghost")}
                                    disabled={selectingKey === key}
                                    onClick={() => handleAllocate(row)}
                                  >
                                    {selectingKey === key
                                      ? "Saving…"
                                      : rowSelection
                                        ? "Selected ✓"
                                        : "Select"}
                                  </button>
                                  {rowSelection && (
                                    <button
                                      type="button"
                                      className="btn btn--ghost"
                                      disabled={selectingKey === key}
                                      onClick={() => handleRemove(row)}
                                    >
                                      ✕
                                    </button>
                                  )}
                                </div>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                <div className="toolbar" style={{ marginTop: 16, marginBottom: 0, justifyContent: "space-between" }}>
                  <span style={{ color: "var(--color-text-muted)", fontSize: "0.85rem" }}>
                    Showing {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, filteredRows.length)} of{" "}
                    {filteredRows.length} rows
                  </span>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button
                      type="button"
                      className="btn btn--ghost"
                      onClick={() => setPage((p) => Math.max(1, p - 1))}
                      disabled={page <= 1}
                    >
                      Previous
                    </button>
                    <button
                      type="button"
                      className="btn btn--ghost"
                      onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                      disabled={page >= totalPages}
                    >
                      Next
                    </button>
                  </div>
                </div>
              </>
            )}
          </section>
        </>
      ) : null}
    </Layout>
  );
}
