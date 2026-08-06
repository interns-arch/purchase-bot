import { apiClient } from "./client";

export async function uploadVendorInvoices(files) {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }

  const response = await apiClient.post("/api/vendor-invoices/imports", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return response.data;
}

export async function listVendorInvoiceImports() {
  const response = await apiClient.get("/api/vendor-invoices/imports");
  return response.data;
}

export async function listVendorInvoiceLines(invoiceImportId) {
  const response = await apiClient.get(`/api/vendor-invoices/imports/${invoiceImportId}/lines`);
  return response.data;
}
