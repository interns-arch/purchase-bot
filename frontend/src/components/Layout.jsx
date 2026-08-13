import { NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { useToast } from "../context/ToastContext";

// Ordered around the actual business workflow: import vendor stock -> take
// a customer order -> compare -> select vendor -> export allocation.
// Vendors are never managed manually -- they're identified automatically
// from each imported inventory file.
const NAV_ITEMS = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/vendor-inventory", label: "Vendor Inventory" },
  { to: "/customer-orders", label: "Customer Orders" },
  { to: "/vendor-comparison", label: "Vendor Comparison" },
  { to: "/delivery-tracking", label: "Delivery Tracking" },
  { to: "/vendor-invoices", label: "Vendor Invoices" },
  { to: "/purchase-orders", label: "Purchase Orders" },
  { to: "/vendor-performance", label: "Vendor Performance" },
  { to: "/file-inbox", label: "File Inbox" },
  { to: "/settings", label: "Settings" },
];

export function Layout({ children, title }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();

  async function handleLogout() {
    try {
      await logout();
      toast.success("You have been logged out.");
    } catch {
      toast.error("Logout failed, but your session was cleared locally.");
    } finally {
      navigate("/login", { replace: true });
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar__brand">Cartrends</div>
        <nav className="sidebar__nav">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => "sidebar__link" + (isActive ? " sidebar__link--active" : "")}
            >
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="app-main">
        <header className="topbar">
          <h1 className="topbar__title">{title}</h1>
          <div className="topbar__user">
            <span className="topbar__username">{user?.username}</span>
            <button type="button" className="btn btn--ghost" onClick={handleLogout}>
              Log out
            </button>
          </div>
        </header>
        <main className="app-content">{children}</main>
      </div>
    </div>
  );
}
