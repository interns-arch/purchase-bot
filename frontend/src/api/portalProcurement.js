import { apiClient } from "./client";

// Live Active Procurement: parts, brands, vendors, who replied, who wins.
export async function getActiveProcurement() {
  const response = await apiClient.get("/api/portal-procurement/live");
  return response.data;
}
