import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";
import { DashboardFilterBar } from "../components/DashboardFilterBar";
import { CHART_GRID, CHART_SERIES_PRIMARY } from "../components/chartColors";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { getVendorPerformance } from "../api/vendorPerformance";

const COLUMNS = [
  { key: "vendor_name", label: "Vendor" },
  { key: "parts_allocated", label: "Parts Allocated", numeric: true },
  { key: "ordered_qty", label: "Ordered Qty", numeric: true },
  { key: "delivered_qty", label: "Delivered Qty", numeric: true },
  { key: "fulfillment_pct", label: "Fulfillment %", numeric: true },
  { key: "short_qty", label: "Short Qty", numeric: true },
  { key: "accuracy_pct", label: "Accuracy %", numeric: true },
];

function StatCard({ label, value, hint }) {
  return (
    <div className="stat-card">
      <p className="stat-card__label">{label}</p>
      <p className="stat-card__value">{value}</p>
      {hint && <p className="stat-card__hint">{hint}</p>}
    </div>
  );
}

export function VendorPerformancePage() {
  const toast = useToast();
  const navigate = useNavigate();

  const [filters, setFilters] = useState({});
  const [data, setData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [vendorOptions, setVendorOptions] = useState([]);

  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState("fulfillment_pct");
  const [sortDir, setSortDir] = useState("desc");

  useEffect(() => {
    getVendorPerformance({})
      .then((base) => {
        setVendorOptions(
          [...base.rows]
            .map((row) => ({ id: row.vendor_id, name: row.vendor_name }))
            .sort((a, b) => a.name.localeCompare(b.name))
        );
      })
      .catch(() => {});
  }, []);

  const loadData = useCallback((currentFilters) => {
    setIsLoading(true);
    getVendorPerformance(currentFilters)
      .then(setData)
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load vendor performance.")))
      .finally(() => setIsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadData(filters);
  }, [filters, loadData]);

  function toggleSort(key) {
    if (sortKey === key) {
      setSortDir((current) => (current === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  const filteredRows = useMemo(() => {
    if (!data) return [];
    const query = search.trim().toLowerCase();
    let rows = data.rows;
    if (query) {
      rows = rows.filter((row) => row.vendor_name.toLowerCase().includes(query));
    }
    const sorted = [...rows].sort((a, b) => {
      const aVal = a[sortKey];
      const bVal = b[sortKey];
      if (typeof aVal === "number") return aVal - bVal;
      return String(aVal).localeCompare(String(bVal));
    });
    if (sortDir === "desc") sorted.reverse();
    return sorted;
  }, [data, search, sortKey, sortDir]);

  const top10 = useMemo(() => (data ? data.rows.slice(0, 10) : []), [data]);
  const bottom10 = useMemo(
    () => (data ? [...data.rows].slice(-10).reverse() : []),
    [data]
  );

  return (
    <Layout title="Vendor Performance">
      <section className="panel">
        <div className="panel__header">
          <h2>Filters</h2>
        </div>
        <DashboardFilterBar filters={filters} onChange={setFilters} vendorOptions={vendorOptions} />
      </section>

      {isLoading ? (
        <div className="page-loading">Loading vendor performance…</div>
      ) : !data || data.rows.length === 0 ? (
        <EmptyState
          title="No vendor performance data yet"
          description="Select vendors on Vendor Comparison and upload delivery files to see performance here."
        />
      ) : (
        <>
          <div className="stat-grid">
            <StatCard
              label="Best Performing Vendor"
              value={data.summary.best_vendor_name || "—"}
              hint={data.summary.best_vendor_fulfillment_pct != null ? `${data.summary.best_vendor_fulfillment_pct}% fulfillment` : undefined}
            />
            <StatCard
              label="Lowest Performing Vendor"
              value={data.summary.lowest_vendor_name || "—"}
              hint={data.summary.lowest_vendor_fulfillment_pct != null ? `${data.summary.lowest_vendor_fulfillment_pct}% fulfillment` : undefined}
            />
            <StatCard label="Average Fulfillment %" value={`${data.summary.average_fulfillment_pct}%`} />
            <StatCard label="Total Vendors" value={data.summary.total_vendors} />
            <StatCard label="Total Deliveries" value={data.summary.total_deliveries} />
            <StatCard label="Total Short Supply" value={data.summary.total_short_qty} />
          </div>

          <div className="stat-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))" }}>
            <section className="panel">
              <div className="panel__header">
                <h2>Vendor Fulfillment %</h2>
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={data.rows}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
                  <XAxis dataKey="vendor_name" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 12 }} unit="%" />
                  <Tooltip />
                  <Bar dataKey="fulfillment_pct" name="Fulfillment %" fill={CHART_SERIES_PRIMARY} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Top 10 Vendors</h2>
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={top10}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
                  <XAxis dataKey="vendor_name" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 12 }} unit="%" />
                  <Tooltip />
                  <Bar dataKey="fulfillment_pct" name="Fulfillment %" fill={CHART_SERIES_PRIMARY} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Bottom 10 Vendors</h2>
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={bottom10}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
                  <XAxis dataKey="vendor_name" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 12 }} unit="%" />
                  <Tooltip />
                  <Bar dataKey="fulfillment_pct" name="Fulfillment %" fill={CHART_SERIES_PRIMARY} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Monthly Performance Trend</h2>
              </div>
              {data.monthly_trend.length === 0 ? (
                <EmptyState title="No dated deliveries yet" description="Upload deliveries with a date column to see this trend." />
              ) : (
                <ResponsiveContainer width="100%" height={260}>
                  <LineChart data={data.monthly_trend}>
                    <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
                    <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 12 }} />
                    <Tooltip />
                    <Line type="monotone" dataKey="delivered_qty" name="Delivered Qty" stroke={CHART_SERIES_PRIMARY} strokeWidth={2} dot={{ r: 4 }} />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Delivery Accuracy Trend</h2>
              </div>
              {data.monthly_trend.length === 0 ? (
                <EmptyState title="No dated deliveries yet" description="Upload deliveries with a date column to see this trend." />
              ) : (
                <ResponsiveContainer width="100%" height={260}>
                  <LineChart data={data.monthly_trend}>
                    <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
                    <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 12 }} unit="%" />
                    <Tooltip />
                    <Line type="monotone" dataKey="accuracy_pct" name="Accuracy %" stroke={CHART_SERIES_PRIMARY} strokeWidth={2} dot={{ r: 4 }} />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </section>
          </div>

          <section className="panel">
            <div className="panel__header">
              <h2>Vendor Ranking</h2>
            </div>
            <div className="toolbar">
              <input
                className="field__input toolbar__search"
                type="search"
                placeholder="Search vendor…"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>

            {filteredRows.length === 0 ? (
              <EmptyState title="No matching vendors" description="Try a different search." />
            ) : (
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
                    {filteredRows.map((row) => (
                      <tr
                        key={row.vendor_id}
                        onClick={() => navigate(`/vendor-performance/${row.vendor_id}`)}
                        style={{ cursor: "pointer" }}
                        title="View vendor detail"
                      >
                        <td>{row.vendor_name}</td>
                        <td>{row.parts_allocated}</td>
                        <td>{row.ordered_qty}</td>
                        <td>{row.delivered_qty}</td>
                        <td>{row.fulfillment_pct}%</td>
                        <td>{row.short_qty}</td>
                        <td>{row.accuracy_pct}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </Layout>
  );
}
