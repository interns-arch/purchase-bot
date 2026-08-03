import { useEffect, useMemo, useState } from "react";
import { Layout } from "../components/Layout";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { listIncomingDocuments } from "../api/documents";

const SOURCE_OPTIONS = ["MANUAL", "WHATSAPP"];
const DOCUMENT_TYPE_OPTIONS = [
  "VENDOR_INVENTORY",
  "CUSTOMER_ORDER",
  "DELIVERY",
  "VENDOR_INVOICE",
  "UNKNOWN",
];
const STATUS_OPTIONS = [
  "RECEIVED",
  "PROCESSED",
  "PROCESSED_WITH_ERRORS",
  "FAILED",
  "SKIPPED_DUPLICATE",
  "NEEDS_REVIEW",
  "UNSUPPORTED",
];

export function DocumentInboxPage() {
  const toast = useToast();
  const [documents, setDocuments] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  useEffect(() => {
    let isMounted = true;
    setIsLoading(true);
    listIncomingDocuments({
      source: sourceFilter || undefined,
      documentType: typeFilter || undefined,
      status: statusFilter || undefined,
    })
      .then((data) => isMounted && setDocuments(data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load documents.")))
      .finally(() => isMounted && setIsLoading(false));
    return () => {
      isMounted = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceFilter, typeFilter, statusFilter]);

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return documents;
    return documents.filter(
      (document) =>
        document.filename.toLowerCase().includes(query) ||
        (document.sender || "").toLowerCase().includes(query)
    );
  }, [documents, search]);

  return (
    <Layout title="Document Inbox">
      <section className="panel">
        <div className="panel__header">
          <h2>Received Documents</h2>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          Every file the system has received -- manual upload or WhatsApp -- appears here first,
          before (and regardless of whether) it was successfully processed.
        </p>

        <div className="toolbar">
          <input
            className="field__input toolbar__search"
            type="search"
            placeholder="Search filename or sender…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <select
            className="field__input"
            style={{ maxWidth: 160 }}
            value={sourceFilter}
            onChange={(event) => setSourceFilter(event.target.value)}
          >
            <option value="">All sources</option>
            {SOURCE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
          <select
            className="field__input"
            style={{ maxWidth: 200 }}
            value={typeFilter}
            onChange={(event) => setTypeFilter(event.target.value)}
          >
            <option value="">All types</option>
            {DOCUMENT_TYPE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option.replaceAll("_", " ")}
              </option>
            ))}
          </select>
          <select
            className="field__input"
            style={{ maxWidth: 200 }}
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            <option value="">All statuses</option>
            {STATUS_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </div>

        {isLoading ? (
          <div className="page-loading">Loading documents…</div>
        ) : filtered.length === 0 ? (
          <EmptyState
            title="No documents yet"
            description="Manual uploads and WhatsApp attachments will show up here as soon as they arrive."
          />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Received</th>
                  <th>Source</th>
                  <th>Type</th>
                  <th>Filename</th>
                  <th>Sender</th>
                  <th>Status</th>
                  <th>Processed</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((document) => (
                  <tr key={document.id}>
                    <td>{new Date(document.received_at).toLocaleString()}</td>
                    <td>{document.source}</td>
                    <td>{document.document_type.replaceAll("_", " ")}</td>
                    <td>{document.filename}</td>
                    <td>{document.sender || "—"}</td>
                    <td>
                      <StatusPill status={document.status} />
                    </td>
                    <td>{document.processed_at ? new Date(document.processed_at).toLocaleString() : "—"}</td>
                    <td>{document.error_message || "—"}</td>
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
