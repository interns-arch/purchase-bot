import { formatDateTime } from "../utils/datetime";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Layout } from "../components/Layout";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import {
  getGmailIntegrationStatus,
  getGoogleSheetsIntegrationStatus,
  getWhatsAppIntegrationStatus,
  testGmailConnection,
  testGoogleSheetsConnection,
  testWhatsAppConnection,
} from "../api/integrationStatus";

function StatusCard({ label, status, hint }) {
  return (
    <div className="stat-card">
      <p className="stat-card__label">{label}</p>
      <p className="stat-card__value">
        <StatusPill status={status} />
      </p>
      {hint && <p className="stat-card__hint">{hint}</p>}
    </div>
  );
}

function formatWhen(value) {
  return value ? formatDateTime(value) : "Never";
}

export function IntegrationStatusPage() {
  const toast = useToast();
  const [status, setStatus] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isTesting, setIsTesting] = useState(false);
  const [gmailStatus, setGmailStatus] = useState(null);
  const [isGmailLoading, setIsGmailLoading] = useState(true);
  const [isTestingGmail, setIsTestingGmail] = useState(false);
  const [sheetsStatus, setSheetsStatus] = useState(null);
  const [isSheetsLoading, setIsSheetsLoading] = useState(true);
  const [isTestingSheets, setIsTestingSheets] = useState(false);

  async function load() {
    setIsLoading(true);
    try {
      const data = await getWhatsAppIntegrationStatus();
      setStatus(data);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load integration status."));
    } finally {
      setIsLoading(false);
    }
  }

  async function loadGmail() {
    setIsGmailLoading(true);
    try {
      const data = await getGmailIntegrationStatus();
      setGmailStatus(data);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load Gmail integration status."));
    } finally {
      setIsGmailLoading(false);
    }
  }

  async function loadSheets() {
    setIsSheetsLoading(true);
    try {
      const data = await getGoogleSheetsIntegrationStatus();
      setSheetsStatus(data);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load Google Sheets integration status."));
    } finally {
      setIsSheetsLoading(false);
    }
  }

  useEffect(() => {
    load();
    loadGmail();
    loadSheets();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleTestConnection() {
    setIsTesting(true);
    try {
      const data = await testWhatsAppConnection();
      setStatus(data);
      if (data.connection_status === "CONNECTED") {
        toast.success(data.connection_message || "Connected.");
      } else {
        toast.error(data.connection_message || "Connection test failed.");
      }
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not test the connection."));
    } finally {
      setIsTesting(false);
    }
  }

  async function handleTestGmailConnection() {
    setIsTestingGmail(true);
    try {
      const data = await testGmailConnection();
      setGmailStatus(data);
      if (data.poll_status === "CONNECTED") {
        toast.success(data.poll_message || "Connected.");
      } else {
        toast.error(data.poll_message || "Connection test failed.");
      }
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not test the Gmail connection."));
    } finally {
      setIsTestingGmail(false);
    }
  }

  async function handleTestSheetsConnection() {
    setIsTestingSheets(true);
    try {
      const data = await testGoogleSheetsConnection();
      setSheetsStatus(data);
      if (data.sync_status === "CONNECTED") {
        toast.success(data.sync_message || "Connected.");
      } else {
        toast.error(data.sync_message || "Connection test failed.");
      }
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not test the Google Sheets connection."));
    } finally {
      setIsTestingSheets(false);
    }
  }

  return (
    <Layout title="Integration Status">
      <Link to="/settings" className="link-button" style={{ display: "inline-block", marginBottom: 12 }}>
        ← Back to Settings
      </Link>

      <section className="panel">
        <div className="panel__header">
          <h2>WhatsApp Business Integration</h2>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="button" className="btn btn--ghost" onClick={load} disabled={isLoading}>
              Refresh
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={handleTestConnection}
              disabled={isTesting}
            >
              {isTesting ? "Testing…" : "Test Connection"}
            </button>
          </div>
        </div>

        {isLoading || !status ? (
          <div className="page-loading">Loading integration status…</div>
        ) : (
          <>
            <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", marginBottom: 16 }}>
              As of {formatWhen(status.checked_at)}. Once WhatsApp credentials are added to{" "}
              <code>backend/.env</code>, this page reflects the real integration state without any
              code changes -- just refresh or test the connection again.
            </p>

            <div className="stat-grid">
              <StatusCard
                label="WhatsApp Business Connection"
                status={status.connection_status}
                hint={status.connection_message}
              />
              <StatusCard
                label="Webhook Verification Status"
                status={status.webhook_verification_status}
                hint={
                  status.last_webhook_verified_at
                    ? `Last verified ${formatWhen(status.last_webhook_verified_at)}`
                    : "Meta hasn't completed the webhook handshake yet."
                }
              />
              <StatusCard
                label="Access Token Status"
                status={status.access_token_status}
                hint={status.access_token_configured ? undefined : "WHATSAPP_ACCESS_TOKEN not set."}
              />
              <StatusCard label="API Health" status={status.api_health} />
              <StatusCard
                label="Media Download Status"
                status={status.media_download_status}
                hint={status.media_download_message}
              />
              <StatusCard
                label="Integration Enabled"
                status={status.enabled ? "SUCCESS" : "DISABLED"}
                hint={status.enabled ? undefined : "WHATSAPP_ENABLED is false -- the webhook no-ops."}
              />
            </div>
          </>
        )}
      </section>

      {status && (
        <section className="panel">
          <div className="panel__header">
            <h2>Details</h2>
          </div>
          <dl className="detail-list">
            <div>
              <dt>Webhook URL</dt>
              <dd>{status.webhook_url || "Not configured (WHATSAPP_WEBHOOK_CALLBACK_URL)"}</dd>
            </div>
            <div>
              <dt>Last Incoming WhatsApp Message</dt>
              <dd>
                {status.last_incoming_message
                  ? `${status.last_incoming_message.filename} from ${status.last_incoming_message.sender || "unknown sender"} — ${formatWhen(status.last_incoming_message.occurred_at)}`
                  : "No WhatsApp messages received yet."}
              </dd>
            </div>
            <div>
              <dt>Last Downloaded Attachment</dt>
              <dd>
                {status.last_downloaded_attachment
                  ? `${status.last_downloaded_attachment.filename} from ${status.last_downloaded_attachment.sender || "unknown sender"} — ${formatWhen(status.last_downloaded_attachment.occurred_at)}`
                  : "No attachments downloaded yet."}
              </dd>
            </div>
            <div>
              <dt>Last Connection Test</dt>
              <dd>
                {status.last_connection_tested_at
                  ? `${formatWhen(status.last_connection_tested_at)} — ${status.connection_message || ""}`
                  : "Not tested yet."}
              </dd>
            </div>
          </dl>
        </section>
      )}

      <section className="panel">
        <div className="panel__header">
          <h2>Gmail Customer Order Automation</h2>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="button" className="btn btn--ghost" onClick={loadGmail} disabled={isGmailLoading}>
              Refresh
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={handleTestGmailConnection}
              disabled={isTestingGmail}
            >
              {isTestingGmail ? "Testing…" : "Test Connection"}
            </button>
          </div>
        </div>

        {isGmailLoading || !gmailStatus ? (
          <div className="page-loading">Loading Gmail integration status…</div>
        ) : (
          <>
            <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", marginBottom: 16 }}>
              As of {formatWhen(gmailStatus.checked_at)}. Once Gmail credentials are added to{" "}
              <code>backend/.env</code>, this page reflects the real integration state without any
              code changes -- just refresh or test the connection again.
            </p>

            <div className="stat-grid">
              <StatusCard
                label="Mailbox Connection"
                status={gmailStatus.poll_status}
                hint={gmailStatus.poll_message}
              />
              <StatusCard
                label="Auth Mode"
                status={gmailStatus.auth_mode.toUpperCase()}
                hint="Set via GMAIL_AUTH_MODE (imap or oauth)."
              />
              <StatusCard
                label="Integration Enabled"
                status={gmailStatus.enabled ? "SUCCESS" : "DISABLED"}
                hint={
                  gmailStatus.enabled
                    ? `Polling every ${gmailStatus.poll_interval_seconds}s.`
                    : "ENABLE_EMAIL_AUTOMATION / GMAIL_ENABLED is false."
                }
              />
            </div>

            <dl className="detail-list" style={{ marginTop: 16 }}>
              <div>
                <dt>Last Poll</dt>
                <dd>{formatWhen(gmailStatus.last_poll_at)}</dd>
              </div>
              <div>
                <dt>Last Message Processed</dt>
                <dd>{formatWhen(gmailStatus.last_message_processed_at)}</dd>
              </div>
            </dl>
          </>
        )}
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>Google Sheets Sync</h2>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="button" className="btn btn--ghost" onClick={loadSheets} disabled={isSheetsLoading}>
              Refresh
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={handleTestSheetsConnection}
              disabled={isTestingSheets}
            >
              {isTestingSheets ? "Testing…" : "Test Connection"}
            </button>
          </div>
        </div>

        {isSheetsLoading || !sheetsStatus ? (
          <div className="page-loading">Loading Google Sheets integration status…</div>
        ) : (
          <>
            <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", marginBottom: 16 }}>
              As of {formatWhen(sheetsStatus.checked_at)}. Every vendor inventory import (manual or
              WhatsApp) pushes that vendor's active inventory to its own worksheet automatically once
              this is configured in <code>backend/.env</code>.
            </p>

            <div className="stat-grid">
              <StatusCard
                label="Sheets Connection"
                status={sheetsStatus.sync_status}
                hint={sheetsStatus.sync_message}
              />
              <StatusCard
                label="Sync Enabled"
                status={sheetsStatus.enabled ? "SUCCESS" : "DISABLED"}
                hint={sheetsStatus.enabled ? undefined : "ENABLE_GOOGLE_SHEETS_SYNC is false."}
              />
            </div>

            <dl className="detail-list" style={{ marginTop: 16 }}>
              <div>
                <dt>Last Sync</dt>
                <dd>{formatWhen(sheetsStatus.last_sync_at)}</dd>
              </div>
              <div>
                <dt>Last Synced Vendor</dt>
                <dd>{sheetsStatus.last_synced_vendor_name || "None yet"}</dd>
              </div>
            </dl>
          </>
        )}
      </section>
    </Layout>
  );
}
