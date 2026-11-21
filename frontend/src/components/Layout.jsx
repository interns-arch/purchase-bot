import { useEffect, useRef, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useResponsiveTables } from "./useResponsiveTables";

// The menu follows the work, top to bottom (21 Nov 2026): what needs me today,
// then the daily procurement flow, then the vendors, then where information
// comes in, then the reports. Every group has a heading so a person finds a
// page by what it is for, not by remembering its name.
const GROUPS = [
  {
    label: "Overview",
    items: [{ to: "/command-centre", label: "Today", purpose: "Today at a glance — what needs your attention first." }],
  },
  {
    label: "Procurement",
    items: [
      {
        to: "/active-procurement",
        label: "Active procurement",
        purpose: "Live: every part on the Dealer Portal's Active Procurement, its brand, the vendors of that brand, who replied and who wins.",
      },
      {
        to: "/advance-orders",
        label: "Advance orders",
        purpose: "Parts not in stock: which vendors were asked, what each quoted, and who was chosen.",
      },
      { to: "/customer-orders", label: "Customer orders", purpose: "Orders received from customers, and what each one asked for." },
    ],
  },
  {
    label: "Vendors",
    items: [
      {
        to: "/vendor-priority",
        label: "Vendors",
        purpose: "Brand-wise vendor order: every vendor of the brand is asked at once; among equally fast replies, the one higher here wins.",
      },
      { to: "/vendor-inventory", label: "Vendor stock", purpose: "Stock every vendor has sent, and the history of their files." },
      {
        to: "/vendor-creation",
        label: "Vendor creation",
        purpose: "Every vendor registered on WhatsApp — its full details, whether it was approved, its Dealer Portal account and its Tez ERP status.",
      },
      {
        to: "/vendor-comparison",
        label: "Vendor comparison",
        purpose: "Which vendors have the parts an order needs — and reserve stock against them.",
      },
    ],
  },
  {
    label: "Messages & files",
    items: [
      { to: "/chats", label: "Chats", purpose: "Every WhatsApp message the bot sent and received, live." },
      { to: "/file-inbox", label: "File inbox", purpose: "Every file received — imported, failed or needing review." },
      {
        to: "/dealer-portal",
        label: "Dealer Portal",
        purpose: "Each vendor's stock is copied to its Dealer Portal account automatically: reset, then the full list.",
      },
    ],
  },
  {
    label: "Reports",
    items: [
      { to: "/performance", label: "Performance", purpose: "How each vendor and each team member is doing." },
      { to: "/part-intelligence", label: "Part search", purpose: "Search one part: who stocks it, how much is free, at what price." },
    ],
  },
  {
    label: "System",
    items: [
      { to: "/settings", label: "Settings", purpose: "Account, integrations and system status." },
      { to: "/settings/integrations", label: "Integrations", purpose: "Whether each connection (WhatsApp, Dealer Portal, Gmail, Sheets) is working.", hidden: true },
    ],
  },
];

const FLAT = GROUPS.flatMap((group) => group.items.map((item) => ({ ...item, group: group.label })));

function currentPage(pathname) {
  // The longest matching route wins ("/settings/integrations" over "/settings").
  return FLAT.filter((item) => pathname === item.to || pathname.startsWith(item.to + "/")).sort(
    (a, b) => b.to.length - a.to.length
  )[0];
}

const TODAY = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  weekday: "short",
  day: "numeric",
  month: "short",
});

function SideLink({ item }) {
  return (
    <NavLink to={item.to} className={({ isActive }) => "sidenav__link" + (isActive ? " sidenav__link--active" : "")}>
      {item.label}
    </NavLink>
  );
}

// A vertical menu on the left; on a phone it opens from the ☰ button.
export function Layout({ children, title, subtitle, bare = false }) {
  const location = useLocation();
  const page = currentPage(location.pathname);
  const heading = page?.label || title;
  const purpose = subtitle ?? page?.purpose;
  const [open, setOpen] = useState(false);
  const content = useRef(null);

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    if (!open) return undefined;
    function onKey(event) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);
  useEffect(() => {
    document.title = heading ? `${heading} · Cartrends Purchase Bot` : "Cartrends Purchase Bot";
  }, [heading]);

  // Tables that do not fit become record rows -- never a sideways scroll.
  useResponsiveTables(content, [location.pathname]);

  return (
    <div className={"app-shell app-shell--side" + (open ? " app-shell--menu-open" : "") + (bare ? " app-shell--bare" : "")}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
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
          <span>{heading || "Cartrends"}</span>
        </NavLink>
      </header>
      {open ? <div className="sidenav__backdrop" onClick={() => setOpen(false)} /> : null}
      <aside className="sidenav" id="sidenav">
        <NavLink to="/command-centre" className="sidenav__brand sidenav__brand--full">
          <span className="sidenav__logo">C</span>
          <span>
            Cartrends <span className="sidenav__brand-sub">Purchase Bot</span>
          </span>
        </NavLink>
        <nav className="sidenav__links" aria-label="Main">
          {GROUPS.map((group) => (
            <div className="sidenav__group" key={group.label}>
              <div className="sidenav__section">{group.label}</div>
              {group.items
                .filter((item) => !item.hidden)
                .map((item) => (
                  <SideLink key={item.to} item={item} />
                ))}
            </div>
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
            <div className="page-head__text">
              {page && page.group !== heading ? (
                <nav className="page-head__crumb" aria-label="You are here">
                  <span>{page.group}</span>
                  <span aria-hidden="true">›</span>
                  <span>{heading}</span>
                </nav>
              ) : null}
              <h1 className="page-head__title">{heading}</h1>
              {purpose ? <p className="page-head__purpose">{purpose}</p> : null}
            </div>
            <span className="page-head__date">{TODAY.format(new Date())}</span>
          </div>
        )}
        <main className="app-content" id="main" ref={content}>
          {children}
        </main>
      </div>
    </div>
  );
}
