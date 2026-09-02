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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, NavLink, Outlet, useLocation } from "react-router-dom";

import { api, isDemoMode } from "../api/client";
import { LoadingState } from "./FeedbackState";

const navigation = [
  { label: "Home", mobileLabel: "Home", path: "/", icon: Home },
  { label: "Approvals", mobileLabel: "Approve", path: "/approvals", icon: Send },
  { label: "Invoices", mobileLabel: "Invoices", path: "/invoices", icon: ReceiptIndianRupee },
  { label: "Activity", mobileLabel: "Activity", path: "/activity", icon: Activity },
  { label: "Connect", mobileLabel: "Connect", path: "/connect", icon: Link2 },
];

type ConnectionGate = "checking" | "connected" | "blocked";

/**
 * Every route but Connect depends on an owner already existing (see `resolve_owner` on the
 * backend). A brand-new install has no owner until Gmail is connected, so those routes would
 * otherwise surface the backend's raw "No owner found" 404. This gate sends a first-run
 * visitor straight to Connect instead, and re-checks whenever navigation leaves Connect so a
 * fresh connect (or disconnect) is picked up without a stale redirect loop.
 */
function useConnectionGate(pathname: string): ConnectionGate {
  const [gate, setGate] = useState<ConnectionGate>("checking");
  const previousPath = useRef(pathname);

  const check = useCallback(async () => {
    try {
      const status = await api.getConnection();
      setGate(status.connected ? "connected" : "blocked");
    } catch {
      // A reachable-but-erroring API is a different problem than "no owner yet" - let the
      // destination screen's own error state explain it rather than bouncing to Connect.
      setGate("connected");
    }
  }, []);

  useEffect(() => { void check(); }, [check]);

  useEffect(() => {
    if (previousPath.current === "/connect" && pathname !== "/connect") void check();
    previousPath.current = pathname;
  }, [pathname, check]);

  return gate;
}

function useTheme() {
  const [dark, setDark] = useState(() => {
    const saved = window.localStorage.getItem("topline-theme");
    return saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  });
  useEffect(() => {
    window.localStorage.setItem("topline-theme", dark ? "dark" : "light");
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
    document.documentElement.classList.toggle("theme-dark", dark);
  }, [dark]);
  return [dark, setDark] as const;
}

export function AppShell() {
  const location = useLocation();
  const [dark, setDark] = useTheme();
  const gate = useConnectionGate(location.pathname);
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
          {location.pathname === "/connect" ? (
            <Outlet />
          ) : gate === "checking" ? (
            <div className="screen-content"><LoadingState label="Checking your connection…" /></div>
          ) : gate === "blocked" ? (
            <Navigate to="/connect" replace />
          ) : (
            <Outlet />
          )}
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
