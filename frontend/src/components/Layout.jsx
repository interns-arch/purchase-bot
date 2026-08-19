import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { useToast } from "../context/ToastContext";

// Grouped by WHAT THE PERSON IS DOING, not by data type -- a flat list of
// thirteen names made it impossible to guess which page answered which
// question. "Daily work" follows the real business flow in order: vendor
// stock arrives -> a customer orders -> compare and reserve -> raise the PO
// -> the vendor invoices -> goods are delivered. Page names are deliberately
// unchanged; only the grouping and the one-line descriptions are new.
const NAV_GROUPS = [
  {
    label: "Overview",
    items: [{ to: "/command-centre", label: "Command Centre" }],
  },
  {
    label: "Daily work",
    items: [
      { to: "/vendor-inventory", label: "Vendor Inventory" },
      { to: "/customer-orders", label: "Customer Orders" },
      { to: "/vendor-comparison", label: "Vendor Comparison" },
      { to: "/purchase-orders", label: "Purchase Orders" },
      { to: "/vendor-invoices", label: "Vendor Invoices" },
      { to: "/delivery-tracking", label: "Delivery Tracking" },
    ],
  },
  {
    label: "Look up",
    items: [
      { to: "/part-intelligence", label: "Part Intelligence" },
      { to: "/vendor-performance", label: "Vendor Performance" },
    ],
  },
  {
    label: "System",
    items: [
      { to: "/file-inbox", label: "File Inbox" },
      { to: "/audit-log", label: "Audit Log" },
      { to: "/settings", label: "Settings" },
    ],
  },
];

// One plain sentence per page, shown under its title, so someone opening a
// tab for the first time knows what it is for without being told.
const PAGE_PURPOSE = {
  "/command-centre": "Today at a glance — what needs your attention first.",
  "/vendor-inventory": "Stock every vendor has sent, and the history of their files.",
  "/customer-orders": "Orders received from customers, and what each one asked for.",
  "/vendor-comparison":
    "Which vendors have the parts an order needs — and reserve stock against them.",
  "/purchase-orders": "Purchase orders raised for vendors, ready to send.",
  "/vendor-invoices": "Invoices vendors sent, checked against what we ordered.",
  "/delivery-tracking": "What was ordered versus what actually arrived.",
  "/part-intelligence": "Search one part: who stocks it, how much is free, at what price.",
  "/vendor-performance": "How reliable each vendor has been over time.",
  "/file-inbox": "Every file received — imported, failed or needing review.",
  "/audit-log": "Who changed what, and when.",
  "/settings": "Account, integrations and system status.",
};

export function Layout({ children, title, subtitle }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const location = useLocation();
  const purpose = subtitle ?? PAGE_PURPOSE[location.pathname];

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
          {NAV_GROUPS.map((group) => (
            <div className="sidebar__group" key={group.label}>
              <p className="sidebar__group-label">{group.label}</p>
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    "sidebar__link" + (isActive ? " sidebar__link--active" : "")
                  }
                >
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>
      <div className="app-main">
        <header className="topbar">
          <div className="topbar__heading">
            <h1 className="topbar__title">{title}</h1>
            {purpose ? <p className="topbar__purpose">{purpose}</p> : null}
          </div>
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
