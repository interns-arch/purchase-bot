import { useEffect, useMemo, useState } from "react";
import { Layout } from "../components/Layout";
import { Modal } from "../components/Modal";
import { StatusPill } from "../components/StatusPill";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { formatDateTime } from "../utils/datetime";
import {
  getQuoteSettings,
  listAdvanceOrders,
  listVendorBrands,
  recordLineAnswer,
  setBrandVendors,
} from "../api/advanceOrders";

const REFRESH_MS = 20000;

function upper(value) {
  return String(value || "").toUpperCase();
}

function money(value, prefix = "") {
  return value === null || value === undefined ? "—" : `${prefix}${value}`;
}

function discountText(quote) {
  if (quote.discount_pct !== null && quote.discount_pct !== undefined) return `${quote.discount_pct}%`;
  if (quote.terms?.discount_note) return quote.terms.discount_note;
  return "—";
}

// ---------------------------------------------------------------- manual entry
function AnswerModal({ order, line, onClose, onSaved }) {
  const toast = useToast();
  const [vendorId, setVendorId] = useState(line.quotes[0]?.vendor_id ?? "");
  const [available, setAvailable] = useState(true);
  const [tatDays, setTatDays] = useState("");
  const [rate, setRate] = useState("");
  const [mrp, setMrp] = useState("");
  const [qty, setQty] = useState("");
  const [letRankingDecide, setLetRankingDecide] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  const askedVendors = useMemo(() => {
    const seen = new Map();
    for (const quote of line.quotes) seen.set(quote.vendor_id, quote.vendor_name);
    return [...seen.entries()];
  }, [line.quotes]);

  async function handleSave(event) {
    event.preventDefault();
    if (!vendorId) {
      toast.error("Pick the vendor this answer came from.");
      return;
    }
    setIsSaving(true);
    try {
      await recordLineAnswer(order.id, line.id, {
        available,
        vendor_id: Number(vendorId),
        tat_days: tatDays === "" ? null : Number(tatDays),
        quoted_rate: rate === "" ? null : rate,
        mrp: mrp === "" ? null : mrp,
        available_qty: qty === "" ? null : Number(qty),
        force: !letRankingDecide,
      });
      toast.success(`Answer recorded for ${line.part_number}.`);
      onSaved();
      onClose();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not record the answer."));
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <Modal title={`Record an answer — ${line.part_number}`} onClose={onClose} width={520}>
      <form onSubmit={handleSave} style={{ display: "grid", gap: 12 }}>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", margin: 0 }}>
          Use this when a vendor answered on the phone, or when the bot could not read a
          WhatsApp reply and sent it to you.
        </p>
        <label className="field">
          <span className="field__label">Vendor</span>
          <input
            id="answer-vendor"
            className="field__input"
            list="answer-vendor-options"
            value={vendorId}
            onChange={(e) => setVendorId(e.target.value)}
            placeholder="Vendor id"
          />
          <datalist id="answer-vendor-options">
            {askedVendors.map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </datalist>
        </label>
        <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
          <input
            id="answer-available"
            type="checkbox"
            checked={available}
            onChange={(e) => setAvailable(e.target.checked)}
          />
          <span>The vendor has it</span>
        </label>
        {available && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 10 }}>
            <label className="field">
              <span className="field__label">Days</span>
              <input id="answer-tat" className="field__input" type="number" min="0" value={tatDays} onChange={(e) => setTatDays(e.target.value)} />
            </label>
            <label className="field">
              <span className="field__label">Rate (₹)</span>
              <input id="answer-rate" className="field__input" type="number" min="0" step="0.01" value={rate} onChange={(e) => setRate(e.target.value)} />
            </label>
            <label className="field">
              <span className="field__label">MRP (₹)</span>
              <input id="answer-mrp" className="field__input" type="number" min="0" step="0.01" value={mrp} onChange={(e) => setMrp(e.target.value)} />
            </label>
            <label className="field">
              <span className="field__label">Qty</span>
              <input id="answer-qty" className="field__input" type="number" min="1" value={qty} onChange={(e) => setQty(e.target.value)} placeholder={String(line.qty)} />
            </label>
          </div>
        )}
        <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
          <input
            id="answer-rank"
            type="checkbox"
            checked={letRankingDecide}
            onChange={(e) => setLetRankingDecide(e.target.checked)}
          />
          <span>Add as one more quote and let the ranking choose</span>
        </label>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: 0 }}>
          Untick to make this vendor the answer for this part outright.
        </p>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn--primary" disabled={isSaving}>
            {isSaving ? "Saving…" : "Record answer"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------- one order
function QuoteTable({ line }) {
  if (line.quotes.length === 0) {
    return <p style={{ color: "var(--color-text-muted)", margin: "6px 0 0" }}>No vendor has answered yet.</p>;
  }
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>Vendor</th>
            <th>Has it</th>
            <th>TAT</th>
            <th>Discount</th>
            <th>Rate</th>
            <th>Net / unit</th>
            <th>Payment</th>
            <th>Pickup</th>
            <th>What they wrote</th>
          </tr>
        </thead>
        <tbody>
          {line.quotes.map((quote) => {
            const isWinner = line.best_quote && line.best_quote.id === quote.id;
            return (
              <tr key={quote.id} style={isWinner ? { fontWeight: 600 } : undefined}>
                <td>
                  {isWinner ? "★ " : ""}
                  {quote.vendor_name || quote.vendor_id}
                  {quote.source === "admin" ? " (desk)" : ""}
                </td>
                <td>{quote.available ? "Yes" : "No"}</td>
                <td>{quote.available ? (quote.tat_days ?? "—") + (quote.tat_days !== null ? " d" : "") : "—"}</td>
                <td>{discountText(quote)}</td>
                <td>{money(quote.quoted_rate, "₹")}</td>
                <td>{money(quote.net_price, "₹")}</td>
                <td>{quote.terms?.payment_terms || "—"}</td>
                <td>{quote.terms?.transport || "—"}</td>
                <td style={{ maxWidth: 260, whiteSpace: "normal", color: "var(--color-text-muted)" }}>
                  {quote.raw_reply || "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function OrderCard({ order, onAnswer }) {
  const [openLine, setOpenLine] = useState(null);
  return (
    <section className="panel">
      <div className="panel__header" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ margin: 0 }}>
          #{order.id} {order.external_ref ? `· ${order.external_ref}` : ""}
          {order.customer?.name ? ` · ${order.customer.name}` : ""}
        </h2>
        <span style={{ display: "flex", gap: 6 }}>
          <span className={"pill pill--" + (order.kind === "dealer_stock" ? "info" : "neutral")}>
            {order.kind === "dealer_stock" ? "Dealer stock order" : "Advance order"}
          </span>
          <StatusPill status={upper(order.status)} />
        </span>
      </div>
      <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "0 0 10px" }}>
        {order.created_at ? `Received ${formatDateTime(order.created_at)}` : ""}
        {order.needed_by ? ` · needed by ${order.needed_by}` : ""}
      </p>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Part</th>
              <th>Brand</th>
              <th>Qty</th>
              <th>Status</th>
              <th>Chosen vendor</th>
              <th>TAT</th>
              <th>Discount</th>
              <th>Net / unit</th>
              <th>Quotes</th>
              <th>Why</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {order.lines.map((line) => (
              <tr key={line.id}>
                <td>{line.part_number}</td>
                <td>{line.brand}</td>
                <td>{line.qty}</td>
                <td>
                  <StatusPill status={upper(line.status)} />
                </td>
                <td>{line.vendor_name || "—"}</td>
                <td>{line.tat_band || "—"}</td>
                <td>{money(line.discount_pct) === "—" ? "—" : `${line.discount_pct}%`}</td>
                <td>{money(line.net_price, "₹")}</td>
                <td>
                  <button type="button" className="btn btn--ghost" onClick={() => setOpenLine(openLine === line.id ? null : line.id)}>
                    {line.quote_count} {openLine === line.id ? "▲" : "▼"}
                  </button>
                </td>
                <td style={{ maxWidth: 220, whiteSpace: "normal", color: "var(--color-text-muted)" }}>{line.note || "—"}</td>
                <td>
                  {order.status === "asking" && (
                    <button type="button" className="btn btn--secondary" onClick={() => onAnswer(order, line)}>
                      Record answer
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {order.lines
        .filter((line) => line.id === openLine)
        .map((line) => (
          <div key={line.id} style={{ marginTop: 12 }}>
            <h3 style={{ fontSize: "0.95rem", margin: "0 0 6px" }}>Every answer for {line.part_number}, best first</h3>
            <QuoteTable line={line} />
          </div>
        ))}
    </section>
  );
}

// ---------------------------------------------------------------- vendor lists
function BrandListsPanel() {
  const toast = useToast();
  const [brands, setBrands] = useState(null);
  const [filter, setFilter] = useState("");

  async function load() {
    try {
      setBrands(await listVendorBrands());
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load the brand vendor lists."));
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function move(brand, index, delta) {
    const rows = brands[brand];
    const next = [...rows];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    try {
      setBrands(await setBrandVendors(brand, next.map((row) => row.vendor_id)));
      toast.success(`${brand}: ask order updated.`);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not reorder."));
    }
  }

  if (!brands) return <p>Loading…</p>;
  const names = Object.keys(brands)
    .filter((brand) => brand.toLowerCase().includes(filter.toLowerCase()))
    .sort();
  if (Object.keys(brands).length === 0) {
    return (
      <EmptyState
        title="No brand vendor lists yet"
        description="Run the VENDOR BRAND MAPPING import (backend/scripts/import_vendor_brand_mapping.py). Until then no vendor is asked about anything."
      />
    );
  }
  return (
    <>
      <div className="toolbar">
        <input
          id="brand-filter"
          className="field__input toolbar__search"
          placeholder="Filter brands…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      {names.map((brand) => (
        <div key={brand} style={{ marginBottom: 16 }}>
          <h3 style={{ fontSize: "0.95rem", margin: "8px 0" }}>
            {brand} <span style={{ color: "var(--color-text-muted)", fontWeight: 400 }}>· {brands[brand].length} vendor(s)</span>
          </h3>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Asked</th>
                  <th>Vendor</th>
                  <th>Discount</th>
                  <th>Payment</th>
                  <th>Pickup</th>
                  <th>Shares stock</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {brands[brand].map((row, index) => (
                  <tr key={row.vendor_id}>
                    <td>{row.priority}</td>
                    <td>{row.vendor_name}</td>
                    <td>
                      {row.discount_type === "percent" && row.discount_pct !== null
                        ? `${row.discount_pct}%`
                        : row.discount_note || (row.discount_type === "rate" ? "rate (asked)" : "—")}
                    </td>
                    <td>{row.payment_terms || "—"}</td>
                    <td>{row.transport || "—"}</td>
                    <td>{row.can_share_stock === null ? "—" : row.can_share_stock ? "Yes" : "No"}</td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <button type="button" className="btn btn--ghost" disabled={index === 0} onClick={() => move(brand, index, -1)} aria-label={`Ask ${row.vendor_name} earlier`}>
                        ↑
                      </button>
                      <button type="button" className="btn btn--ghost" disabled={index === brands[brand].length - 1} onClick={() => move(brand, index, 1)} aria-label={`Ask ${row.vendor_name} later`}>
                        ↓
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </>
  );
}

// ---------------------------------------------------------------- page
export function AdvanceOrdersPage() {
  const toast = useToast();
  const [orders, setOrders] = useState(null);
  const [settings, setSettings] = useState(null);
  const [disabled, setDisabled] = useState(false);
  const [tab, setTab] = useState("orders");
  const [statusFilter, setStatusFilter] = useState("all");
  const [answering, setAnswering] = useState(null);

  async function load() {
    try {
      const [orderData, settingsData] = await Promise.all([listAdvanceOrders(), getQuoteSettings()]);
      setOrders(orderData);
      setSettings(settingsData);
      setDisabled(false);
    } catch (error) {
      if (error?.response?.status === 503) {
        setDisabled(true);
        setOrders([]);
        getQuoteSettings().then(setSettings).catch(() => {});
      } else {
        toast.error(extractErrorMessage(error, "Could not load advance orders."));
      }
    }
  }

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const shown = (orders || []).filter((o) => statusFilter === "all" || o.status === statusFilter);
  const counts = (orders || []).reduce((acc, o) => ({ ...acc, [o.status]: (acc[o.status] || 0) + 1 }), {});

  return (
    <Layout title="Advance Orders">
      {settings && (
        <section className="panel">
          <div className="stat-grid">
            <div className="stat-card">
              <p className="stat-card__label">Status</p>
              <p className="stat-card__value">{settings.enabled ? "On" : "Off"}</p>
              <p className="stat-card__hint">ADVANCE_ORDERS_ENABLED</p>
            </div>
            <div className="stat-card">
              <p className="stat-card__label">Vendors asked at once</p>
              <p className="stat-card__value">{settings.quote_fanout}</p>
              <p className="stat-card__hint">per brand, per order</p>
            </div>
            <div className="stat-card">
              <p className="stat-card__label">Quote window</p>
              <p className="stat-card__value">{settings.quote_window_minutes} min</p>
              <p className="stat-card__hint">then the best answer so far wins</p>
            </div>
            <div className="stat-card">
              <p className="stat-card__label">Sales bot callback</p>
              <p className="stat-card__value">
                {settings.callback_configured ? (settings.callback_shadow ? "Shadow" : "On") : "Polling"}
              </p>
              <p className="stat-card__hint">ADVANCE_ORDER_CALLBACK_URL</p>
            </div>
          </div>
          <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "10px 0 0" }}>
            How a winner is chosen: faster TAT band first ({settings.tat_band_labels.join(" · ")}), then the bigger
            discount inside a band. Vendors are messaged {settings.vendor_hours} IST.
          </p>
        </section>
      )}

      <div className="toolbar" style={{ gap: 8 }}>
        <button type="button" className={"btn " + (tab === "orders" ? "btn--primary" : "btn--ghost")} onClick={() => setTab("orders")}>
          Orders
        </button>
        <button type="button" className={"btn " + (tab === "brands" ? "btn--primary" : "btn--ghost")} onClick={() => setTab("brands")}>
          Vendor lists by brand
        </button>
        {tab === "orders" && (
          <select id="status-filter" className="field__input" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={{ maxWidth: 220 }}>
            <option value="all">All ({(orders || []).length})</option>
            {["asking", "quoted", "no_vendor", "confirmed", "cancelled"].map((s) => (
              <option key={s} value={s}>
                {s.replace("_", " ")} ({counts[s] || 0})
              </option>
            ))}
          </select>
        )}
      </div>

      {tab === "brands" ? (
        <section className="panel">
          <BrandListsPanel />
        </section>
      ) : disabled ? (
        <section className="panel">
          <EmptyState
            title="Advance orders are switched off"
            description="Set ADVANCE_ORDERS_ENABLED=true (and ADVANCE_ORDER_API_KEY) in the backend environment. The vendor lists can be prepared before switching on."
          />
        </section>
      ) : orders === null ? (
        <p>Loading…</p>
      ) : shown.length === 0 ? (
        <section className="panel">
          <EmptyState
            title="No advance orders"
            description="They appear here when the sales bot sends parts that are not in stock."
          />
        </section>
      ) : (
        shown.map((order) => <OrderCard key={order.id} order={order} onAnswer={(o, l) => setAnswering({ order: o, line: l })} />)
      )}

      {answering && (
        <AnswerModal order={answering.order} line={answering.line} onClose={() => setAnswering(null)} onSaved={load} />
      )}
    </Layout>
  );
}
