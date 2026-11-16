import { useEffect, useMemo, useState } from "react";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { addVendorPoc, getVendorPocs, listPocVendors, removeVendorPoc } from "../api/vendorPocs";

// POC tab (User, 14 Nov 2026): a vendor's people. Every number listed as
// "POC" or "Registered" IS the vendor -- one to one and in his WhatsApp group:
// his stock files, typed stock and replies count as his.

const KIND_LABEL = {
  registered: "Registered",
  poc: "POC",
  enquiry: "Enquiry only",
};

function showNumber(number) {
  const n = String(number || "");
  return n.startsWith("91") && n.length === 12 ? `+91 ${n.slice(2, 7)} ${n.slice(7)}` : n;
}

export function VendorPocPanel() {
  const toast = useToast();
  const [vendors, setVendors] = useState(null);
  const [search, setSearch] = useState("");
  const [vendorId, setVendorId] = useState("");
  const [detail, setDetail] = useState(null);
  const [form, setForm] = useState({ name: "", number: "" });
  const [busy, setBusy] = useState(false);

  async function loadVendors() {
    try {
      setVendors(await listPocVendors());
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load vendors."));
      setVendors([]);
    }
  }

  async function loadDetail(id) {
    if (!id) {
      setDetail(null);
      return;
    }
    try {
      setDetail(await getVendorPocs(id));
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load this vendor's numbers."));
    }
  }

  useEffect(() => {
    loadVendors();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadDetail(vendorId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vendorId]);

  const shown = useMemo(
    () =>
      (vendors || []).filter((v) =>
        `${v.vendor_name} ${v.vendor_code || ""}`.toLowerCase().includes(search.toLowerCase())
      ),
    [vendors, search]
  );

  async function onAdd(event) {
    event.preventDefault();
    if (!vendorId || !form.number.trim()) return;
    setBusy(true);
    try {
      setDetail(await addVendorPoc(vendorId, form.number.trim(), form.name.trim() || null));
      setForm({ name: "", number: "" });
      toast.success("Number added. It now counts as this vendor.");
      loadVendors();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not add this number."));
    } finally {
      setBusy(false);
    }
  }

  async function onRemove(row) {
    if (!window.confirm(`Remove ${showNumber(row.number)} from ${detail.vendor_name}? Messages from it will no longer count as this vendor.`)) return;
    setBusy(true);
    try {
      setDetail(await removeVendorPoc(vendorId, row.id));
      toast.success("Number removed.");
      loadVendors();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not remove this number."));
    } finally {
      setBusy(false);
    }
  }

  async function onMakePoc(row) {
    setBusy(true);
    try {
      setDetail(await addVendorPoc(vendorId, row.number, row.name || null));
      toast.success("Now a POC: it counts as this vendor.");
      loadVendors();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not change this number."));
    } finally {
      setBusy(false);
    }
  }

  if (vendors === null) return <p>Loading…</p>;

  return (
    <>
      <section className="panel">
        <div className="toolbar" style={{ gap: 8, flexWrap: "wrap" }}>
          <input
            className="field__input"
            style={{ maxWidth: 220 }}
            placeholder="Search vendor…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select className="field__input" style={{ maxWidth: 340 }} value={vendorId} onChange={(e) => setVendorId(e.target.value)}>
            <option value="">Choose a vendor…</option>
            {shown.map((v) => (
              <option key={v.vendor_id} value={v.vendor_id}>
                {v.vendor_name}
                {v.vendor_code ? ` (${v.vendor_code})` : ""} · {v.pocs + v.registered} number(s)
              </option>
            ))}
          </select>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "10px 0 0" }}>
          A POC number counts as the vendor, one to one and in his WhatsApp group: his stock files, typed stock and
          replies are taken as his. POC numbers do not get the 09:30 stock request (registered numbers do). Our own
          team's numbers cannot be added.
        </p>
      </section>

      {detail && (
        <section className="panel">
          <h2 style={{ marginTop: 0 }}>{detail.vendor_name}</h2>
          {detail.numbers.length === 0 ? (
            <EmptyState title="No numbers yet" description="Add the vendor's people below." />
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>WhatsApp number</th>
                    <th>Type</th>
                    <th>Counts as vendor</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {detail.numbers.map((row) => (
                    <tr key={`${row.kind}-${row.number}`}>
                      <td>{row.name || "—"}</td>
                      <td>{showNumber(row.number)}</td>
                      <td>{KIND_LABEL[row.kind] || row.kind}</td>
                      <td>{row.treat_as_vendor ? "Yes — one to one and in group" : "In group only, not one to one"}</td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        {row.kind === "enquiry" && (
                          <button type="button" className="btn btn--secondary" disabled={busy} onClick={() => onMakePoc(row)}>
                            Make POC
                          </button>
                        )}{" "}
                        {row.removable && (
                          <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => onRemove(row)}>
                            Remove
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <form onSubmit={onAdd} className="toolbar" style={{ gap: 8, flexWrap: "wrap", marginTop: 12 }}>
            <input
              className="field__input"
              style={{ maxWidth: 200 }}
              placeholder="Name (optional)"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
            <input
              className="field__input"
              style={{ maxWidth: 200 }}
              placeholder="WhatsApp number"
              inputMode="tel"
              value={form.number}
              onChange={(e) => setForm({ ...form, number: e.target.value })}
            />
            <button type="submit" className="btn btn--primary" disabled={busy || !form.number.trim()}>
              Add POC
            </button>
          </form>
        </section>
      )}
    </>
  );
}
