import { useState } from "react";
import { Layout } from "../components/Layout";
import { useAuth } from "../context/AuthContext";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { changePassword } from "../api/auth";

export function SettingsPage() {
  const { user } = useAuth();
  const toast = useToast();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [isSaving, setIsSaving] = useState(false);

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
    </Layout>
  );
}
