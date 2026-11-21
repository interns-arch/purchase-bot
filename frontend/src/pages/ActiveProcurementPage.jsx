import { useEffect, useMemo, useState } from "react";
import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";
import { extractErrorMessage } from "../api/client";
import { getActiveProcurement } from "../api/portalProcurement";

// Live Active Procurement (User, 16 Nov 2026): every part the bot is working
// on, by brand, with the vendors of that brand. Refreshes every minute.

const REFRESH_MS = 60_000;

const STATE_PILL = {
  discovered: "neutral",
  enquiry_scheduled: "info",
  awaiting_vendor_replies: "info",
  quotes_normalized: "info",
  winner_selected: "success",
  completed: "success",
  margin_hold: "warning",
  no_vendor: "danger",
  closed: "neutral",
};

function time(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function pct(v) {
  return v === null || v === undefined ? "—" : `${v}%`;
}

function Answer({ a }) {
  if (!a) return <>—</>;
  const bits = [a.vendor];
  if (a.tat !== null && a.tat !== undefined) bits.push(`${a.tat} din`);
  if (a.discount !== null && a.discount !== undefined) bits.push(`${a.discount}% off`);
  if (a.margin !== null && a.margin !== undefined) bits.push(`margin ${a.margin}%`);
  return (
    <span>
      {bits.join(" · ")}
      {a.can_win === false && a.why_not ? (
        <span style={{ color: "var(--color-text-muted)" }}> — can't win: {a.why_not}</span>
      ) : null}
    </span>
  );
}

export function ActiveProcurementPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [brand, setBrand] = useState("");
  const [onlyOpen, setOnlyOpen] = useState(true);

  async function load() {
    try {
      setData(await getActiveProcurement());
      setError(null);
    } catch (e) {
      setError(extractErrorMessage(e, "Could not load Active Procurement."));
    }
  }

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, []);

  const lines = useMemo(() => {
    const open = ["discovered", "enquiry_scheduled", "awaiting_vendor_replies", "quotes_normalized", "margin_hold"];
    return (data?.lines || []).filter(
      (l) => (!brand || l.brand === brand) && (!onlyOpen || open.includes(l.state) || l.choice_pending)
    );
  }, [data, brand, onlyOpen]);

  if (!data && !error) {
    return (
      <Layout title="Active Procurement">
        <p>Loading…</p>
      </Layout>
    );
  }

  const rules = data?.rules || {};
  return (
    <Layout title="Active Procurement">
      {error && <p style={{ color: "var(--color-danger, #b00020)" }}>{error}</p>}
      {data && (
        <>
          <section className="panel">
            <div className="toolbar" style={{ gap: 16, flexWrap: "wrap" }}>
              <span>
                <strong>Mode:</strong> {data.mode === "live" ? "Live — all vendors" : "Pilot"}
              </span>
              <span>
                <strong>Last round:</strong> {time(data.last_round)}
              </span>
              <span>
                <strong>Next round:</strong> {time(data.next_round)}
              </span>
              <span style={{ color: "var(--color-text-muted)" }}>Updated {time(data.generated_at)}</span>
            </div>
            <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "10px 0 0" }}>
              Portal read every {rules.poll_minutes} min. Open parts go to each brand's vendors at{" "}
              {(rules.round_times || []).join(", ")} (groups only), at most {rules.max_asks} times. A vendor whose stock
              sheet from today shows the part is not asked if he has it: his sheet counts as his reply, and he is asked
              only a discount or days the sheet does not give. A reply wins only with at most{" "}
              {rules.max_tat_days} days and {rules.min_margin}% margin; fewer days first, then bigger discount, then
              on-time delivery.
              {data.pilot && (
                <>
                  {" "}
                  Pilot: only {data.pilot.vendors.join(", ") || "nobody"}
                  {data.pilot.brands.length ? `, brands ${data.pilot.brands.join(", ")}` : ""}
                  {data.pilot.parts.length ? `, parts ${data.pilot.parts.join(", ")}` : ""}.
                </>
              )}
            </p>
          </section>

          <section className="panel">
            <h2 style={{ marginTop: 0 }}>Brands</h2>
            {data.brands.length === 0 ? (
              <EmptyState title="Nothing open" description="No part is waiting on Active Procurement." />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Brand</th>
                      <th>Open parts</th>
                      <th>Vendors</th>
                      <th>In a live group</th>
                      <th>No group (not asked)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.brands.map((b) => (
                      <tr key={b.brand} style={{ cursor: "pointer" }} onClick={() => setBrand(brand === b.brand ? "" : b.brand)}>
                        <td>
                          <strong>{b.brand}</strong>
                        </td>
                        <td>{b.open_lines}</td>
                        <td>{b.vendors}</td>
                        <td title={b.in_group_names.join(", ")}>{b.in_group}</td>
                        <td style={{ color: "var(--color-text-muted)" }}>{b.no_group_names.join(", ") || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <div className="toolbar" style={{ gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
              <h2 style={{ margin: 0 }}>Parts</h2>
              <select className="field__input" style={{ maxWidth: 220 }} value={brand} onChange={(e) => setBrand(e.target.value)}>
                <option value="">All brands</option>
                {data.brands.map((b) => (
                  <option key={b.brand} value={b.brand}>
                    {b.brand}
                  </option>
                ))}
              </select>
              <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <input type="checkbox" checked={onlyOpen} onChange={(e) => setOnlyOpen(e.target.checked)} />
                Open only
              </label>
              <span style={{ color: "var(--color-text-muted)" }}>{lines.length} part(s)</span>
            </div>
            {lines.length === 0 ? (
              <EmptyState title="No parts" description="Nothing matches this filter." />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Part</th>
                      <th>Brand</th>
                      <th>Qty</th>
                      <th>Customer</th>
                      <th>Status</th>
                      <th>Asked / replied</th>
                      <th>Best answer</th>
                      <th>Winner</th>
                    </tr>
                  </thead>
                  <tbody>
                    {lines.map((l) => (
                      <tr key={l.id}>
                        <td>
                          <strong>{l.part_no}</strong>
                          <div style={{ color: "var(--color-text-muted)", fontSize: "0.8rem" }}>
                            {l.po_group || "—"} · line {l.portal_line}
                            {!l.in_pilot && data.pilot ? " · not in pilot" : ""}
                          </div>
                        </td>
                        <td>{l.brand}</td>
                        <td>{l.qty}</td>
                        <td>
                          {pct(l.customer_discount)} off
                          {l.sale_price ? <div style={{ fontSize: "0.8rem" }}>pays ₹{l.sale_price}</div> : null}
                        </td>
                        <td>
                          <span className={`pill pill--${STATE_PILL[l.state] || "neutral"}`}>
                            {l.choice_pending ? `Prateek sir to choose (${l.choice_code})` : l.state_label}
                          </span>
                          {l.note && !l.choice_pending ? (
                            <div style={{ color: "var(--color-text-muted)", fontSize: "0.8rem" }}>{l.note}</div>
                          ) : null}
                        </td>
                        <td>
                          {l.asked} / {l.replied}
                          {l.asks_used ? (
                            <div style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                              ask {l.asks_used} of {l.max_asks}
                            </div>
                          ) : null}
                        </td>
                        <td>
                          <Answer a={l.best} />
                        </td>
                        <td>{l.winner ? <Answer a={l.winner} /> : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <h2 style={{ marginTop: 0 }}>Vendor record</h2>
            <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", marginTop: 0 }}>
              Used to break a tie: on-time delivery when known (promised date vs the day the Portal shows the parts
              received), else how often the vendor replies.
            </p>
            {data.vendors.length === 0 ? (
              <EmptyState title="No record yet" description="It builds up as vendors reply and deliver." />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Vendor</th>
                      <th>Delivered</th>
                      <th>On time</th>
                      <th>Asked</th>
                      <th>Replied</th>
                      <th title="Replied after the 3-hour batch window">Late</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.vendors.map((v) => (
                      <tr key={v.vendor}>
                        <td>{v.vendor}</td>
                        <td>{v.delivered}</td>
                        <td>{pct(v.on_time_pct)}</td>
                        <td>{v.asked}</td>
                        <td>{pct(v.reply_pct)}</td>
                        <td>{v.late || 0}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </Layout>
  );
}
