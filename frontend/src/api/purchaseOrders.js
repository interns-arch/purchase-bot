import { apiClient } from "./client";

export async function listPurchaseOrders({ orderId } = {}) {
  const response = await apiClient.get("/api/purchase-orders", {
    params: orderId ? { order_id: orderId } : undefined,
  });
  return response.data;
}

export async function listPurchaseOrderLines(poId) {
  const response = await apiClient.get(`/api/purchase-orders/${poId}/lines`);
  return response.data;
}

export async function resendPurchaseOrderEmail(poId) {
  const response = await apiClient.post(`/api/purchase-orders/${poId}/resend-email`);
  return response.data;
}

export async function downloadPurchaseOrder(poId, fileName) {
  const response = await apiClient.get(`/api/purchase-orders/${poId}/export`, {
    responseType: "blob",
  });
  const url = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = url;
  link.setAttribute("download", fileName || `purchase_order_${poId}.xlsx`);
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}
