// THE single place the UI formats timestamps.
//
// Business timezone is Asia/Kolkata (IST). Every displayed time is pinned to
// IST explicitly, so a user viewing the app from another timezone still sees
// Indian business time -- `toLocaleString()` without a timeZone would follow
// the viewer's machine instead.
//
// Robust to both API shapes, which is what makes double conversion impossible:
//   - "2026-08-09T11:08:29+05:30"  (current API: IST-aware)  -> used as-is
//   - "2026-08-09T05:38:29"        (legacy/naive)            -> treated as UTC
// In both cases the underlying instant is identical, and Intl renders it once
// in Asia/Kolkata. We never add or subtract an offset by hand.

const IST_TIME_ZONE = "Asia/Kolkata";
const EMPTY = "—";

// A naive ISO string (no trailing Z and no ±hh:mm offset) is stored UTC.
const NAIVE_ISO = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;

function toDate(value) {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;

  let text = String(value).trim();
  if (NAIVE_ISO.test(text)) text = `${text.replace(" ", "T")}Z`; // pin as UTC

  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** Date + time in IST, e.g. "09/08/2026, 11:08:29 am". */
export function formatDateTime(value) {
  const date = toDate(value);
  if (!date) return EMPTY;
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: IST_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

/** Date only in IST, e.g. "09/08/2026". */
export function formatDate(value) {
  const date = toDate(value);
  if (!date) return EMPTY;
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: IST_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

export { IST_TIME_ZONE };
