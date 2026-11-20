import { useEffect, useRef, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";

// One short menu, like the sales bot's dashboard (Founder, 15 Oct 2026:
// "make it simple, remove duplicates and unused things"). Eight everyday
// pages; the rarely used ones sit under "More". Pages with no use in 30 days
// (Purchase Orders, Vendor Invoices, Delivery Tracking, Audit Log) and the
// duplicate Vendor Performance (= Performance) are no longer in the menu.
const NAV = [
  { to: "/command-centre", label: "Today" },
  { to: "/active-procurement", label: "Active Procurement" },
  { to: "/advance-orders", label: "Advance orders" },
  { to: "/customer-orders", label: "Orders" },
  { to: "/vendor-inventory", label: "Vendor stock" },
  { to: "/vendor-priority", label: "Vendors" },
  { to: "/vendor-poc", label: "POC" },
  { to: "/vendor-creation", label: "Vendor creation" },
  { to: "/dealer-portal", label: "Dealer Portal" },
  { to: "/performance", label: "Performance" },
  // Chats and groups are one inbox (20 Nov 2026); /groups opens its Groups filter.
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
  "/vendor-priority": "Brand-wise vendor lists: every vendor of the brand is asked for a part at once. Add vendors, reorder, see who replies best.",
  "/chats": "Every WhatsApp message the bot sent and received, live.",
  "/vendor-creation": "Every vendor registered on WhatsApp — its full details, whether Prateek sir / NK Jain sir approved it, and its Dealer Portal account.",
  "/active-procurement": "Live: every part on the Dealer Portal's Active Procurement, its brand, the vendors of that brand, who replied and who wins.",
  "/vendor-poc": "A vendor's people: every number here counts as the vendor, one to one and in his WhatsApp group.",
  "/dealer-portal": "Each vendor's stock is copied to its Dealer Portal account automatically: reset, then the full list.",
};

const TODAY = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  weekday: "short",
  day: "numeric",
  month: "short",
});

function SideLink({ item }) {
  return (
    <NavLink
      to={item.to}
      className={({ isActive }) => "sidenav__link" + (isActive ? " sidenav__link--active" : "")}
    >
      {item.label}
    </NavLink>
  );
}

// A vertical menu on the left (12 Nov 2026: the top menu had grown past the
// screen width, so tabs such as "Vendor creation" were hidden off the edge).
// Every page is listed; on a phone the menu opens from the ☰ button.
export function Layout({ children, title, subtitle, bare = false }) {
  const location = useLocation();
  const purpose = subtitle ?? PAGE_PURPOSE[location.pathname];
  const [open, setOpen] = useState(false);
  const panel = useRef(null);

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    if (!open) return undefined;
    function onKey(event) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className={"app-shell app-shell--side" + (open ? " app-shell--menu-open" : "") + (bare ? " app-shell--bare" : "")}>
      <header className="sidebar-top">
        <button
          type="button"
          className="sidebar-top__toggle"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-controls="sidenav"
          aria-label="Menu"
        >
          ☰
        </button>
        <NavLink to="/command-centre" className="sidenav__brand">
          <span className="sidenav__logo">C</span>
          <span>Cartrends</span>
        </NavLink>
      </header>
      {open ? <div className="sidenav__backdrop" onClick={() => setOpen(false)} /> : null}
      <aside className="sidenav" id="sidenav" ref={panel}>
        <NavLink to="/command-centre" className="sidenav__brand sidenav__brand--full">
          <span className="sidenav__logo">C</span>
          <span>
            Cartrends <span className="sidenav__brand-sub">Purchase Bot</span>
          </span>
        </NavLink>
        <nav className="sidenav__links" aria-label="Main">
          {NAV.map((item) => (
            <SideLink key={item.to} item={item} />
          ))}
          <div className="sidenav__section">More</div>
          {MORE.map((item) => (
            <SideLink key={item.to} item={item} />
          ))}
        </nav>
        <span className="bot-status sidenav__status" title="WhatsApp bot +91 92170 30414">
          <span className="bot-status__dot" />
          Bot live
        </span>
      </aside>
      <div className="app-main">
        {bare ? null : (
          <div className="page-head">
            <div>
              <h1 className="page-head__title">{title}</h1>
              {purpose ? <p className="page-head__purpose">{purpose}</p> : null}
            </div>
            <span className="page-head__date">{TODAY.format(new Date())}</span>
          </div>
        )}
        <main className="app-content">{children}</main>
      </div>
    </div>
  );
}
