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

export async function getCommandCentreFunnel(days) {
  const response = await apiClient.get("/api/command-centre/funnel", { params: { days } });
  return response.data;
}

export async function getCommandCentreOrderTower(days) {
  const response = await apiClient.get("/api/command-centre/order-tower", { params: { days } });
  return response.data;
}

export async function getCommandCentrePoTower(days) {
  const response = await apiClient.get("/api/command-centre/po-tower", { params: { days } });
  return response.data;
}

export async function getCommandCentreDeliveryTower(days) {
  const response = await apiClient.get("/api/command-centre/delivery-tower", {
    params: { days },
  });
  return response.data;
}

export async function getCommandCentreVendorScorecard() {
  const response = await apiClient.get("/api/command-centre/vendor-scorecard");
  return response.data;
}

export async function getCommandCentreTrends(days) {
  const response = await apiClient.get("/api/command-centre/trends", { params: { days } });
  return response.data;
}

export async function getPartIntelligence(query) {
  const response = await apiClient.get("/api/command-centre/part-intelligence", {
    params: { q: query },
  });
  return response.data;
}

export async function getPriceLeakage(days) {
  const response = await apiClient.get("/api/command-centre/price-leakage", {
    params: { days },
  });
  return response.data;
}

export async function getAuditLog(limit) {
  const response = await apiClient.get("/api/command-centre/audit", { params: { limit } });
  return response.data;
}
