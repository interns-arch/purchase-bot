import { formatDateTime } from "../utils/datetime";
import { useEffect, useState } from "react";
import { Layout } from "../components/Layout";
import { Modal } from "../components/Modal";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import {
  downloadPurchaseOrder,
  listPurchaseOrderLines,
  listPurchaseOrders,
  resendPurchaseOrderEmail,
} from "../api/purchaseOrders";

function PurchaseOrderLinesModal({ poId, poNumber, onClose }) {
  const [lines, setLines] = useState(null);
  const toast = useToast();

  useEffect(() => {
    let isMounted = true;
    listPurchaseOrderLines(poId)
      .then((data) => isMounted && setLines(data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load purchase order lines.")));
    return () => {
      isMounted = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [poId]);

  return (
    <Modal title={`Purchase Order Lines — ${poNumber}`} onClose={onClose} width={640}>
      {!lines ? (
        <p>Loading…</p>
      ) : lines.length === 0 ? (
        <p>No line items on this purchase order.</p>
      ) : (
        <div className="table-scroll table-scroll--modal">
          <table className="data-table">
            <thead>
              <tr>
                <th>Part Number</th>
                <th>Vendor Part Number</th>
                <th>Quantity</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((line, index) => (
                <tr key={`${line.part_number}-${index}`}>
                  <td>{line.part_number}</td>
                  <td>{line.vendor_part_number}</td>
                  <td>{line.quantity}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}

export function PurchaseOrdersPage() {
  const toast = useToast();
  const [orders, setOrders] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [linesModalOrder, setLinesModalOrder] = useState(null);
  const [resendingId, setResendingId] = useState(null);

  async function load() {
    setIsLoading(true);
    try {
      const data = await listPurchaseOrders();
      setOrders(data);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load purchase orders."));
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleDownload(po) {
    try {
      await downloadPurchaseOrder(po.id, `${po.po_number}.xlsx`);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Download failed."));
    }
  }

  async function handleResendEmail(po) {
    setResendingId(po.id);
    try {
      const updated = await resendPurchaseOrderEmail(po.id);
      setOrders((current) => current.map((row) => (row.id === updated.id ? updated : row)));
      if (updated.status === "EMAILED") {
        toast.success(`Emailed ${updated.po_number} to the purchase team.`);
      } else {
        toast.error(`Could not email ${updated.po_number} — check the mail configuration and try again.`);
      }
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not resend the purchase order email."));
    } finally {
      setResendingId(null);
    }
  }

  return (
    <Layout title="Purchase Orders">
      <section className="panel">
        <div className="panel__header">
          <h2>Purchase Order History</h2>
          <button type="button" className="btn btn--ghost" onClick={load} disabled={isLoading}>
            Refresh
          </button>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          Purchase Orders are generated automatically after Automatic Vendor Selection, one per
          vendor. They're for internal review only -- vendor-direct delivery isn't enabled yet.
        </p>

        {isLoading ? (
          <div className="page-loading">Loading purchase orders…</div>
        ) : orders.length === 0 ? (
          <EmptyState
            title="No purchase orders yet"
            description="Run Automatic Vendor Selection on a customer order to generate one."
          />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>PO Number</th>
                  <th>Vendor</th>
                  <th>Vendor Code</th>
                  <th>Customer Order</th>
                  <th>Status</th>
                  <th>Created</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {orders.map((po) => (
                  <tr key={po.id}>
                    <td>{po.po_number}</td>
                    <td>{po.vendor_name}</td>
                    <td>{po.vendor_code || "—"}</td>
                    <td>{po.customer_order_id}</td>
                    <td>
                      <StatusPill status={po.status} />
                    </td>
                    <td>{formatDateTime(po.created_at)}</td>
                    <td className="data-table__actions">
                      <button
                        type="button"
                        className="btn btn--ghost"
                        onClick={() => setLinesModalOrder({ id: po.id, poNumber: po.po_number })}
                      >
                        View Lines
                      </button>
                      <button type="button" className="btn btn--ghost" onClick={() => handleDownload(po)}>
                        Download
                      </button>
                      <button
                        type="button"
                        className="btn btn--ghost"
                        onClick={() => handleResendEmail(po)}
                        disabled={resendingId === po.id}
                        title="Re-send this purchase order's internal review email to the purchase team"
                      >
                        {resendingId === po.id ? "Resending…" : "Resend Email"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {linesModalOrder && (
        <PurchaseOrderLinesModal
          poId={linesModalOrder.id}
          poNumber={linesModalOrder.poNumber}
          onClose={() => setLinesModalOrder(null)}
        />
      )}
    </Layout>
  );
}
