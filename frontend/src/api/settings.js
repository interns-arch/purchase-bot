import { apiClient } from "./client";

export async function purgeFileData(scope) {
  const response = await apiClient.post("/api/settings/purge", { scope });
  return response.data; // { scope, deleted: {table: rows}, total_rows }
}
