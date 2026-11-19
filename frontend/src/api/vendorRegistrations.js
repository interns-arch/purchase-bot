import { apiClient } from "./client";

// Vendor registrations filled on WhatsApp, and whether they were approved.
export async function listVendorRegistrations(stage) {
  const response = await apiClient.get("/api/vendor-registrations", { params: stage ? { stage } : {} });
  return response.data;
}

export async function getVendorRegistration(id) {
  const response = await apiClient.get(`/api/vendor-registrations/${id}`);
  return response.data;
}

// Send a waiting registration to Prateek sir / NK Jain sir again (a reminder
// with the Approve / Reject buttons). alsoCustomer: "Haan" / "Nahi", only for
// a request filled before that question existed.
export async function resendVendorApproval(id, alsoCustomer) {
  const response = await apiClient.post(
    `/api/vendor-registrations/${id}/resend`,
    alsoCustomer ? { also_customer: alsoCustomer } : {}
  );
  return response.data;
}

// Send (or resend) the vendor's full details to Shubham as "Create vendor on
// Tez ERP". His OK reply marks the vendor created on Tez ERP.
export async function sendVendorToTez(id) {
  const response = await apiClient.post(`/api/vendor-registrations/${id}/tez-send`);
  return response.data;
}
