import { useState } from "react";
import { Link } from "react-router-dom";
import { Layout } from "../components/Layout";
import { useAuth } from "../context/AuthContext";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { changePassword } from "../api/auth";
import { purgeFileData } from "../api/settings";

const PURGE_OPTIONS = [
  {
    value: "all",
    label: "All file data",
    detail:
      "Vendor inventory, customer orders (with reservations and POs), invoices, " +
      "deliveries and the imported part master. Vendors, customers, users and " +
      "integration settings are kept.",
  },
  {
    value: "vendor",
    label: "Vendor Inventory files",
    detail: "All vendor inventory imports and their rows. Vendor records and codes are kept.",
  },
  {
    value: "customer",
    label: "Customer Order files",
    detail:
      "All customer orders, order lines, vendor selections/reservations and generated " +
      "purchase orders. Customer records and codes are kept.",
  },
  {
    value: "invoice",
    label: "Invoice files",
    detail: "All vendor invoice imports and their verification results.",
  },
];

export function SettingsPage() {
  const { user } = useAuth();
  const toast = useToast();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [purgeScope, setPurgeScope] = useState("vendor");
  const [isPurging, setIsPurging] = useState(false);

  async function handlePurge() {
    const option = PURGE_OPTIONS.find((o) => o.value === purgeScope);
    const confirmed = window.confirm(
      `DELETE ${option.label}?\n\n${option.detail}\n\nThis cannot be undone.`
    );
    if (!confirmed) return;
    const typed = window.prompt(`Type DELETE to permanently remove ${option.label}:`);
    if (typed !== "DELETE") {
      if (typed !== null) toast.info("Purge cancelled — confirmation text did not match.");
      return;
    }

    setIsPurging(true);
    try {
      const result = await purgeFileData(purgeScope);
      toast.success(
        result.total_rows > 0
          ? `Deleted ${option.label}: ${result.total_rows} row(s) removed.`
          : `${option.label}: nothing to delete — already empty.`
      );
    } catch (err) {
      toast.error(extractErrorMessage(err, "Could not delete the selected data."));
    } finally {
      setIsPurging(false);
    }
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");

    if (newPassword.length < 8) {
      setError("New password must be at least 8 characters.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("New password and confirmation do not match.");
      return;
    }

    setIsSaving(true);
    try {
      await changePassword(currentPassword, newPassword);
      toast.success("Password updated.");
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setError(extractErrorMessage(err, "Could not change password."));
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <Layout title="Settings">
      <section className="panel" style={{ maxWidth: 480 }}>
        <div className="panel__header">
          <h2>Account</h2>
        </div>
        <dl className="detail-list">
          <div>
            <dt>Username</dt>
            <dd>{user?.username}</dd>
          </div>
          <div>
            <dt>Role</dt>
            <dd style={{ textTransform: "capitalize" }}>{user?.role}</dd>
          </div>
        </dl>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", marginTop: 12 }}>
          Additional users and roles (Purchase Team, Warehouse, Manager) will be manageable
          from here in a later phase.
        </p>
      </section>

      <section className="panel" style={{ maxWidth: 480 }}>
        <div className="panel__header">
          <h2>Integrations</h2>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          Check WhatsApp Business connection health, webhook verification, and recent
          message/attachment activity.
        </p>
        <Link to="/settings/integrations" className="btn btn--secondary">
          View Integration Status
        </Link>
      </section>

      <section className="panel" style={{ maxWidth: 480 }}>
        <div className="panel__header">
          <h2>Change Password</h2>
        </div>
        <form onSubmit={handleSubmit}>
          <label className="field">
            <span className="field__label">Current password</span>
            <input
              className="field__input"
              type="password"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              required
            />
          </label>
          <label className="field">
            <span className="field__label">New password</span>
            <input
              className="field__input"
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              required
            />
          </label>
          <label className="field">
            <span className="field__label">Confirm new password</span>
            <input
              className="field__input"
              type="password"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              required
            />
          </label>

          {error && <p className="form-error" role="alert">{error}</p>}

          <button type="submit" className="btn btn--primary" disabled={isSaving}>
            {isSaving ? "Saving…" : "Update Password"}
          </button>
        </form>
      </section>

      <section
        className="panel"
        style={{ maxWidth: 480, border: "1px solid var(--color-danger, #b3261e)" }}
      >
        <div className="panel__header">
          <h2 style={{ color: "var(--color-danger, #b3261e)" }}>Danger Zone — Delete File Data</h2>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", marginBottom: 12 }}>
          Permanently delete imported file data from the database in one click. Vendor and
          customer records, their codes, users and integration settings are never deleted.
        </p>
        <div role="radiogroup" aria-label="What to delete">
          {PURGE_OPTIONS.map((option) => (
            <label
              key={option.value}
              style={{ display: "flex", gap: 8, alignItems: "flex-start", marginBottom: 10 }}
            >
              <input
                type="radio"
                name="purge-scope"
                value={option.value}
                checked={purgeScope === option.value}
                onChange={() => setPurgeScope(option.value)}
                style={{ marginTop: 4 }}
              />
              <span>
                <strong>{option.label}</strong>
                <br />
                <span style={{ color: "var(--color-text-muted)", fontSize: "0.8rem" }}>
                  {option.detail}
                </span>
              </span>
            </label>
          ))}
        </div>
        <button
          type="button"
          className="btn"
          onClick={handlePurge}
          disabled={isPurging}
          style={{ background: "var(--color-danger, #b3261e)", color: "#fff" }}
        >
          {isPurging ? "Deleting…" : "Delete Selected Data"}
        </button>
      </section>
    </Layout>
  );
}
