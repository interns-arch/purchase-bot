import { useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";
import { Layout } from "../components/Layout";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { listCustomerOrders } from "../api/customerOrders";
import { downloadVendorComparisonExport, getVendorComparison } from "../api/vendorComparison";

const STOCK_STATUS_OPTIONS = ["Available", "Partial", "Out of Stock", "Not Found"];
const PAGE_SIZE_OPTIONS = [25, 50, 100];

const COLUMNS = [
  { key: "customer_part_number", label: "Customer Part Number" },
  { key: "requested_quantity", label: "Requested Qty", numeric: true },
  { key: "vendor_name", label: "Vendor Name" },
  { key: "vendor_part_number", label: "Vendor Part Number" },
  { key: "part_description", label: "Part Description" },
  { key: "brand", label: "Brand" },
  { key: "vendor_available_quantity", label: "Available Qty", numeric: true },
  { key: "mrp", label: "MRP", numeric: true },
  { key: "sale_price", label: "Sale Price", numeric: true },
  { key: "discount", label: "Discount" },
  { key: "stock_status", label: "Status" },
  { key: "inventory_file", label: "Inventory File" },
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
  const [statusFilter, setStatusFilter] = useState("");
  const [sortKey, setSortKey] = useState("customer_part_number");
  const [sortDir, setSortDir] = useState("asc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [isExporting, setIsExporting] = useState(false);

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
    setPage(1);
  }, [search, statusFilter, orderId]);

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
    if (statusFilter) {
      rows = rows.filter((row) => row.stock_status === statusFilter);
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
  }, [comparison, search, statusFilter, sortKey, sortDir]);

  const totalPages = Math.max(1, Math.ceil(filteredRows.length / pageSize));
  const pageRows = filteredRows.slice((page - 1) * pageSize, page * pageSize);

  async function handleExport() {
    if (!orderId) return;
    setIsExporting(true);
    try {
      await downloadVendorComparisonExport(orderId, `vendor_comparison_order_${orderId}.xlsx`);
      toast.success("Export downloaded.");
    } catch (error) {
      toast.error(extractErrorMessage(error, "Export failed."));
    } finally {
      setIsExporting(false);
    }
  }

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
              className="btn btn--primary"
              onClick={handleExport}
              disabled={isExporting || !comparison}
            >
              {isExporting ? "Exporting…" : "Export to Excel"}
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
            <StatCard label="Matching Vendor Rows" value={comparison.summary.matching_vendors_found} />
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
                style={{ maxWidth: 200 }}
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value)}
              >
                <option value="">All statuses</option>
                {STOCK_STATUS_OPTIONS.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
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
              <EmptyState title="No matching rows" description="Try a different search or filter." />
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
                      </tr>
                    </thead>
                    <tbody>
                      {pageRows.map((row, index) => (
                        <tr key={`${row.customer_part_number}-${row.vendor_name}-${index}`}>
                          <td>{row.customer_part_number}</td>
                          <td>{row.requested_quantity}</td>
                          <td>{row.vendor_name || "-"}</td>
                          <td>{row.vendor_part_number || "-"}</td>
                          <td>{row.part_description || "—"}</td>
                          <td>{row.brand || "—"}</td>
                          <td>{row.vendor_available_quantity ?? "—"}</td>
                          <td>{row.mrp ?? "—"}</td>
                          <td>{row.sale_price ?? "—"}</td>
                          <td>{row.discount || "—"}</td>
                          <td>
                            <StatusPill status={row.stock_status} />
                          </td>
                          <td>{row.inventory_file || "-"}</td>
                        </tr>
                      ))}
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
