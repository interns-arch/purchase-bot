import { useEffect, useState } from "react";
import { Layout } from "../components/Layout";
import { Modal } from "../components/Modal";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { getVendorRegistration, listVendorRegistrations, resendVendorApproval, sendVendorToTez } from "../api/vendorRegistrations";
import { formatDateTime } from "../utils/datetime";

// The one action here: send a waiting request to the approvers again.
function ResendDialog({ row, onClose, onSent }) {
  const toast = useToast();
  const [customer, setCustomer] = useState(row.customer_asked ? "" : "Nahi");
  const [busy, setBusy] = useState(false);

  async function send() {
    setBusy(true);
    try {
      const result = await resendVendorApproval(row.id, row.customer_asked ? undefined : customer);
      toast.success(`${row.code} sent again to ${result.sent_to}.`);
      onSent();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not resend the approval."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Resend ${row.code} for approval`} onClose={onClose} width={460}>
      <p style={{ marginTop: 0 }}>
        <strong>{row.vendor_name}</strong> is still waiting. Prateek sir and NK Jain sir get a 🔔 reminder on WhatsApp
        with all the details and the ✅ Approve / ❌ Reject buttons.
      </p>
      {row.last_resent_at && <p style={muted}>Last resent {formatDateTime(row.last_resent_at)}.</p>}
      {!row.customer_asked && (
        <div style={{ margin: "12px 0" }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>Also add as customer on the Dealer Portal?</div>
          <div style={muted}>This vendor was registered before the bot asked this, so choose it here.</div>
          <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
            {["Haan", "Nahi"].map((option) => (
              <label key={option} style={{ display: "flex", gap: 6, alignItems: "center", cursor: "pointer" }}>
                <input type="radio" name="also-customer" checked={customer === option} onChange={() => setCustomer(option)} />
                {option}
              </label>
            ))}
          </div>
        </div>
      )}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
        <button type="button" className="btn btn--ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="btn btn--primary" onClick={send} disabled={busy}>
          {busy ? "Sending…" : "🔔 Resend now"}
        </button>
      </div>
    </Modal>
  );
}

// Every vendor registered on WhatsApp: its details and whether Prateek sir /
// NK Jain sir approved it. Read-only -- the decision itself stays on WhatsApp.

const STAGES = [
  { key: "", label: "All" },
  { key: "WAITING", label: "Waiting for approval" },
  { key: "APPROVED", label: "Approved" },
  { key: "REJECTED", label: "Rejected" },
  { key: "FAILED", label: "Failed" },
  { key: "FILLING", label: "Being filled" },
  { key: "EXPIRED", label: "Expired" },
  { key: "CANCELLED", label: "Cancelled" },
];

const TONE = {
  WAITING: "warning",
  APPROVED: "success",
  REJECTED: "danger",
  FAILED: "danger",
  FILLING: "info",
  EXPIRED: "neutral",
  CANCELLED: "neutral",
};

const PORTAL_TONE = { CREATED: "success", EXISTS: "info", MAILED: "warning", FAILED: "danger", NONE: "neutral" };
const TEZ_TONE = { CREATED: "success", WAITING: "warning", FAILED: "danger", NONE: "neutral" };
const TEZ_LABEL = { CREATED: "Created on Tez", WAITING: "Waiting for OK", FAILED: "Send failed", NONE: "Not sent" };
const PORTAL_LABEL = { CREATED: "Created", EXISTS: "Already there", MAILED: "Mail sent", FAILED: "Failed", NONE: "Not yet" };

const muted = { color: "var(--color-text-muted)", fontSize: "0.82rem" };

function Approval({ row }) {
  if (row.stage === "WAITING") {
    return (
      <div>
        <span className="pill pill--warning">⏳ Not approved yet</span>
        <div style={muted}>sent {formatDateTime(row.submitted_at)}</div>
      </div>
    );
  }
  if (row.stage === "APPROVED" || row.stage === "FAILED") {
    return (
      <div>
        <span className="pill pill--success">✅ Approved</span>
        <div style={muted}>
          by {row.decided_by || "—"} · {formatDateTime(row.decided_at)}
        </div>
      </div>
    );
  }
  if (row.stage === "REJECTED") {
    return (
      <div>
        <span className="pill pill--danger">❌ Rejected</span>
        <div style={muted}>
          by {row.decided_by || "—"} · {formatDateTime(row.decided_at)}
        </div>
        {row.reason && <div style={{ fontSize: "0.82rem" }}>“{row.reason}”</div>}
      </div>
    );
  }
  return <span className={`pill pill--${TONE[row.stage] || "neutral"}`}>{row.stage_text}</span>;
}

function Details({ id, onClose }) {
  const toast = useToast();
  const [data, setData] = useState(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    getVendorRegistration(id)
      .then((d) => alive && setData(d))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load the vendor details.")));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  return (
    <section className="panel" id="vr-details" style={{ marginTop: 16 }}>
      <div className="toolbar" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <h2 style={{ margin: 0 }}>
            {data ? `${data.code} · ${data.vendor_name || "—"}` : "Loading…"}
          </h2>
          {data && <div style={muted}>Filled by {data.filled_by} · started {formatDateTime(data.started_at)}</div>}
        </div>
        <button type="button" className="btn btn--ghost" onClick={onClose} aria-label="Close details">
          ✕
        </button>
      </div>
      {data && (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
              gap: 12,
              margin: "12px 0",
            }}
          >
            <div className="panel" style={{ margin: 0 }}>
              <div style={muted}>Approval</div>
              <Approval row={data} />
            </div>
            <div className="panel" style={{ margin: 0 }}>
              <div style={muted}>Dealer Portal</div>
              <span className={`pill pill--${PORTAL_TONE[data.portal.state] || "neutral"}`}>{PORTAL_LABEL[data.portal.state] || data.portal.state}</span>
              <div style={{ fontSize: "0.85rem", marginTop: 4 }}>{data.portal.text}</div>
            </div>
            <div className="panel" style={{ margin: 0 }}>
              <div style={muted}>GST portal</div>
              {data.gst.verified ? (
                <>
                  <span className={`pill pill--${/active/i.test(data.gst.status || "") ? "success" : "warning"}`}>
                    {data.gst.status || "checked"}
                  </span>
                  <div style={{ fontSize: "0.85rem", marginTop: 4 }}>
                    {data.gst.legal_name || "—"}
                    {data.gst.registration_date ? ` · since ${data.gst.registration_date}` : ""}
                  </div>
                </>
              ) : (
                <span className="pill pill--warning">not verified online</span>
              )}
            </div>
          </div>
          {data.stage === "FILLING" && data.step && (
            <p style={muted}>Still being answered on WhatsApp — now at: {data.step}</p>
          )}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
              gap: 12,
            }}
          >
            {data.details.map((group) => (
              <div key={group.title} className="panel" style={{ margin: 0 }}>
                <h3 style={{ margin: "0 0 8px", fontSize: "0.95rem" }}>{group.title}</h3>
                <dl style={{ margin: 0 }}>
                  {group.items.map((item) => (
                    <div key={item.label} style={{ display: "flex", gap: 8, padding: "3px 0", fontSize: "0.88rem" }}>
                      <dt style={{ ...muted, fontSize: "0.85rem", minWidth: 120 }}>{item.label}</dt>
                      <dd style={{ margin: 0, wordBreak: "break-word" }}>{item.value}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

export function VendorCreationPage() {
  const toast = useToast();
  const [data, setData] = useState(null);
  const [stage, setStage] = useState("");
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(null);
  const [resend, setResend] = useState(null);

  async function sendTez(row) {
    try {
      const result = await sendVendorToTez(row.id);
      if (result.ok) toast.success(`${row.code} sent to Shubham for Tez ERP.`);
      else toast.error(`Could not send ${row.code} to Shubham.`);
      load();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not send to Tez ERP."));
    }
  }

  async function load() {
    try {
      setData(await listVendorRegistrations());
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load vendor registrations."));
    }
  }

  useEffect(() => {
    load();
    const timer = setInterval(load, 60000); // approvals happen on WhatsApp: refresh every minute
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function show(id) {
    setOpen(id);
    setTimeout(() => document.getElementById("vr-details")?.scrollIntoView({ behavior: "smooth" }), 50);
  }

  const counts = data?.counts || {};
  const needle = search.trim().toLowerCase();
  const rows = (data?.items || []).filter(
    (r) =>
      (!stage || r.stage === stage) &&
      (!needle ||
        [r.code, r.vendor_name, r.gstin, r.mobile, r.brands].some((v) => String(v || "").toLowerCase().includes(needle)))
  );

  return (
    <Layout title="Vendor creation">
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12, marginBottom: 16 }}>
        {[
          ["WAITING", "Waiting for approval", "warning"],
          ["APPROVED", "Approved", "success"],
          ["REJECTED", "Rejected", "danger"],
          ["FAILED", "Failed", "danger"],
        ].map(([key, label, tone]) => (
          <button
            key={key}
            type="button"
            className="panel"
            onClick={() => setStage(stage === key ? "" : key)}
            style={{
              margin: 0,
              textAlign: "left",
              cursor: "pointer",
              borderColor: stage === key ? `var(--color-${tone})` : undefined,
            }}
          >
            <div style={muted}>{label}</div>
            <div style={{ fontSize: "1.6rem", fontWeight: 700, color: `var(--color-${tone})` }}>{counts[key] || 0}</div>
          </button>
        ))}
      </div>

      <section className="panel">
        <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
          <input
            className="field__input"
            placeholder="Search name, GSTIN, mobile, VR-…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ maxWidth: 280 }}
          />
          <select className="field__input" value={stage} onChange={(e) => setStage(e.target.value)} style={{ maxWidth: 220 }}>
            {STAGES.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
                {s.key ? ` (${counts[s.key] || 0})` : ""}
              </option>
            ))}
          </select>
          <button type="button" className="btn btn--ghost" onClick={load}>
            Refresh
          </button>
        </div>

        {!data ? (
          <p style={muted}>Loading…</p>
        ) : rows.length === 0 ? (
          <EmptyState title="No vendor registrations here" description="New vendors are registered on WhatsApp with NEW VENDOR." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Request</th>
                  <th data-title>Vendor</th>
                  <th>GSTIN / Mobile</th>
                  <th>Prateek sir / NK Jain sir</th>
                  <th>Dealer Portal</th>
                  <th>Tez ERP</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} style={r.stage === "WAITING" ? { background: "var(--color-warning-bg)" } : undefined}>
                    <td>
                      <strong>{r.code}</strong>
                      <div style={muted}>{formatDateTime(r.submitted_at || r.started_at)}</div>
                    </td>
                    <td>
                      <strong>{r.vendor_name || "—"}</strong>
                      <div style={muted}>
                        {r.brands || "—"}
                        {r.also_customer ? ` · customer: ${r.also_customer}` : ""}
                      </div>
                      <div style={muted}>by {r.filled_by}</div>
                    </td>
                    <td style={{ fontSize: "0.88rem" }}>
                      {r.gstin || "—"}
                      <div style={muted}>{r.mobile || "—"}</div>
                    </td>
                    <td>
                      <Approval row={r} />
                    </td>
                    <td style={{ fontSize: "0.85rem", maxWidth: 260 }}>
                      <span className={`pill pill--${PORTAL_TONE[r.portal.state] || "neutral"}`}>{PORTAL_LABEL[r.portal.state] || r.portal.state}</span>
                      <div style={{ ...muted, marginTop: 2 }}>{r.portal.text}</div>
                    </td>
                    <td style={{ fontSize: "0.85rem", maxWidth: 220 }}>
                      {r.stage === "APPROVED" ? (
                        <>
                          <span className={`pill pill--${TEZ_TONE[r.tez.state] || "neutral"}`}>{TEZ_LABEL[r.tez.state] || r.tez.state}</span>
                          <div style={{ ...muted, marginTop: 2 }}>
                            {r.tez.state === "CREATED" && r.tez.done_at
                              ? `${r.tez.done_by || "OK"} · ${formatDateTime(r.tez.done_at)}`
                              : r.tez.state === "WAITING" && r.tez.sent_at
                              ? `sent ${formatDateTime(r.tez.sent_at)}`
                              : ""}
                          </div>
                          {(r.tez.state === "NONE" || r.tez.state === "FAILED") && (
                            <button type="button" className="btn btn--ghost" onClick={() => sendTez(r)}>
                              Send to Shubham
                            </button>
                          )}
                        </>
                      ) : (
                        <span style={muted}>—</span>
                      )}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {r.stage === "WAITING" && (
                        <>
                          <button type="button" className="btn btn--primary" onClick={() => setResend(r)}>
                            🔔 Resend
                          </button>{" "}
                        </>
                      )}
                      <button type="button" className="btn btn--ghost" onClick={() => show(r.id)}>
                        View details
                      </button>
                      {r.stage === "WAITING" && r.last_resent_at && (
                        <div style={muted}>resent {formatDateTime(r.last_resent_at)}</div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {open && <Details id={open} onClose={() => setOpen(null)} />}
      {resend && (
        <ResendDialog
          row={resend}
          onClose={() => setResend(null)}
          onSent={() => {
            setResend(null);
            load();
          }}
        />
      )}
    </Layout>
  );
}
