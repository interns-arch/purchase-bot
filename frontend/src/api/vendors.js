import { apiClient } from "./client";

export async function listVendors({ activeOnly = false } = {}) {
  const response = await apiClient.get("/api/vendors", {
    params: { active_only: activeOnly },
  });
  return response.data;
}

export async function getVendor(vendorId) {
  const response = await apiClient.get(`/api/vendors/${vendorId}`);
  return response.data;
}

export async function createVendor(payload) {
  const response = await apiClient.post("/api/vendors", payload);
  return response.data;
}

export async function updateVendor(vendorId, payload) {
  const response = await apiClient.patch(`/api/vendors/${vendorId}`, payload);
  return response.data;
}

export async function disableVendor(vendorId) {
  const response = await apiClient.post(`/api/vendors/${vendorId}/disable`);
  return response.data;
}
