import { apiClient } from "./client";

export async function listVendorSelections(orderId) {
  const response = await apiClient.get(`/api/vendor-selection/${orderId}`);
  return response.data;
}

export async function selectVendor(orderId, orderItemId, { vendorId, quantitySelected }) {
  const response = await apiClient.put(`/api/vendor-selection/${orderId}/items/${orderItemId}`, {
    vendor_id: vendorId,
    quantity_selected: quantitySelected,
  });
  return response.data;
}

export async function downloadSelectedVendorsExport(orderId, fileName) {
  const response = await apiClient.get(`/api/vendor-selection/${orderId}/export`, {
    responseType: "blob",
  });
  const url = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = url;
  link.setAttribute("download", fileName || `selected_vendors_order_${orderId}.xlsx`);
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}
