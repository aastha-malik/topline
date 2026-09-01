import {
  Activity,
  CircleUserRound,
  Home,
  Link2,
  Moon,
  ReceiptIndianRupee,
  Send,
  ShieldCheck,
  Sun,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { isDemoMode } from "../api/client";

const navigation = [
  { label: "Home", mobileLabel: "Home", path: "/", icon: Home },
  { label: "Approvals", mobileLabel: "Approve", path: "/approvals", icon: Send },
  { label: "Invoices", mobileLabel: "Invoices", path: "/invoices", icon: ReceiptIndianRupee },
  { label: "Activity", mobileLabel: "Activity", path: "/activity", icon: Activity },
  { label: "Connect", mobileLabel: "Connect", path: "/connect", icon: Link2 },
];

function useTheme() {
  const [dark, setDark] = useState(() => {
    const saved = window.localStorage.getItem("topline-theme");
    return saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  });
  useEffect(() => {
    window.localStorage.setItem("topline-theme", dark ? "dark" : "light");
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
  }, [dark]);
  return [dark, setDark] as const;
}

export function AppShell() {
  const location = useLocation();
  const [dark, setDark] = useTheme();
  const title = navigation.find((item) => item.path === location.pathname)?.label ?? "Home";
  const dateLabel = useMemo(() => new Intl.DateTimeFormat("en-IN", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(new Date()).toUpperCase(), []);

  return (
    <div className={dark ? "app-shell theme-dark" : "app-shell"}>
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className="side-rail">
        <NavLink className="brand-lockup" to="/" aria-label="Topline home">
          <span className="brand-mark" aria-hidden="true">T</span>
          <span>Topline</span>
        </NavLink>
        <nav aria-label="Primary navigation">
          {navigation.map(({ label, path, icon: Icon }) => (
            <NavLink className={({ isActive }) => isActive ? "nav-item active" : "nav-item"} end={path === "/"} to={path} key={path}>
              <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="control-promise">
          <span className="promise-seal" aria-hidden="true"><ShieldCheck size={20} /></span>
          <div>
            <strong>You stay in control</strong>
            <p>Nothing is sent without your approval.</p>
          </div>
        </div>
      </aside>

      <section className="main-surface">
        <header className="topbar">
          <NavLink className="mobile-brand" to="/" aria-label="Topline home">T</NavLink>
          <div className="header-copy">
            <p className="date-line">{dateLabel}</p>
            <h1>{title === "Home" ? "Good morning." : title}</h1>
          </div>
          <div className="top-actions">
            {isDemoMode && <span className="demo-label">Illustrative data</span>}
            <button className="theme-toggle" type="button" onClick={() => setDark(!dark)} aria-pressed={dark} aria-label={dark ? "Use light theme" : "Use dark theme"}>
              {dark ? <Sun size={17} aria-hidden="true" /> : <Moon size={17} aria-hidden="true" />}
              <span>{dark ? "Light" : "Dark"}</span>
            </button>
            <button className="avatar" type="button" aria-label="Account menu">
              <CircleUserRound size={20} aria-hidden="true" />
            </button>
          </div>
        </header>
        <main id="main-content" tabIndex={-1}>
          <Outlet />
        </main>
      </section>

      <nav className="mobile-nav" aria-label="Mobile navigation">
        {navigation.map(({ mobileLabel, path, icon: Icon }) => (
          <NavLink className={({ isActive }) => isActive ? "active" : ""} end={path === "/"} to={path} key={path}>
            <Icon size={19} strokeWidth={1.8} aria-hidden="true" />
            <span>{mobileLabel}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
