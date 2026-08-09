import { formatDateTime } from "../utils/datetime";
import { useEffect, useRef, useState } from "react";
import { Layout } from "../components/Layout";
import { Modal } from "../components/Modal";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import {
  listVendorInvoiceImports,
  listVendorInvoiceLines,
  uploadVendorInvoices,
} from "../api/vendorInvoices";

const ACCEPTED_EXTENSIONS = [".pdf"];

function InvoiceLinesModal({ invoiceImportId, fileName, onClose }) {
  const [lines, setLines] = useState(null);
  const toast = useToast();

  useEffect(() => {
    let isMounted = true;
    listVendorInvoiceLines(invoiceImportId)
      .then((data) => isMounted && setLines(data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load invoice lines.")));
    return () => {
      isMounted = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invoiceImportId]);

  return (
    <Modal title={`Invoice Lines — ${fileName}`} onClose={onClose} width={720}>
      {!lines ? (
        <p>Loading…</p>
      ) : lines.length === 0 ? (
        <p>No line items were extracted from this invoice.</p>
      ) : (
        <div className="table-scroll table-scroll--modal">
          <table className="data-table">
            <thead>
              <tr>
                <th>Part Number</th>
                <th>Invoiced Qty</th>
                <th>Expected Qty</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <tr key={line.id}>
                  <td>{line.part_number_raw}</td>
                  <td>{line.quantity_invoiced}</td>
                  <td>{line.expected_quantity ?? "—"}</td>
                  <td>
                    <StatusPill status={line.discrepancy_type} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}

export function VendorInvoicesPage() {
  const toast = useToast();
  const fileInputRef = useRef(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [lastResults, setLastResults] = useState([]);
  const [history, setHistory] = useState([]);
  const [isHistoryLoading, setIsHistoryLoading] = useState(true);
  const [linesModalInvoice, setLinesModalInvoice] = useState(null);

  async function loadHistory() {
    setIsHistoryLoading(true);
    try {
      const data = await listVendorInvoiceImports();
      setHistory(data);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load vendor invoice history."));
    } finally {
      setIsHistoryLoading(false);
    }
  }

  useEffect(() => {
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;

    setIsUploading(true);
    setLastResults([]);
    try {
      const results = await uploadVendorInvoices(files);
      setLastResults(results);

      const needsReview = results.filter((r) => r.status === "NEEDS_REVIEW").length;
      const succeeded = results.length - needsReview;
      if (needsReview === 0) {
        toast.success(`${succeeded} invoice(s) verified.`);
      } else {
        toast.error(`${needsReview} invoice(s) need manual review. See details below.`);
      }
      loadHistory();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Upload failed."));
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function handleDrop(event) {
    event.preventDefault();
    setIsDragging(false);
    handleFiles(event.dataTransfer.files);
  }

  return (
    <Layout title="Vendor Invoices">
      <section className="panel">
        <div className="panel__header">
          <h2>Upload Vendor Invoice</h2>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          Upload a vendor's invoice PDF to verify delivered part numbers and quantities
          against the vendors selected on Vendor Comparison, and update Delivery Tracking
          and Vendor Performance automatically.
        </p>

        <div
          className={"dropzone" + (isDragging ? " dropzone--active" : "")}
          onDragOver={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={handleDrop}
        >
          <p className="dropzone__title">Drag &amp; drop the invoice PDF here</p>
          <p className="dropzone__hint">
            Supported: {ACCEPTED_EXTENSIONS.join(", ")} (text-based only — scanned/image PDFs
            aren't supported yet) — one or multiple files at once
          </p>
          <button
            type="button"
            className="btn btn--primary btn--large"
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploading}
          >
            {isUploading ? "Uploading…" : "Choose File"}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ACCEPTED_EXTENSIONS.join(",")}
            hidden
            onChange={(event) => handleFiles(event.target.files)}
          />
        </div>

        {lastResults.length > 0 && (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Vendor</th>
                  <th>Rows</th>
                  <th>Discrepancies</th>
                  <th>Status</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {lastResults.map((result, index) => (
                  <tr key={`${result.invoice_import_id}-${index}`}>
                    <td>{result.file_name}</td>
                    <td>{result.vendor_name || "—"}</td>
                    <td>{result.row_count}</td>
                    <td>{result.error_count}</td>
                    <td>
                      <StatusPill status={result.status} />
                    </td>
                    <td>{result.message || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>Invoice History</h2>
        </div>

        {isHistoryLoading ? (
          <div className="page-loading">Loading history…</div>
        ) : history.length === 0 ? (
          <EmptyState title="No vendor invoices yet" description="Upload an invoice PDF to get started." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Vendor</th>
                  <th>Rows</th>
                  <th>Discrepancies</th>
                  <th>Status</th>
                  <th>Uploaded</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {history.map((row) => (
                  <tr key={row.id}>
                    <td>{row.file_name}</td>
                    <td>{row.vendor_name || row.vendor_name_extracted || "—"}</td>
                    <td>{row.row_count}</td>
                    <td>{row.error_count}</td>
                    <td>
                      <StatusPill status={row.status} />
                    </td>
                    <td>{formatDateTime(row.created_at)}</td>
                    <td className="data-table__actions">
                      <button
                        type="button"
                        className="btn btn--ghost"
                        onClick={() => setLinesModalInvoice({ id: row.id, fileName: row.file_name })}
                      >
                        View Lines
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {linesModalInvoice && (
        <InvoiceLinesModal
          invoiceImportId={linesModalInvoice.id}
          fileName={linesModalInvoice.fileName}
          onClose={() => setLinesModalInvoice(null)}
        />
      )}
    </Layout>
  );
}
