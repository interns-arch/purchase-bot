import { formatDateTime } from "../utils/datetime";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Layout } from "../components/Layout";
import { Modal } from "../components/Modal";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { listImportErrors, listImportHistory, uploadInventoryFiles } from "../api/inventory";

const ACCEPTED_EXTENSIONS = [".csv", ".xlsx", ".xlsm", ".xls"];

function ImportErrorsModal({ importId, fileName, onClose }) {
  const [errors, setErrors] = useState(null);
  const toast = useToast();

  useEffect(() => {
    let isMounted = true;
    listImportErrors(importId)
      .then((data) => isMounted && setErrors(data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load validation errors.")));
    return () => {
      isMounted = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [importId]);

  return (
    <Modal title={`Validation Errors — ${fileName}`} onClose={onClose} width={640}>
      {!errors ? (
        <p>Loading…</p>
      ) : errors.length === 0 ? (
        <p>No row-level errors recorded for this import.</p>
      ) : (
        <div className="table-scroll table-scroll--modal">
          <table className="data-table">
            <thead>
              <tr>
                <th>Row</th>
                <th>Reason</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {errors.map((row) => (
                <tr key={row.id}>
                  <td>{row.row_number ?? "—"}</td>
                  <td>{row.error_reason}</td>
                  <td>{row.error_detail || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}

export function VendorInventoryPage() {
  const toast = useToast();
  const fileInputRef = useRef(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [lastResults, setLastResults] = useState([]);
  const [history, setHistory] = useState([]);
  const [historySearch, setHistorySearch] = useState("");
  const [isHistoryLoading, setIsHistoryLoading] = useState(true);
  const [errorsModalImport, setErrorsModalImport] = useState(null);

  const loadHistory = useCallback(async () => {
    setIsHistoryLoading(true);
    try {
      const data = await listImportHistory();
      setHistory(data);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load import history."));
    } finally {
      setIsHistoryLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  const filteredHistory = useMemo(() => {
    const query = historySearch.trim().toLowerCase();
    if (!query) return history;
    return history.filter(
      (row) =>
        row.file_name.toLowerCase().includes(query) ||
        row.vendor_name.toLowerCase().includes(query)
    );
  }, [history, historySearch]);

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;

    setIsUploading(true);
    setUploadProgress(0);
    setLastResults([]);
    try {
      const results = await uploadInventoryFiles(files, { onProgress: setUploadProgress });
      setLastResults(results);

      const failed = results.filter((r) => r.status === "FAILED").length;
      const skipped = results.filter((r) => r.status === "SKIPPED_DUPLICATE").length;
      const succeeded = results.length - failed - skipped;

      if (failed === 0) {
        toast.success(
          `${succeeded} file(s) imported${skipped ? `, ${skipped} skipped as unchanged` : ""}.`
        );
      } else {
        toast.error(`${failed} file(s) failed to import. See details below.`);
      }
      loadHistory();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Upload failed."));
    } finally {
      setIsUploading(false);
      setUploadProgress(0);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function handleDrop(event) {
    event.preventDefault();
    setIsDragging(false);
    handleFiles(event.dataTransfer.files);
  }

  return (
    <Layout title="Vendor Inventory">
      <section className="panel">
        <div className="panel__header">
          <h2>Upload Vendor Inventory Files</h2>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          A vendor's first upload can be named with their real company name (e.g. "Arvind Auto
          Parts.xlsx") -- a Vendor Code is generated automatically and shown in the message below.
          Every upload after that must be prefixed with that code (e.g. "AR_CT_Inventory.xlsx").
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
          <p className="dropzone__title">Drag &amp; drop CSV / Excel files here</p>
          <p className="dropzone__hint">
            Supported: {ACCEPTED_EXTENSIONS.join(", ")} — one or multiple files at once
          </p>
          <button
            type="button"
            className="btn btn--primary btn--large"
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploading}
          >
            {isUploading ? "Uploading…" : "Choose Files"}
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

        {isUploading && (
          <div className="progress-bar" role="progressbar" aria-valuenow={uploadProgress}>
            <div className="progress-bar__fill" style={{ width: `${uploadProgress}%` }} />
          </div>
        )}

        {lastResults.length > 0 && (
          <>
            <p style={{ color: "var(--color-text-muted)", fontSize: "0.88rem", marginBottom: 8 }}>
              Import summary: {lastResults.filter((r) => r.status !== "FAILED" && r.status !== "SKIPPED_DUPLICATE").length}{" "}
              imported, {lastResults.filter((r) => r.status === "SKIPPED_DUPLICATE").length} skipped (unchanged),{" "}
              {lastResults.filter((r) => r.status === "FAILED").length} failed.
            </p>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>File</th>
                    <th>Vendor</th>
                    <th>Rows</th>
                    <th>Errors</th>
                    <th>Status</th>
                    <th>Message</th>
                  </tr>
                </thead>
                <tbody>
                  {lastResults.map((result, index) => (
                    <tr key={`${result.import_id}-${index}`}>
                      <td>{result.file_name}</td>
                      <td>{result.vendor_name}</td>
                      <td>{result.row_count}</td>
                      <td>
                        {result.error_count > 0 ? (
                          <button
                            type="button"
                            className="link-button"
                            onClick={() =>
                              setErrorsModalImport({ id: result.import_id, fileName: result.file_name })
                            }
                          >
                            {result.error_count} row(s)
                          </button>
                        ) : (
                          0
                        )}
                      </td>
                      <td>
                        <StatusPill status={result.status} />
                      </td>
                      <td>{result.message || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>Import History</h2>
        </div>
        <div className="toolbar">
          <input
            className="field__input toolbar__search"
            type="search"
            placeholder="Search by vendor or file name…"
            value={historySearch}
            onChange={(event) => setHistorySearch(event.target.value)}
          />
        </div>

        {isHistoryLoading ? (
          <div className="page-loading">Loading history…</div>
        ) : filteredHistory.length === 0 ? (
          <EmptyState title="No import history yet" description="Uploaded files will show up here." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Vendor</th>
                  <th>Vendor Code</th>
                  <th>File</th>
                  <th>Rows</th>
                  <th>Errors</th>
                  <th>Active batch</th>
                  <th>Status</th>
                  <th>Imported</th>
                </tr>
              </thead>
              <tbody>
                {filteredHistory.map((row) => (
                  <tr key={row.id}>
                    <td>{row.vendor_name}</td>
                    <td>{row.vendor_code || "—"}</td>
                    <td>{row.file_name}</td>
                    <td>{row.row_count}</td>
                    <td>
                      {row.error_count > 0 ? (
                        <button
                          type="button"
                          className="link-button"
                          onClick={() => setErrorsModalImport({ id: row.id, fileName: row.file_name })}
                        >
                          {row.error_count} row(s)
                        </button>
                      ) : (
                        0
                      )}
                    </td>
                    <td>{row.is_active ? "Yes" : "No"}</td>
                    <td>
                      <StatusPill status={row.status} />
                    </td>
                    <td>{formatDateTime(row.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {errorsModalImport && (
        <ImportErrorsModal
          importId={errorsModalImport.id}
          fileName={errorsModalImport.fileName}
          onClose={() => setErrorsModalImport(null)}
        />
      )}
    </Layout>
  );
}
