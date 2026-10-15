import { useEffect, useRef, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";

// One short top menu, like the sales bot's dashboard (Founder, 15 Oct 2026:
// "make it simple, remove duplicates and unused things"). Eight everyday
// pages; the rarely used ones sit under "More". Pages with no use in 30 days
// (Purchase Orders, Vendor Invoices, Delivery Tracking, Audit Log) and the
// duplicate Vendor Performance (= Performance) are no longer in the menu.
const NAV = [
  { to: "/command-centre", label: "Today" },
  { to: "/advance-orders", label: "Advance orders" },
  { to: "/customer-orders", label: "Orders" },
  { to: "/vendor-inventory", label: "Vendor stock" },
  { to: "/vendor-priority", label: "Vendors" },
  { to: "/dealer-portal", label: "Dealer Portal" },
  { to: "/performance", label: "Performance" },
  { to: "/chats", label: "Chats" },
];

const MORE = [
  { to: "/vendor-comparison", label: "Vendor comparison" },
  { to: "/part-intelligence", label: "Part search" },
  { to: "/file-inbox", label: "File inbox" },
  { to: "/settings", label: "Settings" },
];

// One plain sentence per page, shown under its title.
const PAGE_PURPOSE = {
  "/command-centre": "Today at a glance — what needs your attention first.",
  "/vendor-inventory": "Stock every vendor has sent, and the history of their files.",
  "/customer-orders": "Orders received from customers, and what each one asked for.",
  "/vendor-comparison": "Which vendors have the parts an order needs — and reserve stock against them.",
  "/advance-orders": "Parts not in stock: which vendors were asked, what each quoted, and who was chosen.",
  "/part-intelligence": "Search one part: who stocks it, how much is free, at what price.",
  "/file-inbox": "Every file received — imported, failed or needing review.",
  "/settings": "Account, integrations and system status.",
  "/vendor-priority": "Brand-wise vendor order the bot follows — #1 is asked first. Add vendors, reorder, see who replies best.",
  "/chats": "Every WhatsApp message the bot sent and received, live.",
  "/dealer-portal": "Each vendor's stock is copied to its Dealer Portal account automatically: reset, then the full list.",
};

const TODAY = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  weekday: "short",
  day: "numeric",
  month: "short",
});

function MoreMenu() {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const box = useRef(null);
  const active = MORE.some((item) => location.pathname.startsWith(item.to));

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    function close(event) {
      if (box.current && !box.current.contains(event.target)) setOpen(false);
    }
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  return (
    <div className="topnav__more" ref={box}>
      <button
        type="button"
        className={"topnav__link" + (active ? " topnav__link--active" : "")}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        More ▾
      </button>
      {open ? (
        <div className="topnav__menu">
          {MORE.map((item) => (
            <NavLink key={item.to} to={item.to} className="topnav__menu-item">
              {item.label}
            </NavLink>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function Layout({ children, title, subtitle }) {
  const location = useLocation();
  const purpose = subtitle ?? PAGE_PURPOSE[location.pathname];

  return (
    <div className="app-shell app-shell--top">
      <header className="topnav">
        <div className="topnav__inner">
          <NavLink to="/command-centre" className="topnav__brand">
            <span className="topnav__logo">C</span>
            <span>
              Cartrends <span className="topnav__brand-sub">Purchase Bot</span>
            </span>
          </NavLink>
          <nav className="topnav__links">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) => "topnav__link" + (isActive ? " topnav__link--active" : "")}
              >
                {item.label}
              </NavLink>
            ))}
            <MoreMenu />
          </nav>
          <span className="bot-status" title="WhatsApp bot +91 92170 30414">
            <span className="bot-status__dot" />
            Bot live
          </span>
        </div>
      </header>
      <div className="app-main">
        <div className="page-head">
          <div>
            <h1 className="page-head__title">{title}</h1>
            {purpose ? <p className="page-head__purpose">{purpose}</p> : null}
          </div>
          <span className="page-head__date">{TODAY.format(new Date())}</span>
        </div>
        <main className="app-content">{children}</main>
      </div>
    </div>
  );
}
