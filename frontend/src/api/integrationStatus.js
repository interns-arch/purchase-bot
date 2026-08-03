import { apiClient } from "./client";

export async function getWhatsAppIntegrationStatus() {
  const response = await apiClient.get("/api/integrations/whatsapp/status");
  return response.data;
}

export async function testWhatsAppConnection() {
  const response = await apiClient.post("/api/integrations/whatsapp/test-connection");
  return response.data;
}
