import { formatDateTime } from "../utils/datetime";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Layout } from "../components/Layout";
import { Modal } from "../components/Modal";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { DashboardFilterBar } from "../components/DashboardFilterBar";
import { CHART_GRID, CHART_SERIES_PRIMARY, CHART_SERIES_SECONDARY, STATUS_COLORS } from "../components/chartColors";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import {
  listDeliveryImportErrors,
  listDeliveryImportHistory,
  uploadDeliveryFiles,
} from "../api/deliveries";
import { getDeliveryTracking } from "../api/deliveryTracking";

const ACCEPTED_EXTENSIONS = [".csv", ".xlsx", ".xlsm", ".xls"];
const STATUS_OPTIONS = ["COMPLETE", "PARTIAL", "NOT_DELIVERED"];

const COLUMNS = [
  { key: "part_number", label: "Customer Part" },
  { key: "vendor_name", label: "Vendor" },
  { key: "ordered_qty", label: "Ordered Qty", numeric: true },
  { key: "delivered_qty", label: "Delivered Qty", numeric: true },
  { key: "short_qty", label: "Short Qty", numeric: true },
  { key: "status", label: "Status" },
];

function StatCard({ label, value }) {
  return (
    <div className="stat-card">
      <p className="stat-card__label">{label}</p>
      <p className="stat-card__value">{value}</p>
    </div>
  );
}

function ImportErrorsModal({ importId, fileName, onClose }) {
  const [errors, setErrors] = useState(null);
  const toast = useToast();

  useEffect(() => {
    let isMounted = true;
    listDeliveryImportErrors(importId)
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

export function DeliveryTrackingPage() {
  const toast = useToast();
  const fileInputRef = useRef(null);

  const [filters, setFilters] = useState({});
  const [data, setData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [vendorOptions, setVendorOptions] = useState([]);

  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState("part_number");
  const [sortDir, setSortDir] = useState("asc");

  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [lastResults, setLastResults] = useState([]);
  const [history, setHistory] = useState([]);
  const [isHistoryLoading, setIsHistoryLoading] = useState(true);
  const [errorsModalImport, setErrorsModalImport] = useState(null);

  // Vendor filter options come from an unfiltered baseline fetch, once --
  // deriving them from the (possibly vendor-filtered) display data would
  // collapse the dropdown to just the selected vendor.
  useEffect(() => {
    getDeliveryTracking({})
      .then((base) => {
        const seen = new Map();
        base.rows.forEach((row) => seen.set(row.vendor_id, row.vendor_name));
        setVendorOptions([...seen.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name)));
      })
      .catch(() => {});
  }, []);

  const loadData = useCallback((currentFilters) => {
    setIsLoading(true);
    getDeliveryTracking(currentFilters)
      .then(setData)
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load delivery tracking data.")))
      .finally(() => setIsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadData(filters);
  }, [filters, loadData]);

  const loadHistory = useCallback(async () => {
    setIsHistoryLoading(true);
    try {
      setHistory(await listDeliveryImportHistory());
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load delivery import history."));
    } finally {
      setIsHistoryLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;

    setIsUploading(true);
    setUploadProgress(0);
    setLastResults([]);
    try {
      const results = await uploadDeliveryFiles(files, { onProgress: setUploadProgress });
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
      loadData(filters);
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

  function toggleSort(key) {
    if (sortKey === key) {
      setSortDir((current) => (current === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  const filteredRows = useMemo(() => {
    if (!data) return [];
    const query = search.trim().toLowerCase();
    let rows = data.rows;
    if (query) {
      rows = rows.filter(
        (row) =>
          row.part_number.toLowerCase().includes(query) || row.vendor_name.toLowerCase().includes(query)
      );
    }
    const column = COLUMNS.find((c) => c.key === sortKey);
    const sorted = [...rows].sort((a, b) => {
      const aVal = a[sortKey];
      const bVal = b[sortKey];
      if (column?.numeric) return aVal - bVal;
      return String(aVal).localeCompare(String(bVal));
    });
    if (sortDir === "desc") sorted.reverse();
    return sorted;
  }, [data, search, sortKey, sortDir]);

  const statusPieData = useMemo(() => {
    if (!data) return [];
    return [
      { name: "Complete", key: "COMPLETE", value: data.summary.complete_count },
      { name: "Partial", key: "PARTIAL", value: data.summary.partial_count },
      { name: "Not Delivered", key: "NOT_DELIVERED", value: data.summary.not_delivered_count },
    ].filter((entry) => entry.value > 0);
  }, [data]);

  const orderedVsDeliveredData = useMemo(() => {
    if (!data) return [];
    return [...data.rows]
      .sort((a, b) => b.ordered_qty - a.ordered_qty)
      .slice(0, 15)
      .map((row) => ({ part_number: row.part_number, Ordered: row.ordered_qty, Delivered: row.delivered_qty }));
  }, [data]);

  return (
    <Layout title="Delivery Tracking">
      <section className="panel">
        <div className="panel__header">
          <h2>Upload Vendor Delivery File</h2>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          Each row must reference the Vendor, Part Number, and Delivered Quantity -- reconciled
          against the vendor allocations made on Vendor Comparison. An optional date column powers
          the charts below.
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
          <div className="table-scroll" style={{ marginTop: 16 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>File</th>
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
        )}
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>Filters</h2>
        </div>
        <DashboardFilterBar
          filters={filters}
          onChange={setFilters}
          vendorOptions={vendorOptions}
          statusOptions={STATUS_OPTIONS}
        />
      </section>

      {isLoading ? (
        <div className="page-loading">Loading delivery tracking…</div>
      ) : !data || data.rows.length === 0 ? (
        <EmptyState
          title="No allocated deliveries yet"
          description="Select vendors on the Vendor Comparison page and upload a delivery file to see tracking data here."
        />
      ) : (
        <>
          <div className="stat-grid">
            <StatCard label="Total Parts Ordered" value={data.summary.total_ordered_qty} />
            <StatCard label="Total Parts Delivered" value={data.summary.total_delivered_qty} />
            <StatCard label="Total Short Quantity" value={data.summary.total_short_qty} />
            <StatCard label="Completed Deliveries" value={data.summary.complete_count} />
            <StatCard label="Partial Deliveries" value={data.summary.partial_count} />
            <StatCard label="Pending Deliveries" value={data.summary.not_delivered_count} />
          </div>

          <div className="stat-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))" }}>
            <section className="panel">
              <div className="panel__header">
                <h2>Delivery Status</h2>
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie
                    data={statusPieData}
                    dataKey="value"
                    nameKey="name"
                    label={(entry) => `${entry.name}: ${entry.value}`}
                  >
                    {statusPieData.map((entry) => (
                      <Cell key={entry.key} fill={STATUS_COLORS[entry.key]} />
                    ))}
                  </Pie>
                  <Legend />
                  <Tooltip />
                </PieChart>
              </ResponsiveContainer>
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Vendor-wise Deliveries</h2>
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={data.vendorwise_deliveries}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
                  <XAxis dataKey="vendor_name" tick={{ fontSize: 12 }} />
                  <YAxis tick={{ fontSize: 12 }} />
                  <Tooltip />
                  <Bar dataKey="delivered_qty" name="Delivered Qty" fill={CHART_SERIES_PRIMARY} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Daily Deliveries</h2>
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={data.daily_deliveries}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
                  <XAxis dataKey="delivery_date" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 12 }} />
                  <Tooltip />
                  <Bar dataKey="delivered_qty" name="Delivered Qty" fill={CHART_SERIES_PRIMARY} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Ordered vs Delivered</h2>
              </div>
              <p style={{ color: "var(--color-text-muted)", fontSize: "0.78rem", marginTop: -8, marginBottom: 8 }}>
                Top 15 parts by ordered quantity.
              </p>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={orderedVsDeliveredData}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
                  <XAxis dataKey="part_number" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 12 }} />
                  <Tooltip />
                  <Legend />
                  <Bar dataKey="Ordered" fill={CHART_SERIES_PRIMARY} radius={[4, 4, 0, 0]} />
                  <Bar dataKey="Delivered" fill={CHART_SERIES_SECONDARY} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </section>
          </div>

          <section className="panel">
            <div className="panel__header">
              <h2>Delivery Tracking</h2>
            </div>
            <div className="toolbar">
              <input
                className="field__input toolbar__search"
                type="search"
                placeholder="Search part number or vendor…"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>

            {filteredRows.length === 0 ? (
              <EmptyState title="No matching rows" description="Try a different search or filter." />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      {COLUMNS.map((column) => (
                        <th key={column.key}>
                          <button
                            type="button"
                            className="link-button"
                            style={{ color: "inherit", textDecoration: "none", fontWeight: 600 }}
                            onClick={() => toggleSort(column.key)}
                          >
                            {column.label}
                            {sortKey === column.key ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                          </button>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {filteredRows.map((row) => (
                      <tr key={`${row.vendor_id}-${row.part_id}`}>
                        <td>{row.part_number}</td>
                        <td>{row.vendor_name}</td>
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
        </>
      )}

      <section className="panel">
        <div className="panel__header">
          <h2>Delivery Import History</h2>
        </div>
        {isHistoryLoading ? (
          <div className="page-loading">Loading history…</div>
        ) : history.length === 0 ? (
          <EmptyState title="No delivery imports yet" description="Uploaded files will show up here." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Rows</th>
                  <th>Errors</th>
                  <th>Status</th>
                  <th>Imported</th>
                </tr>
              </thead>
              <tbody>
                {history.map((row) => (
                  <tr key={row.id}>
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
