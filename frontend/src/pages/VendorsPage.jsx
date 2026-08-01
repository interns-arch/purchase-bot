import { useEffect, useMemo, useState } from "react";
import { Layout } from "../components/Layout";
import { Modal } from "../components/Modal";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import {
  createVendor,
  disableVendor,
  getVendor,
  listVendors,
  updateVendor,
} from "../api/vendors";

const EMPTY_FORM = { name: "", contact_info: "", payment_terms: "" };

function VendorFormModal({ initialValues, title, onClose, onSubmit }) {
  const [values, setValues] = useState(initialValues || EMPTY_FORM);
  const [error, setError] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  function handleChange(field) {
    return (event) => setValues((current) => ({ ...current, [field]: event.target.value }));
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    if (!values.name.trim()) {
      setError("Vendor name is required.");
      return;
    }
    setIsSaving(true);
    try {
      await onSubmit(values);
    } catch (err) {
      setError(extractErrorMessage(err, "Could not save vendor."));
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <Modal title={title} onClose={onClose}>
      <form onSubmit={handleSubmit}>
        <label className="field">
          <span className="field__label">Vendor name *</span>
          <input
            className="field__input"
            value={values.name}
            onChange={handleChange("name")}
            autoFocus
            required
          />
        </label>
        <label className="field">
          <span className="field__label">Contact info</span>
          <input
            className="field__input"
            value={values.contact_info || ""}
            onChange={handleChange("contact_info")}
            placeholder="Phone, email, address…"
          />
        </label>
        <label className="field">
          <span className="field__label">Payment terms</span>
          <input
            className="field__input"
            value={values.payment_terms || ""}
            onChange={handleChange("payment_terms")}
            placeholder="e.g. Net 30"
          />
        </label>

        {error && <p className="form-error" role="alert">{error}</p>}

        <div className="modal__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn--primary" disabled={isSaving}>
            {isSaving ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function VendorDetailModal({ vendorId, onClose }) {
  const [detail, setDetail] = useState(null);
  const toast = useToast();

  useEffect(() => {
    let isMounted = true;
    getVendor(vendorId)
      .then((data) => isMounted && setDetail(data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load vendor details.")));
    return () => {
      isMounted = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vendorId]);

  return (
    <Modal title="Vendor Details" onClose={onClose}>
      {!detail ? (
        <p>Loading…</p>
      ) : (
        <dl className="detail-list">
          <div>
            <dt>Name</dt>
            <dd>{detail.name}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>{detail.active ? "Active" : "Disabled"}</dd>
          </div>
          <div>
            <dt>Contact info</dt>
            <dd>{detail.contact_info || "—"}</dd>
          </div>
          <div>
            <dt>Payment terms</dt>
            <dd>{detail.payment_terms || "—"}</dd>
          </div>
          <div>
            <dt>Parts in active inventory</dt>
            <dd>{detail.total_parts}</dd>
          </div>
          <div>
            <dt>Total quantity available</dt>
            <dd>{detail.total_quantity_available}</dd>
          </div>
          <div>
            <dt>Last import</dt>
            <dd>
              {detail.last_import_at
                ? `${new Date(detail.last_import_at).toLocaleString()} (${detail.last_import_status})`
                : "No imports yet"}
            </dd>
          </div>
          <div>
            <dt>Added on</dt>
            <dd>{new Date(detail.created_at).toLocaleDateString()}</dd>
          </div>
        </dl>
      )}
    </Modal>
  );
}

export function VendorsPage() {
  const toast = useToast();
  const [vendors, setVendors] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [activeOnly, setActiveOnly] = useState(false);
  const [formModal, setFormModal] = useState(null); // { mode: 'add' | 'edit', vendor }
  const [detailVendorId, setDetailVendorId] = useState(null);

  async function refresh() {
    setIsLoading(true);
    try {
      const data = await listVendors({ activeOnly });
      setVendors(data);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load vendors."));
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeOnly]);

  const filteredVendors = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return vendors;
    return vendors.filter(
      (vendor) =>
        vendor.name.toLowerCase().includes(query) ||
        (vendor.contact_info || "").toLowerCase().includes(query)
    );
  }, [vendors, search]);

  async function handleCreate(values) {
    await createVendor(values);
    toast.success(`Vendor "${values.name}" added.`);
    setFormModal(null);
    refresh();
  }

  async function handleEdit(vendorId, values) {
    await updateVendor(vendorId, values);
    toast.success("Vendor updated.");
    setFormModal(null);
    refresh();
  }

  async function handleDisable(vendor) {
    if (!window.confirm(`Disable vendor "${vendor.name}"? It can be re-enabled later via Edit.`)) {
      return;
    }
    try {
      await disableVendor(vendor.id);
      toast.success(`Vendor "${vendor.name}" disabled.`);
      refresh();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not disable vendor."));
    }
  }

  return (
    <Layout title="Vendor Management">
      <div className="toolbar">
        <input
          className="field__input toolbar__search"
          type="search"
          placeholder="Search vendors by name or contact…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <label className="toolbar__toggle">
          <input
            type="checkbox"
            checked={activeOnly}
            onChange={(event) => setActiveOnly(event.target.checked)}
          />
          Active only
        </label>
        <button type="button" className="btn btn--primary btn--large" onClick={() => setFormModal({ mode: "add" })}>
          + Add Vendor
        </button>
      </div>

      {isLoading ? (
        <div className="page-loading">Loading vendors…</div>
      ) : filteredVendors.length === 0 ? (
        <EmptyState
          title="No vendors found"
          description="Add your first vendor to start importing inventory."
          action={
            <button type="button" className="btn btn--primary" onClick={() => setFormModal({ mode: "add" })}>
              + Add Vendor
            </button>
          }
        />
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Contact Info</th>
                <th>Payment Terms</th>
                <th>Status</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {filteredVendors.map((vendor) => (
                <tr key={vendor.id}>
                  <td>{vendor.name}</td>
                  <td>{vendor.contact_info || "—"}</td>
                  <td>{vendor.payment_terms || "—"}</td>
                  <td>
                    <span className={`pill pill--${vendor.active ? "success" : "neutral"}`}>
                      {vendor.active ? "Active" : "Disabled"}
                    </span>
                  </td>
                  <td className="data-table__actions">
                    <button type="button" className="btn btn--ghost" onClick={() => setDetailVendorId(vendor.id)}>
                      View
                    </button>
                    <button
                      type="button"
                      className="btn btn--ghost"
                      onClick={() => setFormModal({ mode: "edit", vendor })}
                    >
                      Edit
                    </button>
                    {vendor.active && (
                      <button type="button" className="btn btn--ghost btn--danger" onClick={() => handleDisable(vendor)}>
                        Disable
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {formModal?.mode === "add" && (
        <VendorFormModal title="Add Vendor" onClose={() => setFormModal(null)} onSubmit={handleCreate} />
      )}
      {formModal?.mode === "edit" && (
        <VendorFormModal
          title={`Edit Vendor — ${formModal.vendor.name}`}
          initialValues={{
            name: formModal.vendor.name,
            contact_info: formModal.vendor.contact_info || "",
            payment_terms: formModal.vendor.payment_terms || "",
          }}
          onClose={() => setFormModal(null)}
          onSubmit={(values) => handleEdit(formModal.vendor.id, values)}
        />
      )}
      {detailVendorId && (
        <VendorDetailModal vendorId={detailVendorId} onClose={() => setDetailVendorId(null)} />
      )}
    </Layout>
  );
}
