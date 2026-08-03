import { apiClient } from "./client";

export async function uploadDeliveryFiles(files, { onProgress } = {}) {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }

  const response = await apiClient.post("/api/deliveries/imports", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    onUploadProgress: (event) => {
      if (onProgress && event.total) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    },
  });
  return response.data;
}

export async function listDeliveryImportHistory({ limit = 50 } = {}) {
  const response = await apiClient.get("/api/deliveries/imports", {
    params: { limit },
  });
  return response.data;
}

export async function listDeliveryImportErrors(importId) {
  const response = await apiClient.get(`/api/deliveries/imports/${importId}/errors`);
  return response.data;
}
