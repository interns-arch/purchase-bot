import { apiClient } from "./client";

export async function uploadInventoryFiles(files, { vendorId, onProgress } = {}) {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }
  if (vendorId) {
    formData.append("vendor_id", String(vendorId));
  }

  const response = await apiClient.post("/api/inventory/imports", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    onUploadProgress: (event) => {
      if (onProgress && event.total) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    },
  });
  return response.data;
}

export async function listImportHistory({ vendorId, limit = 50 } = {}) {
  const response = await apiClient.get("/api/inventory/imports", {
    params: { vendor_id: vendorId, limit },
  });
  return response.data;
}

export async function listImportErrors(importId) {
  const response = await apiClient.get(`/api/inventory/imports/${importId}/errors`);
  return response.data;
}

export async function confirmImport(importId) {
  const response = await apiClient.post(`/api/inventory/imports/${importId}/confirm`);
  return response.data;
}

export async function cancelImport(importId) {
  const response = await apiClient.post(`/api/inventory/imports/${importId}/cancel`);
  return response.data;
}

export async function listVendorInventoryItems(vendorId) {
  const response = await apiClient.get(`/api/inventory/vendors/${vendorId}/items`);
  return response.data;
}
