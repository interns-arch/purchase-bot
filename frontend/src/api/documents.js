import { apiClient } from "./client";

export async function listDocuments({ status, limit } = {}) {
  const params = {};
  if (status) params.status = status;
  if (limit) params.limit = limit;
  const response = await apiClient.get("/api/documents", { params });
  return response.data;
}

export async function downloadDocument(documentId, fileName) {
  const response = await apiClient.get(`/api/documents/${documentId}/download`, {
    responseType: "blob",
  });
  const url = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = url;
  link.setAttribute("download", fileName || `document_${documentId}`);
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}
