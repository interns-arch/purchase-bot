import { apiClient } from "./client";

// Poll the transient integration-event buffer. Pass the last id seen to get
// only newer events; omit it on first load to just fetch the current cursor.
export async function fetchNotifications(afterId) {
  const params = afterId == null ? {} : { after: afterId };
  const { data } = await apiClient.get("/api/notifications", { params });
  return data; // { events: [{id, level, title, message}], latest_id }
}
