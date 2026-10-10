import { apiClient } from "./client";

// Desk endpoints (normal login). The sales bot uses the X-Api-Key routes
// instead; this page never needs that key.

export async function listAdvanceOrders(limit = 50) {
  const response = await apiClient.get("/api/advance-orders", { params: { limit } });
  return response.data;
}

export async function recordLineAnswer(orderId, lineId, answer) {
  const response = await apiClient.post(
    `/api/advance-orders/${orderId}/lines/${lineId}/answer`,
    answer
  );
  return response.data;
}

export async function getQuoteSettings() {
  const response = await apiClient.get("/api/advance-orders/settings/quotes");
  return response.data;
}

export async function listVendorBrands() {
  const response = await apiClient.get("/api/vendor-brands");
  return response.data;
}

export async function setBrandVendors(brand, vendorIds) {
  const response = await apiClient.put(`/api/vendor-brands/${encodeURIComponent(brand)}`, {
    vendor_ids: vendorIds,
  });
  return response.data;
}

export async function addBrandVendor(brand, vendor) {
  const response = await apiClient.post(`/api/vendor-brands/${encodeURIComponent(brand)}/vendors`, vendor);
  return response.data;
}

export async function getVendorPerformance() {
  const response = await apiClient.get("/api/vendor-brands/performance");
  return response.data;
}

export async function listVendorOptions() {
  const response = await apiClient.get("/api/vendor-brands/vendors");
  return response.data;
}
