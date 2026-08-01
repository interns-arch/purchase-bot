import { apiClient } from "./client";

export async function getDashboardSummary() {
  const response = await apiClient.get("/api/dashboard");
  return response.data;
}
