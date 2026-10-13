import { useEffect, useMemo, useState } from "react";
import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { listVendorOptions } from "../api/advanceOrders";
import {
  listDealerAccounts,
  previewDealerPush,
  pushDealerNow,
  removeDealerAccount,
  saveDealerAccount,
  setDealerEnabled,
} from "../api/dealerPortal";
import { formatDateTime } from "../utils/datetime";

// Each vendor's stock goes to its own Dealer Portal dealer account. Save the
// dealer's login once; every stock file that vendor sends afterwards is
// pushed on its own: reset-own-stock, then the full list (Google Sheet rule).

const STATUS_TONE = { SUCCESS: "success", PARTIAL: "warning", FAILED: "danger", SHADOW: "info", PENDING: "neutral" };

function LastPush({ push }) {
  if (!push) return <span style={{ color: "var(--color-text-muted)" }}>never</span>;
  return (
    <div>
      <span className={`pill pill--${STATUS_TONE[push.status] || "neutral"}`}>{push.status}</span>{" "}
      <span style={{ fontSize: "0.82rem", color: "var(--color-text-muted)" }}>{formatDateTime(push.at)}</span>
      <div style={{ fontSize: "0.82rem", marginTop: 2 }}>
        {push.rows_sent} sent · {push.inserted + push.updated} accepted · {push.failed} refused
      </div>
      {push.error && <div style={{ fontSize: "0.78rem", color: "var(--color-danger)" }}>{push.error}</div>}
    </div>
  );
}

export function DealerPortalPage() {
  const toast = useToast();
  const [data, setData] = useState(null);
  const [vendors, setVendors] = useState([]);
  const [busy, setBusy] = useState("");
  const [preview, setPreview] = useState(null);
  const [form, setForm] = useState({ vendor_id: "", username: "", password: "", dealer_id: "" });
  const [vendorFilter, setVendorFilter] = useState("");

  async function load() {
    try {
      const [d, v] = await Promise.all([listDealerAccounts(), listVendorOptions()]);
      setData(d);
      setVendors(v);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load Dealer Portal accounts."));
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const connected = useMemo(() => {
    const ids = new Set();
    (data?.accounts || []).forEach((a) => a.vendors.forEach((v) => ids.add(v.id)));
    return ids;
  }, [data]);

  const vendorChoices = vendors
    .filter((v) => !connected.has(v.vendor_id))
    .filter((v) => v.vendor_name.toLowerCase().includes(vendorFilter.toLowerCase()));

  async function save(event) {
    event.preventDefault();
    if (!form.vendor_id || !form.username.trim() || !form.password) {
      toast.error("Pick the vendor and enter the dealer's username and password.");
      return;
    }
    setBusy("save");
    try {
      const result = await saveDealerAccount({
        vendor_id: Number(form.vendor_id),
        username: form.username.trim(),
        password: form.password,
        dealer_id: form.dealer_id === "" ? null : Number(form.dealer_id),
      });
      toast.success(`Login works: dealer ${result.login.dealer_id} (${result.login.display_name}). Saved.`);
      setForm({ vendor_id: "", username: "", password: "", dealer_id: "" });
      await load();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not save the login."));
    } finally {
      setBusy("");
    }
  }

  async function doPreview(key) {
    setBusy(`preview-${key}`);
    try {
      setPreview(await previewDealerPush(key));
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not prepare the preview."));
    } finally {
      setBusy("");
    }
  }

  async function doPush(account) {
    const what = account.reset_before_upload ? "RESET this dealer's stock on the portal and upload" : "upload";
    if (!window.confirm(`${account.vendors.map((v) => v.name).join(", ")}: ${what} the latest stock now?`)) return;
    setBusy(`push-${account.key}`);
    try {
      const result = await pushDealerNow(account.key);
      toast.success(`${account.key}: ${result.status}`);
      await load();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Upload failed."));
    } finally {
      setBusy("");
    }
  }

  async function toggle(account) {
    try {
      await setDealerEnabled(account.key, !account.enabled);
      await load();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not change it."));
    }
  }

  async function remove(account) {
    if (!window.confirm(`Remove the Dealer Portal login for ${account.vendors.map((v) => v.name).join(", ")}? Its stock will stop going to the portal.`)) return;
    try {
      await removeDealerAccount(account.key);
      await load();
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not remove it."));
    }
  }

  if (!data) {
    return (
      <Layout title="Dealer Portal">
        <p>Loading…</p>
      </Layout>
    );
  }

  return (
    <Layout
      title="Dealer Portal"
      subtitle="Each vendor's stock is copied to its Dealer Portal account automatically: reset, then the full list."
    >
      <section className="panel">
        {data.accounts.length === 0 ? (
          <EmptyState title="No dealer logins yet" description="Add one below." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Vendor</th>
                  <th>Dealer</th>
                  <th>Latest stock in the bot</th>
                  <th>Last upload to portal</th>
                  <th>Auto</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.accounts.map((a) => (
                  <tr key={a.key}>
                    <td>
                      <strong>{a.vendors.map((v) => v.name).join(", ") || a.key}</strong>
                      <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
                        login {a.username || "token"} · {a.reset_before_upload ? "reset + full list" : a.full_snapshot ? "full list" : "changes only"}
                        {a.source === "env" ? " · server settings" : ""}
                      </div>
                    </td>
                    <td>{a.dealer_id ?? "—"}</td>
                    <td style={{ fontSize: "0.85rem" }}>
                      {a.stock.length
                        ? a.stock.map((s) => (
                            <div key={s.vendor_id}>
                              {s.rows} parts · {formatDateTime(s.at)}
                            </div>
                          ))
                        : "no stock yet"}
                    </td>
                    <td>
                      <LastPush push={a.last_push} />
                    </td>
                    <td>
                      <span className={`pill pill--${a.enabled ? "success" : "neutral"}`}>{a.enabled ? "On" : "Off"}</span>
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <button type="button" className="btn btn--ghost" disabled={!!busy} onClick={() => doPreview(a.key)}>
                        Preview
                      </button>{" "}
                      <button type="button" className="btn btn--primary" disabled={!!busy || !a.stock.length} onClick={() => doPush(a)}>
                        {busy === `push-${a.key}` ? "Uploading…" : "Upload now"}
                      </button>{" "}
                      {a.source === "file" && (
                        <>
                          <button type="button" className="btn btn--ghost" onClick={() => toggle(a)}>
                            {a.enabled ? "Turn off" : "Turn on"}
                          </button>{" "}
                          <button type="button" className="btn btn--ghost" onClick={() => remove(a)} aria-label="Remove">
                            ✕
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {preview && (
          <div className="panel" style={{ marginTop: 12, background: "var(--color-neutral-bg)" }}>
            <strong>Preview {preview.key}</strong> — {preview.reset_first ? "reset, then " : ""}upload {preview.rows_to_send} part(s)
            {preview.no_price ? ` · ⚠ ${preview.no_price} without price (sent as 0)` : ""}
            {preview.duplicates_collapsed ? ` · ${preview.duplicates_collapsed} duplicate(s) merged` : ""}. Nothing was sent.
            <pre style={{ fontSize: "0.8rem", margin: "8px 0 0", whiteSpace: "pre-wrap" }}>{preview.sample}</pre>
            <button type="button" className="btn btn--ghost" onClick={() => setPreview(null)}>
              Close
            </button>
          </div>
        )}
      </section>

      <section className="panel">
        <h3 style={{ fontSize: "0.95rem", margin: "0 0 8px" }}>➕ Connect a vendor to its Dealer Portal account</h3>
        {!data.file_configured && (
          <p style={{ color: "var(--color-danger)" }}>DEALER_PORTAL_ACCOUNTS_FILE is not set on the server — logins cannot be saved yet.</p>
        )}
        <form className="toolbar" style={{ gap: 8, flexWrap: "wrap" }} onSubmit={save} autoComplete="off">
          <input
            className="field__input"
            style={{ maxWidth: 160 }}
            placeholder="Search vendor…"
            value={vendorFilter}
            onChange={(e) => setVendorFilter(e.target.value)}
          />
          <select
            className="field__input"
            style={{ maxWidth: 280 }}
            value={form.vendor_id}
            onChange={(e) => setForm({ ...form, vendor_id: e.target.value })}
          >
            <option value="">Vendor…</option>
            {vendorChoices.map((v) => (
              <option key={v.vendor_id} value={v.vendor_id}>
                {v.vendor_name}
              </option>
            ))}
          </select>
          <input
            className="field__input"
            style={{ maxWidth: 170 }}
            placeholder="Portal username"
            autoComplete="off"
            value={form.username}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
          />
          <input
            className="field__input"
            style={{ maxWidth: 170 }}
            type="password"
            placeholder="Portal password"
            autoComplete="new-password"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
          <input
            className="field__input"
            style={{ maxWidth: 130 }}
            placeholder="Dealer ID (opt.)"
            inputMode="numeric"
            value={form.dealer_id}
            onChange={(e) => setForm({ ...form, dealer_id: e.target.value.replace(/\D/g, "") })}
          />
          <button type="submit" className="btn btn--primary" disabled={busy === "save" || !data.file_configured}>
            {busy === "save" ? "Checking login…" : "Test login & save"}
          </button>
        </form>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "8px 0 0" }}>
          The login is tested on the portal first and saved only if it works (and matches the dealer ID, if given). The
          password is kept on the server only and is never shown again. From then on, every stock file this vendor sends
          resets their portal stock and uploads the full list — only for vendors who always send their COMPLETE stock.
        </p>
      </section>
    </Layout>
  );
}
