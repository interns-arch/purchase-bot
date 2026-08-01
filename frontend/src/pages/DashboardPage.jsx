import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Layout } from "../components/Layout";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { getDashboardSummary } from "../api/dashboard";
import { extractErrorMessage } from "../api/client";
import { useToast } from "../context/ToastContext";

function StatCard({ label, value, hint }) {
  return (
    <div className="stat-card">
      <p className="stat-card__label">{label}</p>
      <p className="stat-card__value">{value}</p>
      {hint && <p className="stat-card__hint">{hint}</p>}
    </div>
  );
}

const ACTIVITY_LABEL = {
  INVENTORY_IMPORT: "Vendor Inventory",
  CUSTOMER_ORDER: "Customer Order",
};

export function DashboardPage() {
  const toast = useToast();
  const [summary, setSummary] = useState(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let isMounted = true;
    getDashboardSummary()
      .then((data) => isMounted && setSummary(data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load dashboard data.")))
      .finally(() => isMounted && setIsLoading(false));
    return () => {
      isMounted = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Layout title="Dashboard">
      {isLoading ? (
        <div className="page-loading">Loading dashboard…</div>
      ) : (
        <>
          <div className="stat-grid">
            <StatCard label="Active Vendors" value={summary.active_vendors} hint={`${summary.total_vendors} total`} />
            <StatCard label="Inventory Files Imported" value={summary.total_inventory_imports} />
            <StatCard label="Customer Orders Uploaded" value={summary.total_customer_orders} />
            <StatCard label="Parts Matched" value={summary.parts_matched} />
            <StatCard label="Parts Not Found" value={summary.parts_not_found} />
            <StatCard
              label="Last Import Time"
              value={
                summary.last_import_at ? new Date(summary.last_import_at).toLocaleString() : "—"
              }
            />
          </div>

          <section className="panel">
            <div className="panel__header">
              <h2>Recent Activity</h2>
              <div style={{ display: "flex", gap: 8 }}>
                <Link to="/vendor-inventory" className="btn btn--secondary">
                  Upload Vendor Inventory
                </Link>
                <Link to="/customer-orders" className="btn btn--secondary">
                  Upload Customer Order
                </Link>
              </div>
            </div>

            {summary.recent_activity.length === 0 ? (
              <EmptyState
                title="No activity yet"
                description="Upload a vendor inventory file or a customer order to get started."
                action={
                  <Link to="/vendor-inventory" className="btn btn--primary">
                    Upload Vendor Inventory
                  </Link>
                }
              />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Type</th>
                      <th>Vendor</th>
                      <th>File</th>
                      <th>Rows</th>
                      <th>Errors</th>
                      <th>Status</th>
                      <th>When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.recent_activity.map((entry) => (
                      <tr key={`${entry.activity_type}-${entry.reference_id}`}>
                        <td>{ACTIVITY_LABEL[entry.activity_type] || entry.activity_type}</td>
                        <td>{entry.activity_type === "INVENTORY_IMPORT" ? entry.label : "—"}</td>
                        <td>{entry.file_name}</td>
                        <td>{entry.row_count}</td>
                        <td>{entry.error_count}</td>
                        <td>
                          <StatusPill status={entry.status} />
                        </td>
                        <td>{new Date(entry.created_at).toLocaleString()}</td>
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
