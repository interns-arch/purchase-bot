import { apiClient } from "./client";

// The WhatsApp groups the bot sits in (Whapi), read live.
export async function listGroups() {
  const response = await apiClient.get("/api/groups");
  return response.data;
}

// The live feed: what the bot took from which group and what it posted.
export async function listActivity(after = 0) {
  const response = await apiClient.get("/api/groups/activity", { params: { after } });
  return response.data;
}

export async function getGroup(groupId) {
  const response = await apiClient.get(`/api/groups/${encodeURIComponent(groupId)}`);
  return response.data;
}

export async function downloadGroupFile(mediaId, fileName) {
  const response = await apiClient.get(`/api/groups/media/${encodeURIComponent(mediaId)}`, { responseType: "blob" });
  const url = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = url;
  link.setAttribute("download", fileName || `file_${mediaId}`);
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}
