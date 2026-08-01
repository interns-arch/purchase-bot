import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Layout } from "../components/Layout";
import { Modal } from "../components/Modal";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import {
  listCustomerOrderErrors,
  listCustomerOrderItems,
  listCustomerOrders,
  uploadCustomerOrders,
} from "../api/customerOrders";

const ACCEPTED_EXTENSIONS = [".csv", ".xlsx", ".xlsm", ".xls"];

function OrderErrorsModal({ orderId, fileName, onClose }) {
  const [errors, setErrors] = useState(null);
  const toast = useToast();

  useEffect(() => {
    let isMounted = true;
    listCustomerOrderErrors(orderId)
      .then((data) => isMounted && setErrors(data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load validation errors.")));
    return () => {
      isMounted = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderId]);

  return (
    <Modal title={`Validation Errors — ${fileName}`} onClose={onClose} width={640}>
      {!errors ? (
        <p>Loading…</p>
      ) : errors.length === 0 ? (
        <p>No row-level errors recorded for this order.</p>
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

function OrderItemsModal({ orderId, fileName, onClose }) {
  const [items, setItems] = useState(null);
  const toast = useToast();

  useEffect(() => {
    let isMounted = true;
    listCustomerOrderItems(orderId)
      .then((data) => isMounted && setItems(data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load order items.")));
    return () => {
      isMounted = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderId]);

  return (
    <Modal title={`Order Items — ${fileName}`} onClose={onClose} width={480}>
      {!items ? (
        <p>Loading…</p>
      ) : items.length === 0 ? (
        <p>No valid lines were found in this order.</p>
      ) : (
        <div className="table-scroll table-scroll--modal">
          <table className="data-table">
            <thead>
              <tr>
                <th>Part Number</th>
                <th>Requested Qty</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>{item.part_number_raw}</td>
                  <td>{item.quantity_requested}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}

export function CustomerOrdersPage() {
  const toast = useToast();
  const fileInputRef = useRef(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [lastResults, setLastResults] = useState([]);
  const [history, setHistory] = useState([]);
  const [search, setSearch] = useState("");
  const [isHistoryLoading, setIsHistoryLoading] = useState(true);
  const [errorsModalOrder, setErrorsModalOrder] = useState(null);
  const [itemsModalOrder, setItemsModalOrder] = useState(null);

  async function loadHistory() {
    setIsHistoryLoading(true);
    try {
      const data = await listCustomerOrders();
      setHistory(data);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load customer order history."));
    } finally {
      setIsHistoryLoading(false);
    }
  }

  useEffect(() => {
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filteredHistory = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return history;
    return history.filter((row) => row.file_name.toLowerCase().includes(query));
  }, [history, search]);

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;

    setIsUploading(true);
    setLastResults([]);
    try {
      const results = await uploadCustomerOrders(files);
      setLastResults(results);

      const failed = results.filter((r) => r.status === "FAILED").length;
      const skipped = results.filter((r) => r.status === "SKIPPED_DUPLICATE").length;
      const succeeded = results.length - failed - skipped;

      if (failed === 0) {
        toast.success(
          `${succeeded} order(s) imported${skipped ? `, ${skipped} skipped as unchanged` : ""}.`
        );
      } else {
        toast.error(`${failed} file(s) failed to import. See details below.`);
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
    <Layout title="Customer Orders">
      <section className="panel">
        <div className="panel__header">
          <h2>Upload Customer Order</h2>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          Upload the part list your customer sent. Once imported, open{" "}
          <Link to="/vendor-comparison">Vendor Comparison</Link> to see which vendors can supply it.
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
          <p className="dropzone__title">Drag &amp; drop the customer order file here</p>
          <p className="dropzone__hint">
            Supported: {ACCEPTED_EXTENSIONS.join(", ")} — one or multiple files at once
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
                  <th>Rows</th>
                  <th>Errors</th>
                  <th>Status</th>
                  <th>Message</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {lastResults.map((result, index) => (
                  <tr key={`${result.order_id}-${index}`}>
                    <td>{result.file_name}</td>
                    <td>{result.row_count}</td>
                    <td>
                      {result.error_count > 0 ? (
                        <button
                          type="button"
                          className="link-button"
                          onClick={() =>
                            setErrorsModalOrder({ id: result.order_id, fileName: result.file_name })
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
                    <td>
                      {result.order_id ? (
                        <Link to="/vendor-comparison" state={{ orderId: result.order_id }} className="btn btn--ghost">
                          Compare
                        </Link>
                      ) : null}
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
          <h2>Order History</h2>
        </div>
        <div className="toolbar">
          <input
            className="field__input toolbar__search"
            type="search"
            placeholder="Search by file name…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>

        {isHistoryLoading ? (
          <div className="page-loading">Loading history…</div>
        ) : filteredHistory.length === 0 ? (
          <EmptyState title="No customer orders yet" description="Upload a customer order file to get started." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Rows</th>
                  <th>Errors</th>
                  <th>Status</th>
                  <th>Uploaded</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {filteredHistory.map((row) => (
                  <tr key={row.id}>
                    <td>{row.file_name}</td>
                    <td>{row.row_count}</td>
                    <td>
                      {row.error_count > 0 ? (
                        <button
                          type="button"
                          className="link-button"
                          onClick={() => setErrorsModalOrder({ id: row.id, fileName: row.file_name })}
                        >
                          {row.error_count} row(s)
                        </button>
                      ) : (
                        0
                      )}
                    </td>
                    <td>
                      <StatusPill status={row.status} />
                    </td>
                    <td>{new Date(row.created_at).toLocaleString()}</td>
                    <td className="data-table__actions">
                      <button
                        type="button"
                        className="btn btn--ghost"
                        onClick={() => setItemsModalOrder({ id: row.id, fileName: row.file_name })}
                      >
                        View Items
                      </button>
                      <Link to="/vendor-comparison" state={{ orderId: row.id }} className="btn btn--ghost">
                        Compare
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {errorsModalOrder && (
        <OrderErrorsModal
          orderId={errorsModalOrder.id}
          fileName={errorsModalOrder.fileName}
          onClose={() => setErrorsModalOrder(null)}
        />
      )}
      {itemsModalOrder && (
        <OrderItemsModal
          orderId={itemsModalOrder.id}
          fileName={itemsModalOrder.fileName}
          onClose={() => setItemsModalOrder(null)}
        />
      )}
    </Layout>
  );
}
