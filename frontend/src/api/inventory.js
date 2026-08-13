import { apiClient } from "./client";

export async function uploadInventoryFiles(files, { onProgress } = {}) {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
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

export async function listImportHistory({ limit = 50 } = {}) {
  const response = await apiClient.get("/api/inventory/imports", {
    params: { limit },
  });
  return response.data;
}

export async function listImportErrors(importId) {
  const response = await apiClient.get(`/api/inventory/imports/${importId}/errors`);
  return response.data;
}

export async function downloadConsolidatedWorkbook() {
  const response = await apiClient.get("/api/inventory/workbook", {
    responseType: "blob",
  });
  const url = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = url;
  link.setAttribute("download", "Vendor_Inventory.xlsx");
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}
