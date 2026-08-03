import { apiClient } from "./client";

export async function getDeliveryTracking(filters = {}) {
  const response = await apiClient.get("/api/delivery-tracking", {
    params: {
      date_from: filters.dateFrom || undefined,
      date_to: filters.dateTo || undefined,
      vendor_id: filters.vendorId || undefined,
      customer_order_id: filters.customerOrderId || undefined,
      part_number: filters.partNumber || undefined,
      status: filters.status || undefined,
    },
  });
  return response.data;
}
