import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import {
  getCommandCentreAlerts,
  getCommandCentreStockGaps,
  getCommandCentreSummary,
} from "../api/commandCentre";

const SEVERITY_COLORS = {
  error: "#dc2626",
  warning: "#d97706",
  info: "#2563eb",
};

function KpiCard({ label, value, sub, tone, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        textAlign: "left",
        background: "var(--color-surface, #fff)",
        border: "1px solid var(--color-border, #e5e7eb)",
        borderLeft: `4px solid ${tone || "var(--color-border, #e5e7eb)"}`,
        borderRadius: 10,
        padding: "12px 16px",
        cursor: "pointer",
        minWidth: 0,
      }}
      title="Click to open the underlying records"
    >
      <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)", marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ fontSize: "1.5rem", fontWeight: 700, lineHeight: 1.1 }}>{value}</div>
      {sub && (
        <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)", marginTop: 4 }}>
          {sub}
        </div>
      )}
    </button>
  );
}

export function CommandCentrePage() {
  const toast = useToast();
  const navigate = useNavigate();
  const [summary, setSummary] = useState(null);
  const [alerts, setAlerts] = useState([]);
  const [gaps, setGaps] = useState([]);
  const [isLoading, setIsLoading] = useState(true);

  async function load() {
    setIsLoading(true);
    try {
      const [summaryData, alertsData, gapsData] = await Promise.all([
        getCommandCentreSummary(),
        getCommandCentreAlerts(),
        getCommandCentreStockGaps(),
      ]);
      setSummary(summaryData);
      setAlerts(alertsData);
      setGaps(gapsData);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load the Command Centre."));
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (isLoading || !summary) {
    return (
      <Layout title="Command Centre">
        <div className="page-loading">Loading the procurement position…</div>
      </Layout>
    );
  }

  const { vendors, files_today, orders, stock, purchase_orders, delivery, invoices } = summary;
  const problemCount = alerts.filter((a) => a.severity === "error").length;

  return (
    <Layout title="Founder Command Centre">
      <section className="panel">
        <div className="panel__header">
          <h2>Today at a glance</h2>
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <span style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
              {summary.generated_at_ist}
            </span>
            <button type="button" className="btn btn--ghost" onClick={load}>
              Refresh
            </button>
          </div>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
            gap: 12,
          }}
        >
          <KpiCard
            label="Vendors Expected Today"
            value={`${vendors.received} / ${vendors.expected}`}
            sub={
              vendors.pending
                ? `${vendors.pending} pending`
                : "all received"
            }
            tone={vendors.pending ? SEVERITY_COLORS.warning : "#16a34a"}
            onClick={() => navigate("/vendor-inventory")}
          />
          <KpiCard
            label="Customer Orders Today"
            value={orders.orders_today}
            sub={`${orders.lines_today} line(s)`}
            tone="#2563eb"
            onClick={() => navigate("/customer-orders")}
          />
          <KpiCard
            label="Qty Ordered Today"
            value={orders.qty_ordered_today}
            sub={`allocated ${orders.qty_allocated_today}`}
            tone="#2563eb"
            onClick={() => navigate("/customer-orders")}
          />
          <KpiCard
            label="Fill Rate Today"
            value={orders.fill_rate_pct === null ? "—" : `${orders.fill_rate_pct}%`}
            sub={`short ${orders.qty_short_today}`}
            tone={
              orders.qty_short_today > 0 ? SEVERITY_COLORS.error : "#16a34a"
            }
            onClick={() => navigate("/vendor-comparison")}
          />
          <KpiCard
            label="At-Risk Orders (7d)"
            value={orders.at_risk_orders}
            sub="orders still short"
            tone={orders.at_risk_orders ? SEVERITY_COLORS.error : "#16a34a"}
            onClick={() => navigate("/customer-orders")}
          />
          <KpiCard
            label="Live Remaining Stock"
            value={stock.live_remaining}
            sub={`${stock.total_quantity} imported − ${stock.reserved_quantity} reserved`}
            tone="#0891b2"
            onClick={() => navigate("/vendor-comparison")}
          />
          <KpiCard
            label="Active Parts / Vendors"
            value={stock.distinct_parts}
            sub={`${stock.vendors_with_stock} vendor(s) with stock`}
            tone="#0891b2"
            onClick={() => navigate("/vendor-inventory")}
          />
          <KpiCard
            label="POs Today / MTD"
            value={`${purchase_orders.created_today} / ${purchase_orders.created_mtd}`}
            sub={
              purchase_orders.email_failed
                ? `${purchase_orders.email_failed} email(s) failed`
                : `qty ${purchase_orders.ordered_qty_today} today`
            }
            tone={purchase_orders.email_failed ? SEVERITY_COLORS.warning : "#7c3aed"}
            onClick={() => navigate("/purchase-orders")}
          />
          <KpiCard
            label="Delivery Outstanding"
            value={delivery.short_qty}
            sub={`${delivery.delivered_qty} of ${delivery.ordered_qty} delivered${
              delivery.fulfilment_pct !== null ? ` (${delivery.fulfilment_pct}%)` : ""
            }`}
            tone={delivery.short_qty > 0 ? SEVERITY_COLORS.warning : "#16a34a"}
            onClick={() => navigate("/delivery-tracking")}
          />
          <KpiCard
            label="Invoice Mismatches (7d)"
            value={
              invoices.short_supply +
              invoices.extra_supply +
              invoices.missing_part +
              invoices.unexpected_part
            }
            sub={`${invoices.matched_lines} matched, ${invoices.needs_review} need review`}
            tone={
              invoices.short_supply + invoices.missing_part > 0
                ? SEVERITY_COLORS.warning
                : "#16a34a"
            }
            onClick={() => navigate("/vendor-invoices")}
          />
          <KpiCard
            label="Files Today"
            value={files_today.received}
            sub={`${files_today.processed} ok · ${files_today.failed} failed · ${files_today.needs_review} review`}
            tone={files_today.failed ? SEVERITY_COLORS.error : "#16a34a"}
            onClick={() => navigate("/file-inbox")}
          />
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>
            Action Required{" "}
            {problemCount > 0 && (
              <span style={{ color: SEVERITY_COLORS.error }}>({problemCount} critical)</span>
            )}
          </h2>
        </div>
        {alerts.length === 0 ? (
          <EmptyState
            title="Nothing needs you right now"
            description="No failures, shortages, pending vendors or mismatches."
          />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {alerts.map((alert, index) => (
              <div
                key={`${alert.type}-${index}`}
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 12,
                  border: "1px solid var(--color-border, #e5e7eb)",
                  borderLeft: `4px solid ${SEVERITY_COLORS[alert.severity] || "#6b7280"}`,
                  borderRadius: 8,
                  padding: "10px 14px",
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600 }}>{alert.title}</div>
                  <div
                    style={{
                      fontSize: "0.85rem",
                      color: "var(--color-text-muted)",
                      whiteSpace: "normal",
                    }}
                  >
                    {alert.detail}
                    {alert.age_hours !== null && alert.age_hours !== undefined && (
                      <> · {alert.age_hours}h old</>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => navigate(alert.link)}
                >
                  Open
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>Stock vs Demand — parts that cannot be fulfilled</h2>
        </div>
        {gaps.length === 0 ? (
          <EmptyState
            title="No unfulfillable demand"
            description="Every recent order line is covered by live vendor stock."
          />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Part</th>
                  <th>Vendor Stock</th>
                  <th>Reserved</th>
                  <th>Live Remaining</th>
                  <th>Demand</th>
                  <th>Allocated</th>
                  <th>Short</th>
                  <th>Gap</th>
                  <th>Vendors</th>
                </tr>
              </thead>
              <tbody>
                {gaps.map((row) => (
                  <tr key={row.part_number}>
                    <td>{row.part_number}</td>
                    <td>{row.vendor_stock}</td>
                    <td>{row.reserved}</td>
                    <td>{row.live_remaining}</td>
                    <td>{row.demand}</td>
                    <td>{row.allocated}</td>
                    <td>{row.short}</td>
                    <td
                      style={{
                        color: row.gap < 0 ? SEVERITY_COLORS.error : "#16a34a",
                        fontWeight: 600,
                      }}
                    >
                      {row.gap}
                    </td>
                    <td>{row.vendors.join(", ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </Layout>
  );
}
