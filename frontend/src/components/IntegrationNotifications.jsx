import { useEffect, useRef } from "react";
import { useAuth } from "../context/AuthContext";
import { useToast } from "../context/ToastContext";
import { fetchNotifications } from "../api/notifications";

// How often to check for new integration events. These events are infrequent,
// so a light 5s poll keeps toasts near-real-time with negligible overhead.
const POLL_INTERVAL_MS = 5000;
const VALID_LEVELS = new Set(["success", "error", "warning", "info"]);

/**
 * Renders nothing. While a user is logged in, it quietly polls the backend's
 * transient notification buffer and raises a toast for each new integration
 * event (WhatsApp / Gmail / Google Sheets imports & failures). Mounted once at
 * the app root so it survives page navigation and shows each event exactly once.
 */
export function IntegrationNotifications() {
  const { isAuthenticated } = useAuth();
  const toast = useToast();
  // Keep the latest toast API in a ref so the polling effect doesn't restart
  // every time a toast is added/removed.
  const toastRef = useRef(toast);
  toastRef.current = toast;
  const lastIdRef = useRef(null);

  useEffect(() => {
    if (!isAuthenticated) {
      lastIdRef.current = null;
      return undefined;
    }

    let cancelled = false;
    let timer;

    async function poll() {
      try {
        const { events, latest_id: latestId } = await fetchNotifications(lastIdRef.current);
        if (cancelled) return;
        if (lastIdRef.current == null) {
          // First tick: adopt the current cursor so pre-login events aren't replayed.
          lastIdRef.current = latestId ?? 0;
        } else {
          for (const event of events || []) {
            const level = VALID_LEVELS.has(event.level) ? event.level : "info";
            toastRef.current[level](event.message, { title: event.title });
          }
          if (latestId != null) lastIdRef.current = latestId;
        }
      } catch {
        // Transient poll failure (offline, expired token) -- ignore; retry next tick.
      } finally {
        if (!cancelled) timer = setTimeout(poll, POLL_INTERVAL_MS);
      }
    }

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [isAuthenticated]);

  return null;
}
