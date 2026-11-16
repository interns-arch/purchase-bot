import { apiClient } from "./client";

// Vendor POC numbers: a number here counts as the vendor, one to one and in
// his WhatsApp group.
export async function listPocVendors() {
  const response = await apiClient.get("/api/vendor-pocs/vendors");
  return response.data;
}

export async function getVendorPocs(vendorId) {
  const response = await apiClient.get(`/api/vendor-pocs/${vendorId}`);
  return response.data;
}

export async function addVendorPoc(vendorId, number, name) {
  const response = await apiClient.post(`/api/vendor-pocs/${vendorId}`, { number, name });
  return response.data;
}

export async function removeVendorPoc(vendorId, contactId) {
  const response = await apiClient.delete(`/api/vendor-pocs/${vendorId}/${contactId}`);
  return response.data;
}
