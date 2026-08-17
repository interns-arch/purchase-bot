import { apiClient } from "./client";

export async function getCommandCentreSummary() {
  const response = await apiClient.get("/api/command-centre/summary");
  return response.data;
}

export async function getCommandCentreAlerts() {
  const response = await apiClient.get("/api/command-centre/alerts");
  return response.data;
}

export async function getCommandCentreStockGaps() {
  const response = await apiClient.get("/api/command-centre/stock-gaps");
  return response.data;
}
