import { useEffect, useState } from "react";
import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { getAuditLog } from "../api/commandCentre";
import { formatDateTime } from "../utils/datetime";

const ACTION_LABELS = {
  manual_vendor_select: "Manual vendor selection",
  manual_vendor_deselect: "Manual vendor removal",
  data_purge: "Data purge (Danger Zone)",
  contact_registry_update: "Vendor contacts updated (WhatsApp)",
};

export function AuditLogPage() {
  const toast = useToast();
  const [rows, setRows] = useState([]);
  const [isLoading, setIsLoading] = useState(true);

  async function load() {
    setIsLoading(true);
    try {
      setRows(await getAuditLog(200));
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load the audit log."));
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Layout title="Audit Log">
      <section className="panel">
        <div className="panel__header">
          <h2>Manual actions — who changed what, and when</h2>
          <button type="button" className="btn btn--ghost" onClick={load} disabled={isLoading}>
            Refresh
          </button>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          Automatic pipeline activity lives in Import History and the File Inbox; this trail is
          only for human overrides. Rows are append-only — nothing here can be edited or deleted.
        </p>
        {isLoading ? (
          <div className="page-loading">Loading audit trail…</div>
        ) : rows.length === 0 ? (
          <EmptyState
            title="No manual actions recorded yet"
            description="Manual vendor selections, purges and contact updates will appear here."
          />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Who</th>
                  <th>Action</th>
                  <th>Entity</th>
                  <th>Before</th>
                  <th>After</th>
                  <th>Context</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td>{formatDateTime(row.created_at)}</td>
                    <td>{row.actor}</td>
                    <td>{ACTION_LABELS[row.action] || row.action}</td>
                    <td>
                      {row.entity_type}
                      {row.entity_id ? ` #${row.entity_id}` : ""}
                    </td>
                    <td style={{ maxWidth: 260, whiteSpace: "normal", fontSize: "0.8rem" }}>
                      {row.previous_value || "—"}
                    </td>
                    <td style={{ maxWidth: 260, whiteSpace: "normal", fontSize: "0.8rem" }}>
                      {row.new_value || "—"}
                    </td>
                    <td style={{ maxWidth: 220, whiteSpace: "normal", fontSize: "0.8rem" }}>
                      {row.reason || "—"}
                    </td>
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
