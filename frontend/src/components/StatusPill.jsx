const STATUS_TONE = {
  COMPLETED: "success",
  CAN_FULFILL: "success",
  ACTIVE: "success",
  COMPLETED_WITH_ERRORS: "warning",
  PARTIAL: "warning",
  AWAITING_CONFIRMATION: "warning",
  PROCESSING: "info",
  PENDING: "info",
  SKIPPED_DUPLICATE: "neutral",
  SUPERSEDED: "neutral",
  CANCELLED: "neutral",
  FAILED: "danger",
  UNFULFILLED: "danger",
};

export function StatusPill({ status }) {
  const tone = STATUS_TONE[status] || "neutral";
  const label = String(status || "-").replaceAll("_", " ");
  return <span className={`pill pill--${tone}`}>{label}</span>;
}
