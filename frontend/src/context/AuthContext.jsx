import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { fetchCurrentUser, login as loginRequest, logout as logoutRequest } from "../api/auth";
import { setUnauthorizedHandler } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [isLoading, setIsLoading] = useState(true);

  const clearSession = useCallback(() => {
    localStorage.removeItem("access_token");
    setUser(null);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(clearSession);
  }, [clearSession]);

  useEffect(() => {
    const token = localStorage.getItem("access_token");
    if (!token) {
      setIsLoading(false);
      return;
    }
    fetchCurrentUser()
      .then(setUser)
      .catch(() => clearSession())
      .finally(() => setIsLoading(false));
  }, [clearSession]);

  const login = useCallback(async (username, password) => {
    const { access_token: accessToken } = await loginRequest(username, password);
    localStorage.setItem("access_token", accessToken);
    const currentUser = await fetchCurrentUser();
    setUser(currentUser);
    return currentUser;
  }, []);

  const logout = useCallback(async () => {
    try {
      await logoutRequest();
    } finally {
      clearSession();
    }
  }, [clearSession]);

  const value = useMemo(
    () => ({ user, isLoading, isAuthenticated: Boolean(user), login, logout }),
    [user, isLoading, login, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
