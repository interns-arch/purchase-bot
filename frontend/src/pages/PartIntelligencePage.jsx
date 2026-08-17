import { useState } from "react";
import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { getPartIntelligence } from "../api/commandCentre";

export function PartIntelligencePage() {
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [intel, setIntel] = useState(null);
  const [isLoading, setIsLoading] = useState(false);

  async function search(event) {
    event?.preventDefault();
    if (!query.trim()) return;
    setIsLoading(true);
    try {
      setIntel(await getPartIntelligence(query.trim()));
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not look up that part."));
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <Layout title="Part Intelligence">
      <section className="panel">
        <div className="panel__header">
          <h2>Search any part number</h2>
        </div>
        <form onSubmit={search} style={{ display: "flex", gap: 8, maxWidth: 480 }}>
          <input
            className="field__input"
            type="search"
            placeholder="e.g. 01411M0625A, OEM-111, dashes/spaces ignored"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button type="submit" className="btn" disabled={isLoading}>
            {isLoading ? "Searching…" : "Search"}
          </button>
        </form>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", marginTop: 8 }}>
          Matches every known spelling of the part — vendor aliases, root/OEM numbers, special
          characters ignored.
        </p>
      </section>

      {intel && (
        <>
          <section className="panel">
            <div className="panel__header">
              <h2>
                {intel.canonical_part_number || intel.query}
                {intel.description ? ` — ${intel.description}` : ""}
              </h2>
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
                gap: 12,
                marginBottom: 12,
              }}
            >
              <div>
                <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
                  Demand (30d)
                </div>
                <div style={{ fontSize: "1.3rem", fontWeight: 700 }}>{intel.demand_30d}</div>
              </div>
              <div>
                <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
                  Allocated (30d)
                </div>
                <div style={{ fontSize: "1.3rem", fontWeight: 700 }}>{intel.allocated_30d}</div>
              </div>
              <div>
                <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
                  Short (30d)
                </div>
                <div
                  style={{
                    fontSize: "1.3rem",
                    fontWeight: 700,
                    color: intel.short_30d > 0 ? "#dc2626" : "#16a34a",
                  }}
                >
                  {intel.short_30d}
                </div>
              </div>
              <div>
                <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
                  Best price
                </div>
                <div style={{ fontSize: "1.3rem", fontWeight: 700 }}>
                  {intel.best_price === null ? "—" : `₹${intel.best_price}`}
                </div>
              </div>
            </div>
            {intel.aliases.length > 0 && (
              <p style={{ fontSize: "0.85rem", color: "var(--color-text-muted)" }}>
                Known as: {intel.aliases.join(", ")}
              </p>
            )}
            <p style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
              {intel.price_note}
            </p>
          </section>

          <section className="panel">
            <div className="panel__header">
              <h2>Vendors carrying this part</h2>
            </div>
            {intel.vendors.length === 0 ? (
              <EmptyState
                title="No vendor stocks this part"
                description="It will appear here the moment any vendor's stock file includes it."
              />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Vendor</th>
                      <th>Their Part No.</th>
                      <th>Declared</th>
                      <th>Reserved</th>
                      <th>Live Remaining</th>
                      <th>Price</th>
                      <th>MRP</th>
                      <th>Selected?</th>
                      <th>Delivered</th>
                      <th>Short</th>
                      <th>Last Upload</th>
                    </tr>
                  </thead>
                  <tbody>
                    {intel.vendors.map((row) => (
                      <tr key={row.vendor_id}>
                        <td>
                          {row.vendor}
                          {row.vendor_code ? ` (${row.vendor_code})` : ""}
                        </td>
                        <td>{row.vendor_part_number}</td>
                        <td>{row.declared}</td>
                        <td>{row.reserved}</td>
                        <td style={{ fontWeight: 600 }}>{row.live_remaining}</td>
                        <td>
                          {row.price === null ? "—" : `₹${row.price}`}
                          {row.price !== null &&
                            intel.best_price !== null &&
                            row.price === intel.best_price && (
                              <span style={{ color: "#16a34a" }}> ★ best</span>
                            )}
                        </td>
                        <td>{row.mrp === null ? "—" : `₹${row.mrp}`}</td>
                        <td>{row.is_selected ? "✓" : "—"}</td>
                        <td>{row.delivered}</td>
                        <td style={{ color: row.delivery_short ? "#dc2626" : undefined }}>
                          {row.delivery_short}
                        </td>
                        <td>{row.last_upload || "—"}</td>
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
