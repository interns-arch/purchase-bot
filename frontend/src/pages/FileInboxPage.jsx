import { useEffect, useState } from "react";
import { Layout } from "../components/Layout";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { downloadDocument, listDocuments } from "../api/documents";
import { formatDateTime } from "../utils/datetime";

// Every status the backend records, in "what broke?" first order.
const STATUS_FILTERS = [
  { value: "", label: "All files" },
  { value: "FAILED", label: "Failed" },
  { value: "NEEDS_REVIEW", label: "Needs review" },
  { value: "DOWNLOAD_FAILED", label: "Download failed" },
  { value: "UNSUPPORTED", label: "Unsupported" },
  { value: "SKIPPED_DUPLICATE", label: "Duplicates (skipped)" },
  { value: "PROCESSED_WITH_ERRORS", label: "Processed with errors" },
  { value: "PROCESSED", label: "Processed" },
];

export function FileInboxPage() {
  const toast = useToast();
  const [documents, setDocuments] = useState([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [downloadingId, setDownloadingId] = useState(null);

  async function load(status) {
    setIsLoading(true);
    try {
      const data = await listDocuments({ status: status || undefined, limit: 100 });
      setDocuments(data);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load the file inbox."));
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    load(statusFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  async function handleDownload(row) {
    setDownloadingId(row.id);
    try {
      await downloadDocument(row.id, row.filename);
    } catch (error) {
      toast.error(
        extractErrorMessage(
          error,
          "Download failed — the file may no longer be stored on the server."
        )
      );
    } finally {
      setDownloadingId(null);
    }
  }

  return (
    <Layout title="File Inbox">
      <section className="panel">
        <div className="panel__header">
          <h2>Every file received — WhatsApp, Gmail, manual upload</h2>
          <div style={{ display: "flex", gap: 8 }}>
            <select
              className="input"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
            >
              {STATUS_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => load(statusFilter)}
              disabled={isLoading}
            >
              Refresh
            </button>
          </div>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          Download the exact file a vendor or customer sent — especially a failed one — to
          inspect it yourself. Files received before the last server restart may no longer be
          stored (the Download button is disabled for those).
        </p>

        {isLoading ? (
          <div className="page-loading">Loading files…</div>
        ) : documents.length === 0 ? (
          <EmptyState
            title="No files here"
            description={
              statusFilter
                ? "No files with this status. Try 'All files'."
                : "Files will appear as soon as anything is received on WhatsApp, Gmail, or upload."
            }
          />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Received</th>
                  <th>File</th>
                  <th>Source</th>
                  <th>Type</th>
                  <th>Sender</th>
                  <th>Status</th>
                  <th>Reason</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {documents.map((row) => (
                  <tr key={row.id}>
                    <td>{formatDateTime(row.received_at)}</td>
                    <td>{row.filename}</td>
                    <td>{row.source}</td>
                    <td>{row.document_type.replaceAll("_", " ")}</td>
                    <td>{row.sender || "—"}</td>
                    <td>
                      <StatusPill status={row.status} />
                    </td>
                    <td
                      style={{ maxWidth: 340, whiteSpace: "normal", fontSize: "0.85rem" }}
                      title={row.error_message || ""}
                    >
                      {row.error_message || "—"}
                    </td>
                    <td className="data-table__actions">
                      <button
                        type="button"
                        className="btn btn--ghost"
                        onClick={() => handleDownload(row)}
                        disabled={!row.file_available || downloadingId === row.id}
                        title={
                          row.file_available
                            ? "Download the exact file that was received"
                            : "No longer stored on the server (cleared by a restart/redeploy)"
                        }
                      >
                        {downloadingId === row.id ? "Downloading…" : "Download"}
                      </button>
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
