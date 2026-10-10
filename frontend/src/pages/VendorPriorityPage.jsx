import { useEffect, useMemo, useState } from "react";
import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import {
  addBrandVendor,
  getVendorPerformance,
  listVendorBrands,
  listVendorOptions,
  setBrandVendors,
} from "../api/advanceOrders";

// Brand-wise vendor priority (Founder, 10 Oct 2026). The purchase bot asks a
// brand's vendors in exactly this order for every advance order -- #1 first,
// the next only after a "no" or a silent 2 hours -- so whatever is saved here
// is what the very next order uses. Anyone logged in can change it.

function minutesText(value) {
  if (value === null || value === undefined) return "—";
  if (value < 60) return `${value} min`;
  const hours = value / 60;
  return hours < 24 ? `${hours.toFixed(1)} h` : `${(hours / 24).toFixed(1)} d`;
}

function discountText(row) {
  if (!row) return "—";
  if (row.discount_type === "percent" && row.discount_pct !== null) return `${row.discount_pct}%`;
  return row.discount_type === "rate" ? "rate (asked)" : "—";
}

function ScoreBadge({ score, littleData }) {
  if (score === undefined || score === null) return <span className="pill pill--neutral">—</span>;
  const tone = score >= 70 ? "success" : score >= 50 ? "info" : "warning";
  return (
    <span className={`pill pill--${tone}`} title={littleData ? "Little history yet - partly an average" : "Performance score"}>
      {score}
      {littleData ? "*" : ""}
    </span>
  );
}

export function VendorPriorityPage() {
  const toast = useToast();
  const [brands, setBrands] = useState(null);
  const [perf, setPerf] = useState({ vendors: {}, brands: {} });
  const [options, setOptions] = useState([]);
  const [brand, setBrand] = useState("");
  const [brandFilter, setBrandFilter] = useState("");
  const [adding, setAdding] = useState("");
  const [saving, setSaving] = useState(false);
  const [newVendor, setNewVendor] = useState({ name: "", phone: "", discount: "", position: "" });

  async function load() {
    try {
      const [b, p, o] = await Promise.all([listVendorBrands(), getVendorPerformance(), listVendorOptions()]);
      setBrands(b);
      setPerf(p);
      setOptions(o);
      setBrand((current) => current || Object.keys(b).filter((x) => x !== "*").sort()[0] || "");
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load the vendor priority."));
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const rows = useMemo(() => (brands && brand ? brands[brand] || [] : []), [brands, brand]);
  const scores = useMemo(
    () => Object.fromEntries((perf.brands[brand] || []).map((x) => [x.vendor_id, x.score])),
    [perf, brand]
  );
  const best = (perf.brands[brand] || [])[0];
  const bestVendor = best ? perf.vendors[best.vendor_id] : null;
  const preferred = rows[0];

  // "Add vendor" dropdown: this brand's best performers first, then everyone
  // else by overall reply record, then the rest A-Z.
  const addChoices = useMemo(() => {
    const onList = new Set(rows.map((r) => r.vendor_id));
    const overall = (id) => {
      const v = perf.vendors[id];
      if (!v || v.little_data) return -1;
      return (v.reply_rate || 0) * 2 - Math.min(v.median_reply_minutes || 0, 1440) / 30;
    };
    return options
      .filter((o) => !onList.has(o.vendor_id))
      .sort((a, b) => overall(b.vendor_id) - overall(a.vendor_id) || a.vendor_name.localeCompare(b.vendor_name));
  }, [options, rows, perf]);

  async function save(ids, message) {
    setSaving(true);
    try {
      setBrands(await setBrandVendors(brand, ids));
      setPerf(await getVendorPerformance());
      toast.success(message);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not save the priority."));
    } finally {
      setSaving(false);
    }
  }

  const ids = rows.map((r) => r.vendor_id);
  function move(index, delta) {
    const next = [...ids];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    save(next, `${brand}: order updated.`);
  }
  function setPosition(index, position) {
    const next = [...ids];
    const [id] = next.splice(index, 1);
    next.splice(position, 0, id);
    save(next, `${brand}: ${rows[index].vendor_name} is now #${position + 1}.`);
  }
  function remove(index) {
    if (!window.confirm(`Remove ${rows[index].vendor_name} from ${brand}? The bot will stop asking them for this brand.`)) return;
    save(ids.filter((_, i) => i !== index), `${brand}: ${rows[index].vendor_name} removed.`);
  }
  function add() {
    if (!adding) return;
    const vendor = options.find((o) => String(o.vendor_id) === adding);
    save([...ids, Number(adding)], `${brand}: ${vendor ? vendor.vendor_name : "vendor"} added at the bottom.`);
    setAdding("");
  }
  async function addNew(event) {
    event.preventDefault();
    const name = newVendor.name.trim();
    const digits = newVendor.phone.replace(/\D/g, "");
    if (!name) return toast.error("Vendor name is required.");
    if (digits.length < 10) return toast.error("Enter the vendor's 10-digit WhatsApp number.");
    setSaving(true);
    try {
      setBrands(
        await addBrandVendor(brand, {
          name,
          phone: newVendor.phone,
          discount_pct: newVendor.discount === "" ? null : Number(newVendor.discount),
          position: newVendor.position === "" ? null : Number(newVendor.position),
        })
      );
      const [p, o] = await Promise.all([getVendorPerformance(), listVendorOptions()]);
      setPerf(p);
      setOptions(o);
      setNewVendor({ name: "", phone: "", discount: "", position: "" });
      toast.success(`${name} added to ${brand}. The bot will ask them in their turn.`);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not add the vendor."));
    } finally {
      setSaving(false);
    }
  }

  function sortByPerformance() {
    const order = (perf.brands[brand] || []).map((x) => x.vendor_id).filter((id) => ids.includes(id));
    const rest = ids.filter((id) => !order.includes(id));
    if (!window.confirm(`Reorder ${brand} by performance score? The best performer will be asked first.`)) return;
    save([...order, ...rest], `${brand}: ordered by performance.`);
  }

  if (!brands) {
    return (
      <Layout title="Vendor Priority">
        <p>Loading…</p>
      </Layout>
    );
  }
  const brandNames = Object.keys(brands)
    .filter((b) => b.toLowerCase().includes(brandFilter.toLowerCase()))
    .sort((a, b) => (a === "*" ? 1 : b === "*" ? -1 : a.localeCompare(b)));

  return (
    <Layout title="Vendor Priority">
      <section className="panel">
        <div className="toolbar" style={{ gap: 8, flexWrap: "wrap" }}>
          <input
            className="field__input"
            style={{ maxWidth: 200 }}
            placeholder="Search brand…"
            value={brandFilter}
            onChange={(e) => setBrandFilter(e.target.value)}
          />
          <select className="field__input" style={{ maxWidth: 280 }} value={brand} onChange={(e) => setBrand(e.target.value)}>
            {brandNames.map((b) => (
              <option key={b} value={b}>
                {b === "*" ? "* (any brand without its own list)" : b} · {brands[b].length} vendor(s)
              </option>
            ))}
          </select>
          <button type="button" className="btn btn--ghost" onClick={load}>
            Refresh
          </button>
        </div>

        <div className="stat-grid" style={{ marginTop: 12 }}>
          <div className="stat-card">
            <p className="stat-card__label">⭐ Best by performance</p>
            <p className="stat-card__value" style={{ fontSize: "1.1rem" }}>{bestVendor ? bestVendor.vendor_name : "—"}</p>
            <p className="stat-card__hint">
              {bestVendor
                ? `score ${best.score} · replies ${bestVendor.reply_rate ?? "—"}% · median ${minutesText(bestVendor.median_reply_minutes)}`
                : "no vendors on this brand"}
            </p>
          </div>
          <div className="stat-card">
            <p className="stat-card__label">👑 Preferred (asked first)</p>
            <p className="stat-card__value" style={{ fontSize: "1.1rem" }}>{preferred ? preferred.vendor_name : "—"}</p>
            <p className="stat-card__hint">
              {preferred && best && preferred.vendor_id !== best.vendor_id
                ? "differs from the best performer"
                : preferred
                ? "also the best performer"
                : "set one below"}
            </p>
          </div>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "10px 0 0" }}>
          The bot asks #1 first; the next vendor only after a "no". A vendor silent for 2 hours (with 2 reminders) goes to
          Prateek sir. Score = reply rate 35 + reply speed 30 + "yes" rate 20 + discount 15, from the last 60 days of
          WhatsApp. * = little history yet.
        </p>
      </section>

      <section className="panel">
        {rows.length === 0 ? (
          <EmptyState title={`No vendors for ${brand || "this brand"} yet`} description="Add one below." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Priority</th>
                  <th>Vendor</th>
                  <th>Score</th>
                  <th>Reply rate</th>
                  <th>Median reply</th>
                  <th>Said yes</th>
                  <th>Avg delivery</th>
                  <th>Discount</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => {
                  const v = perf.vendors[row.vendor_id] || {};
                  const isBest = best && best.vendor_id === row.vendor_id;
                  return (
                    <tr key={row.vendor_id}>
                      <td>
                        <select
                          className="field__input"
                          style={{ width: 70 }}
                          value={index}
                          disabled={saving}
                          onChange={(e) => setPosition(index, Number(e.target.value))}
                          aria-label={`Priority of ${row.vendor_name}`}
                        >
                          {rows.map((_, i) => (
                            <option key={i} value={i}>
                              #{i + 1}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td>
                        {index === 0 && "👑 "}
                        {isBest && "⭐ "}
                        {row.vendor_name}
                        <div style={{ fontSize: "0.78rem", color: row.numbers?.length ? "var(--color-text-muted)" : "var(--color-danger)" }}>
                          {row.numbers?.length ? row.numbers.map((n) => "+" + n).join(", ") : "⚠ no WhatsApp number - bot cannot ask"}
                        </div>
                      </td>
                      <td>
                        <ScoreBadge score={scores[row.vendor_id]} littleData={v.little_data} />
                      </td>
                      <td>{v.reply_rate !== null && v.reply_rate !== undefined ? `${v.reply_rate}% (${v.replied}/${v.asked})` : "—"}</td>
                      <td>{minutesText(v.median_reply_minutes)}</td>
                      <td>{v.yes_rate !== null && v.yes_rate !== undefined ? `${v.yes_rate}% of ${v.answers}` : "—"}</td>
                      <td>{v.avg_tat_days !== null && v.avg_tat_days !== undefined ? `${v.avg_tat_days} d` : "—"}</td>
                      <td>{discountText(row)}</td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <button type="button" className="btn btn--ghost" disabled={saving || index === 0} onClick={() => setPosition(index, 0)} title="Make preferred (#1)">
                          Top
                        </button>
                        <button type="button" className="btn btn--ghost" disabled={saving || index === 0} onClick={() => move(index, -1)} aria-label="Up">
                          ↑
                        </button>
                        <button type="button" className="btn btn--ghost" disabled={saving || index === rows.length - 1} onClick={() => move(index, 1)} aria-label="Down">
                          ↓
                        </button>
                        <button type="button" className="btn btn--ghost" disabled={saving} onClick={() => remove(index)} aria-label="Remove">
                          ✕
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="toolbar" style={{ gap: 8, marginTop: 12, flexWrap: "wrap" }}>
          <select className="field__input" style={{ maxWidth: 360 }} value={adding} onChange={(e) => setAdding(e.target.value)}>
            <option value="">+ Add a vendor to {brand}…</option>
            {addChoices.map((o, i) => {
              const v = perf.vendors[o.vendor_id];
              const hint = v && !v.little_data ? ` · replies ${v.reply_rate}% · ${minutesText(v.median_reply_minutes)}` : "";
              return (
                <option key={o.vendor_id} value={o.vendor_id}>
                  {i === 0 && hint ? "⭐ " : ""}
                  {o.vendor_name}
                  {hint}
                </option>
              );
            })}
          </select>
          <button type="button" className="btn btn--primary" disabled={!adding || saving} onClick={add}>
            Add
          </button>
          <button type="button" className="btn btn--ghost" disabled={saving || rows.length < 2} onClick={sortByPerformance}>
            Order by performance
          </button>
        </div>
      </section>

      <section className="panel">
        <h3 style={{ fontSize: "0.95rem", margin: "0 0 8px" }}>➕ Add a new vendor to {brand === "*" ? "any brand (*)" : brand}</h3>
        <form className="toolbar" style={{ gap: 8, flexWrap: "wrap" }} onSubmit={addNew}>
          <input
            className="field__input"
            style={{ maxWidth: 240 }}
            placeholder="Vendor name"
            value={newVendor.name}
            onChange={(e) => setNewVendor({ ...newVendor, name: e.target.value })}
          />
          <input
            className="field__input"
            style={{ maxWidth: 180 }}
            placeholder="WhatsApp number"
            inputMode="tel"
            value={newVendor.phone}
            onChange={(e) => setNewVendor({ ...newVendor, phone: e.target.value })}
          />
          <input
            className="field__input"
            style={{ maxWidth: 130 }}
            placeholder="Discount % (opt.)"
            inputMode="decimal"
            value={newVendor.discount}
            onChange={(e) => setNewVendor({ ...newVendor, discount: e.target.value })}
          />
          <select
            className="field__input"
            style={{ maxWidth: 150 }}
            value={newVendor.position}
            onChange={(e) => setNewVendor({ ...newVendor, position: e.target.value })}
          >
            <option value="">Priority: last</option>
            {[...rows, null].map((_, i) => (
              <option key={i} value={i + 1}>
                Priority #{i + 1}
              </option>
            ))}
          </select>
          <button type="submit" className="btn btn--primary" disabled={saving || !brand}>
            Add vendor
          </button>
        </form>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "8px 0 0" }}>
          An existing vendor with the same name or number is reused, not duplicated. No discount = the bot asks them for
          a rate. They are asked from the next advance order on, in their priority turn; they do not get the daily
          stock request.
        </p>
      </section>
    </Layout>
  );
}
