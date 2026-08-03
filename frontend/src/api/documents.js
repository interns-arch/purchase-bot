import { apiClient } from "./client";

export async function listIncomingDocuments({ source, documentType, status, limit = 50 } = {}) {
  const response = await apiClient.get("/api/documents", {
    params: { source, document_type: documentType, status, limit },
  });
  return response.data;
}
