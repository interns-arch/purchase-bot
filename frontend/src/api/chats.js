import { apiClient } from "./client";

export async function listChats(q = "") {
  const response = await apiClient.get("/api/chats", { params: q ? { q } : {} });
  return response.data;
}

export async function getChat(number) {
  const response = await apiClient.get(`/api/chats/${encodeURIComponent(number)}`);
  return response.data;
}

export async function downloadChatFile(messageId, fileName) {
  const response = await apiClient.get(`/api/chats/messages/${messageId}/file`, { responseType: "blob" });
  const url = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = url;
  link.setAttribute("download", fileName || `file_${messageId}`);
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}
