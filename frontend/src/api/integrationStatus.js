import { apiClient } from "./client";

export async function getWhatsAppIntegrationStatus() {
  const response = await apiClient.get("/api/integrations/whatsapp/status");
  return response.data;
}

export async function testWhatsAppConnection() {
  const response = await apiClient.post("/api/integrations/whatsapp/test-connection");
  return response.data;
}

export async function getGmailIntegrationStatus() {
  const response = await apiClient.get("/api/integrations/gmail/status");
  return response.data;
}

export async function testGmailConnection() {
  const response = await apiClient.post("/api/integrations/gmail/test-connection");
  return response.data;
}

export async function getGoogleSheetsIntegrationStatus() {
  const response = await apiClient.get("/api/integrations/google-sheets/status");
  return response.data;
}

export async function testGoogleSheetsConnection() {
  const response = await apiClient.post("/api/integrations/google-sheets/test-connection");
  return response.data;
}
