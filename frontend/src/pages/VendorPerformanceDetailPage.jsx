import { formatDateTime, formatDate } from "../utils/datetime";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Layout } from "../components/Layout";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { getVendorDetail } from "../api/vendorPerformance";

function StatCard({ label, value }) {
  return (
    <div className="stat-card">
      <p className="stat-card__label">{label}</p>
      <p className="stat-card__value">{value}</p>
    </div>
  );
}

export function VendorPerformanceDetailPage() {
  const { vendorId } = useParams();
  const toast = useToast();
  const [detail, setDetail] = useState(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let isMounted = true;
    setIsLoading(true);
    getVendorDetail(vendorId)
      .then((data) => isMounted && setDetail(data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load vendor detail.")))
      .finally(() => isMounted && setIsLoading(false));
    return () => {
      isMounted = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vendorId]);

  return (
    <Layout title={detail ? detail.vendor_name : "Vendor Detail"}>
      <Link to="/vendor-performance" className="link-button" style={{ display: "inline-block", marginBottom: 12 }}>
        ← Back to Vendor Performance
      </Link>

      {isLoading ? (
        <div className="page-loading">Loading vendor detail…</div>
      ) : !detail ? (
        <EmptyState title="Vendor not found" description="This vendor may have been removed." />
      ) : (
        <>
          {detail.performance && (
            <div className="stat-grid">
              <StatCard label="Parts Allocated" value={detail.performance.parts_allocated} />
              <StatCard label="Ordered Qty" value={detail.performance.ordered_qty} />
              <StatCard label="Delivered Qty" value={detail.performance.delivered_qty} />
              <StatCard label="Fulfillment %" value={`${detail.performance.fulfillment_pct}%`} />
              <StatCard label="Short Qty" value={detail.performance.short_qty} />
              <StatCard label="Accuracy %" value={`${detail.performance.accuracy_pct}%`} />
            </div>
          )}

          <section className="panel">
            <div className="panel__header">
              <h2>Delivery Status by Part</h2>
            </div>
            {detail.delivery_rows.length === 0 ? (
              <EmptyState title="No allocations yet" description="This vendor has no reconciled parts." />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Part</th>
                      <th>Ordered Qty</th>
                      <th>Delivered Qty</th>
                      <th>Short Qty</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.delivery_rows.map((row) => (
                      <tr key={row.part_id}>
                        <td>{row.part_number}</td>
                        <td>{row.ordered_qty}</td>
                        <td>{row.delivered_qty}</td>
                        <td>{row.short_qty}</td>
                        <td>
                          <StatusPill status={row.status} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <div className="panel__header">
              <h2>Every Order Allocated to This Vendor</h2>
            </div>
            {detail.selections.length === 0 ? (
              <EmptyState title="No allocations yet" description="No customer order has selected this vendor." />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Customer Order</th>
                      <th>Part</th>
                      <th>Quantity Selected</th>
                      <th>Selected At</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.selections.map((selection, index) => (
                      <tr key={`${selection.customer_order_id}-${selection.part_number}-${index}`}>
                        <td>{selection.customer_order_file_name}</td>
                        <td>{selection.part_number}</td>
                        <td>{selection.quantity_selected}</td>
                        <td>{formatDateTime(selection.selected_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <div className="panel__header">
              <h2>Every Delivery Recorded</h2>
            </div>
            {detail.deliveries.length === 0 ? (
              <EmptyState title="No deliveries yet" description="No delivery file has referenced this vendor." />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Part</th>
                      <th>Quantity Delivered</th>
                      <th>Delivery Date</th>
                      <th>Source File</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.deliveries.map((delivery, index) => (
                      <tr key={`${delivery.part_number}-${index}`}>
                        <td>{delivery.part_number}</td>
                        <td>{delivery.quantity_delivered}</td>
                        <td>{delivery.delivery_date ? formatDate(delivery.delivery_date) : "—"}</td>
                        <td>{delivery.file_name}</td>
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
