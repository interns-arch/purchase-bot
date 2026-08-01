import { apiClient } from "./client";

export async function getVendorComparison(orderId) {
  const response = await apiClient.get(`/api/vendor-comparison/${orderId}`);
  return response.data;
}

export async function downloadVendorComparisonExport(orderId, fileName) {
  const response = await apiClient.get(`/api/vendor-comparison/${orderId}/export`, {
    responseType: "blob",
  });
  const url = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = url;
  link.setAttribute("download", fileName || `vendor_comparison_order_${orderId}.xlsx`);
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}
