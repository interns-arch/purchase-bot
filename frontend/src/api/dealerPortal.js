import { apiClient } from "./client";

// Dealer Portal accounts (Founder, 13 Oct 2026): the dealer's own login per
// vendor, saved once -- the bot then resets + uploads each new stock file.

export async function listDealerAccounts() {
  const response = await apiClient.get("/api/dealer-portal/accounts");
  return response.data;
}

export async function saveDealerAccount(account) {
  const response = await apiClient.post("/api/dealer-portal/accounts", account);
  return response.data;
}

export async function previewDealerPush(key) {
  const response = await apiClient.post(`/api/dealer-portal/accounts/${encodeURIComponent(key)}/preview`);
  return response.data;
}

export async function pushDealerNow(key) {
  const response = await apiClient.post(`/api/dealer-portal/accounts/${encodeURIComponent(key)}/push`);
  return response.data;
}

export async function setDealerEnabled(key, enabled) {
  const response = await apiClient.post(`/api/dealer-portal/accounts/${encodeURIComponent(key)}/enabled`, { enabled });
  return response.data;
}

export async function removeDealerAccount(key) {
  const response = await apiClient.delete(`/api/dealer-portal/accounts/${encodeURIComponent(key)}`);
  return response.data;
}
