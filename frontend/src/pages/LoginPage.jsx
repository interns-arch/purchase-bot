import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { extractErrorMessage } from "../api/client";

export function LoginPage() {
  const { login, isAuthenticated } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  if (isAuthenticated) {
    return <Navigate to={location.state?.from?.pathname || "/dashboard"} replace />;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    setIsSubmitting(true);
    try {
      await login(username.trim(), password);
      navigate(location.state?.from?.pathname || "/dashboard", { replace: true });
    } catch (err) {
      setError(extractErrorMessage(err, "Incorrect username or password."));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={handleSubmit}>
        <h1 className="login-card__brand">Cartrends</h1>
        <p className="login-card__subtitle">Vendor Inventory &amp; Order Fulfillment</p>

        <label className="field">
          <span className="field__label">Username</span>
          <input
            className="field__input"
            type="text"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
          />
        </label>

        <label className="field">
          <span className="field__label">Password</span>
          <input
            className="field__input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>

        {error && <p className="form-error" role="alert">{error}</p>}

        <button type="submit" className="btn btn--primary btn--block btn--large" disabled={isSubmitting}>
          {isSubmitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
