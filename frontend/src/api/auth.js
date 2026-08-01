import { apiClient } from "./client";

export async function login(username, password) {
  const body = new URLSearchParams();
  body.set("username", username);
  body.set("password", password);
  const response = await apiClient.post("/api/auth/login", body, {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
  return response.data; // { access_token, token_type }
}

export async function logout() {
  await apiClient.post("/api/auth/logout");
}

export async function fetchCurrentUser() {
  const response = await apiClient.get("/api/auth/me");
  return response.data; // { id, username, role }
}

export async function changePassword(currentPassword, newPassword) {
  await apiClient.post("/api/auth/change-password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
}
