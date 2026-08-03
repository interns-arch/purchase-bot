import { apiClient } from "./client";

export async function getVendorPerformance(filters = {}) {
  const response = await apiClient.get("/api/vendor-performance", {
    params: {
      date_from: filters.dateFrom || undefined,
      date_to: filters.dateTo || undefined,
      vendor_id: filters.vendorId || undefined,
      customer_order_id: filters.customerOrderId || undefined,
      part_number: filters.partNumber || undefined,
    },
  });
  return response.data;
}

export async function getVendorDetail(vendorId) {
  const response = await apiClient.get(`/api/vendor-performance/${vendorId}`);
  return response.data;
}
