import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import {
  getCommandCentreAlerts,
  getCommandCentreDeliveryTower,
  getCommandCentreFunnel,
  getCommandCentreOrderTower,
  getCommandCentrePoTower,
  getCommandCentreStockGaps,
  getCommandCentreSummary,
  getCommandCentreTrends,
  getCommandCentreVendorScorecard,
  getPriceLeakage,
  getTeamActivity,
} from "../api/commandCentre";

const PERIODS = [
  { days: 1, label: "Today" },
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
];

function StatRow({ label, value, tone }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0" }}>
      <span style={{ color: "var(--color-text-muted)", fontSize: "0.85rem" }}>{label}</span>
      <span style={{ fontWeight: 600, color: tone }}>{value}</span>
    </div>
  );
}

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
  const [funnel, setFunnel] = useState([]);
  const [orderTower, setOrderTower] = useState(null);
  const [poTower, setPoTower] = useState(null);
  const [deliveryTower, setDeliveryTower] = useState(null);
  const [scorecard, setScorecard] = useState([]);
  const [trends, setTrends] = useState([]);
  const [leakage, setLeakage] = useState(null);
  const [teamActivity, setTeamActivity] = useState([]);
  const [days, setDays] = useState(7);
  const [isLoading, setIsLoading] = useState(true);

  async function load(windowDays) {
    setIsLoading(true);
    try {
      const [
        summaryData,
        alertsData,
        gapsData,
        funnelData,
        orderData,
        poData,
        deliveryData,
        scorecardData,
        trendsData,
        leakageData,
        teamActivityData,
      ] = await Promise.all([
        getCommandCentreSummary(),
        getCommandCentreAlerts(),
        getCommandCentreStockGaps(),
        getCommandCentreFunnel(windowDays),
        getCommandCentreOrderTower(windowDays),
        getCommandCentrePoTower(windowDays),
        getCommandCentreDeliveryTower(windowDays),
        getCommandCentreVendorScorecard(),
        getCommandCentreTrends(windowDays),
        getPriceLeakage(windowDays),
        getTeamActivity(windowDays),
      ]);
      setSummary(summaryData);
      setAlerts(alertsData);
      setGaps(gapsData);
      setFunnel(funnelData);
      setOrderTower(orderData);
      setPoTower(poData);
      setDeliveryTower(deliveryData);
      setScorecard(scorecardData);
      setTrends(trendsData);
      setLeakage(leakageData);
      setTeamActivity(teamActivityData);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load the Command Centre."));
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    load(days);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

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
            <div style={{ display: "flex", gap: 4 }}>
              {PERIODS.map((period) => (
                <button
                  key={period.days}
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => setDays(period.days)}
                  style={
                    period.days === days
                      ? { fontWeight: 700, textDecoration: "underline" }
                      : undefined
                  }
                >
                  {period.label}
                </button>
              ))}
            </div>
            <button type="button" className="btn btn--ghost" onClick={() => load(days)}>
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

      <section className="panel">
        <div className="panel__header">
          <h2>Procurement Funnel ({days === 1 ? "today" : `${days} days`})</h2>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Stage</th>
                <th>Count</th>
                <th>Quantity</th>
                <th>Conversion</th>
                <th aria-label="Open" />
              </tr>
            </thead>
            <tbody>
              {funnel.map((stage) => {
                const maxQty = Math.max(...funnel.map((s) => s.quantity), 1);
                return (
                  <tr key={stage.stage}>
                    <td>{stage.stage}</td>
                    <td>{stage.count}</td>
                    <td style={{ minWidth: 220 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div
                          style={{
                            height: 10,
                            width: `${Math.max((stage.quantity / maxQty) * 160, 2)}px`,
                            background: "#2563eb",
                            borderRadius: 4,
                          }}
                        />
                        <span>{stage.quantity}</span>
                      </div>
                    </td>
                    <td>{stage.conversion_pct === null ? "—" : `${stage.conversion_pct}%`}</td>
                    <td className="data-table__actions">
                      <button
                        type="button"
                        className="btn btn--ghost"
                        onClick={() => navigate(stage.link)}
                      >
                        Open
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>Control Towers</h2>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: 16,
          }}
        >
          {orderTower && (
            <div style={{ border: "1px solid var(--color-border, #e5e7eb)", borderRadius: 10, padding: 14 }}>
              <h3 style={{ marginTop: 0 }}>Customer Orders</h3>
              <StatRow label="Total in window" value={orderTower.total} />
              <StatRow label="Fully allocated" value={orderTower.fully_allocated} tone="#16a34a" />
              <StatRow label="Partially allocated" value={orderTower.partially_allocated} tone="#d97706" />
              <StatRow label="Unallocated" value={orderTower.unallocated} tone="#dc2626" />
              <StatRow label="Awaiting PO" value={orderTower.awaiting_po} />
              <div style={{ marginTop: 8, fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                Still-short order age:
              </div>
              {Object.entries(orderTower.ageing).map(([bucket, count]) => (
                <StatRow key={bucket} label={bucket} value={count} tone={count ? "#dc2626" : undefined} />
              ))}
            </div>
          )}
          {poTower && (
            <div style={{ border: "1px solid var(--color-border, #e5e7eb)", borderRadius: 10, padding: 14 }}>
              <h3 style={{ marginTop: 0 }}>Purchase Orders</h3>
              <StatRow label="Created in window" value={poTower.created_in_window} />
              <StatRow label="Emailed" value={poTower.emailed} tone="#16a34a" />
              <StatRow label="Generated (not emailed)" value={poTower.generated} />
              <StatRow label="Email failed" value={poTower.email_failed} tone={poTower.email_failed ? "#dc2626" : undefined} />
              <StatRow label="Fully supplied" value={poTower.complete} tone="#16a34a" />
              <StatRow label="Partially supplied" value={poTower.partial} tone="#d97706" />
              <StatRow label="Not supplied" value={poTower.not_supplied} tone={poTower.not_supplied ? "#dc2626" : undefined} />
              <div style={{ marginTop: 8, fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                Unsupplied PO age:
              </div>
              {Object.entries(poTower.ageing).map(([bucket, count]) => (
                <StatRow key={bucket} label={bucket} value={count} tone={count ? "#dc2626" : undefined} />
              ))}
            </div>
          )}
          {deliveryTower && (
            <div style={{ border: "1px solid var(--color-border, #e5e7eb)", borderRadius: 10, padding: 14 }}>
              <h3 style={{ marginTop: 0 }}>Deliveries</h3>
              <StatRow label="Ordered qty" value={deliveryTower.ordered_qty} />
              <StatRow label="Delivered qty" value={deliveryTower.delivered_qty} tone="#16a34a" />
              <StatRow
                label="Short qty"
                value={deliveryTower.short_qty}
                tone={deliveryTower.short_qty ? "#dc2626" : undefined}
              />
              <StatRow
                label="Fulfilment"
                value={deliveryTower.fulfilment_pct === null ? "—" : `${deliveryTower.fulfilment_pct}%`}
              />
              <StatRow label="Complete / Partial / None" value={`${deliveryTower.complete_lines} / ${deliveryTower.partial_lines} / ${deliveryTower.not_delivered_lines}`} />
              {deliveryTower.vendor_short.length > 0 && (
                <>
                  <div style={{ marginTop: 8, fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                    Vendor short supply:
                  </div>
                  {deliveryTower.vendor_short.slice(0, 5).map((row) => (
                    <StatRow key={row.vendor} label={row.vendor} value={row.short_qty} tone="#dc2626" />
                  ))}
                </>
              )}
            </div>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>Vendor Scorecard</h2>
        </div>
        {scorecard.length === 0 ? (
          <EmptyState title="No vendor activity yet" description="Scores appear once vendors declare stock and receive allocations." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Vendor</th>
                  <th>Score</th>
                  <th>Trust</th>
                  <th>Fulfilment</th>
                  <th>Declared</th>
                  <th>Allocated</th>
                  <th>Invoiced</th>
                  <th>Delivered</th>
                  <th>Short</th>
                  <th>Uploads (30d)</th>
                </tr>
              </thead>
              <tbody>
                {scorecard.map((row) => (
                  <tr key={row.vendor_id}>
                    <td>{row.rank ?? "—"}</td>
                    <td>
                      {row.vendor}
                      {row.vendor_code ? ` (${row.vendor_code})` : ""}
                    </td>
                    <td style={{ fontWeight: 700 }}>{row.score ?? "—"}</td>
                    <td
                      style={{
                        color:
                          row.trust_pct !== null && row.trust_pct < 80 ? "#dc2626" : "#16a34a",
                      }}
                    >
                      {row.trust_pct === null ? "—" : `${row.trust_pct}%`}
                    </td>
                    <td>{row.fulfilment_pct === null ? "—" : `${row.fulfilment_pct}%`}</td>
                    <td>{row.declared_qty}</td>
                    <td>{row.allocated_qty}</td>
                    <td>{row.invoiced_qty}</td>
                    <td>{row.delivered_qty}</td>
                    <td style={{ color: row.short_qty ? "#dc2626" : undefined }}>{row.short_qty}</td>
                    <td>{row.submissions_30d}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>Team Activity ({days === 1 ? "today" : `${days} days`})</h2>
        </div>
        {teamActivity.length === 0 ? (
          <EmptyState
            title="No WhatsApp activity in this window"
            description="Counts appear per sender as files arrive."
          />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Who</th>
                  <th>Number</th>
                  <th>Type</th>
                  <th>Files Sent</th>
                  <th>Orders</th>
                  <th>Stock Files</th>
                  <th>Failed</th>
                  <th>Last Activity</th>
                </tr>
              </thead>
              <tbody>
                {teamActivity.map((row) => (
                  <tr key={row.number}>
                    <td style={{ fontWeight: row.kind === "team" ? 700 : 400 }}>{row.name}</td>
                    <td>{row.number}</td>
                    <td>{row.kind}</td>
                    <td style={{ fontWeight: 600 }}>{row.files_sent}</td>
                    <td>{row.orders_sent}</td>
                    <td>{row.stock_files_sent}</td>
                    <td style={{ color: row.failed_files ? "#dc2626" : undefined }}>
                      {row.failed_files}
                    </td>
                    <td>{row.last_activity ? row.last_activity.slice(0, 16).replace("T", " ") : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {leakage && (
        <section className="panel">
          <div className="panel__header">
            <h2>Price Leakage ({days === 1 ? "today" : `${days} days`})</h2>
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
              gap: 12,
              marginBottom: 12,
            }}
          >
            <div>
              <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
                Purchase value (priced)
              </div>
              <div style={{ fontSize: "1.3rem", fontWeight: 700 }}>
                ₹{leakage.purchase_value_priced}
              </div>
            </div>
            <div>
              <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
                Price coverage
              </div>
              <div style={{ fontSize: "1.3rem", fontWeight: 700 }}>
                {leakage.priced_coverage_pct === null ? "—" : `${leakage.priced_coverage_pct}%`}
              </div>
            </div>
            <div>
              <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
                Potential leakage
              </div>
              <div
                style={{
                  fontSize: "1.3rem",
                  fontWeight: 700,
                  color: leakage.potential_leakage > 0 ? "#dc2626" : "#16a34a",
                }}
              >
                ₹{leakage.potential_leakage}
              </div>
            </div>
          </div>
          <p style={{ fontSize: "0.8rem", color: "var(--color-text-muted)", marginBottom: 12 }}>
            {leakage.note}
          </p>
          {leakage.rows.length > 0 && (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Part</th>
                    <th>Selected Vendor</th>
                    <th>Selected ₹</th>
                    <th>Best Vendor</th>
                    <th>Best ₹</th>
                    <th>Qty</th>
                    <th>Leakage</th>
                  </tr>
                </thead>
                <tbody>
                  {leakage.rows.map((row, index) => (
                    <tr key={`${row.part_number}-${index}`}>
                      <td>{row.part_number}</td>
                      <td>{row.selected_vendor}</td>
                      <td>₹{row.selected_price}</td>
                      <td>{row.best_vendor}</td>
                      <td>₹{row.best_price}</td>
                      <td>{row.quantity}</td>
                      <td style={{ color: "#dc2626", fontWeight: 600 }}>₹{row.leakage}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <section className="panel">
        <div className="panel__header">
          <h2>Trends ({days === 1 ? "today" : `last ${days} days`})</h2>
        </div>
        {trends.length === 0 ? (
          <EmptyState title="No trend data yet" description="Charts fill in as daily activity accumulates." />
        ) : (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
              gap: 16,
            }}
          >
            <div style={{ height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trends}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="date" fontSize={11} />
                  <YAxis fontSize={11} />
                  <Tooltip />
                  <Legend />
                  <Line type="monotone" dataKey="orders_qty" name="Ordered" stroke="#2563eb" dot={false} />
                  <Line type="monotone" dataKey="allocated_qty" name="Allocated" stroke="#16a34a" dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div style={{ height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={trends}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="date" fontSize={11} />
                  <YAxis fontSize={11} />
                  <Tooltip />
                  <Legend />
                  <Bar dataKey="stock_qty" name="Stock received" fill="#0891b2" />
                  <Bar dataKey="files_failed" name="Files failed" fill="#dc2626" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}
      </section>
    </Layout>
  );
}
