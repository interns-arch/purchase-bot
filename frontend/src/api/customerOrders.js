import { apiClient } from "./client";

export async function uploadCustomerOrders(files) {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }
  const response = await apiClient.post("/api/customer-orders", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return response.data;
}

export async function listCustomerOrders({ limit = 50 } = {}) {
  const response = await apiClient.get("/api/customer-orders", { params: { limit } });
  return response.data;
}

export async function listCustomerOrderItems(orderId) {
  const response = await apiClient.get(`/api/customer-orders/${orderId}/items`);
  return response.data;
}

export async function listCustomerOrderErrors(orderId) {
  const response = await apiClient.get(`/api/customer-orders/${orderId}/errors`);
  return response.data;
}
