import {
  Activity,
  CircleUserRound,
  Home,
  Link2,
  ListChecks,
  LogOut,
  Moon,
  ReceiptIndianRupee,
  Send,
  ShieldCheck,
  Sun,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, NavLink, Outlet, useLocation } from "react-router-dom";

import { api, isDemoMode } from "../api/client";
import type { SessionInfo } from "../api/types";
import { LoadingState } from "./FeedbackState";

const navigation = [
  { label: "Home", mobileLabel: "Home", path: "/", icon: Home },
  { label: "Daily queue", mobileLabel: "Queue", path: "/queue", icon: ListChecks },
  { label: "Approvals", mobileLabel: "Approve", path: "/approvals", icon: Send },
  { label: "Invoices", mobileLabel: "Invoices", path: "/invoices", icon: ReceiptIndianRupee },
  { label: "Activity", mobileLabel: "Activity", path: "/activity", icon: Activity },
  { label: "Connect", mobileLabel: "Connect", path: "/connect", icon: Link2 },
];

type AuthGate = "checking" | "authed" | "signed-out";
type ConnectionGate = "checking" | "connected" | "blocked";

const DEMO_SESSION: SessionInfo = {
  authenticated: true,
  user: { id: "demo", email: "you@yourbusiness.in", name: "You" },
  workspace: { id: "demo", business_name: "Demo workspace" },
};

/**
 * Every route in the shell requires a signed-in owner. The backend establishes the session
 * in the Google OAuth callback; here we just ask `/auth/session` who we are and bounce to
 * `/signin` when the answer is 401. Demo mode has no backend, so it is always "authed".
 */
function useAuthGate(): { gate: AuthGate; session: SessionInfo | null } {
  const [gate, setGate] = useState<AuthGate>(isDemoMode ? "authed" : "checking");
  const [session, setSession] = useState<SessionInfo | null>(isDemoMode ? DEMO_SESSION : null);

  useEffect(() => {
    if (isDemoMode) return;
    let alive = true;
    api.getSession()
      .then((info) => { if (alive) { setSession(info); setGate("authed"); } })
      .catch(() => { if (alive) { setSession(null); setGate("signed-out"); } });
    return () => { alive = false; };
  }, []);

  return { gate, session };
}

/**
 * Every route but Connect depends on a mailbox already being connected. A first-run visitor
 * is sent to Connect instead of the destination screen's raw error, and the check re-runs
 * whenever navigation leaves Connect so a fresh connect (or disconnect) is picked up.
 */
function useConnectionGate(pathname: string, authed: boolean): ConnectionGate {
  const [gate, setGate] = useState<ConnectionGate>("checking");
  const previousPath = useRef(pathname);

  const check = useCallback(async () => {
    try {
      const status = await api.getConnection();
      setGate(status.connected ? "connected" : "blocked");
    } catch {
      // A reachable-but-erroring API is a different problem than "no mailbox yet" - let the
      // destination screen's own error state explain it rather than bouncing to Connect.
      setGate("connected");
    }
  }, []);

  useEffect(() => { if (authed) void check(); }, [authed, check]);

  useEffect(() => {
    if (authed && previousPath.current === "/connect" && pathname !== "/connect") void check();
    previousPath.current = pathname;
  }, [pathname, authed, check]);

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

function AccountMenu({ session }: { session: SessionInfo | null }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function signOut() {
    try { await api.logout(); } catch { /* clearing the cookie client-side is enough */ }
    window.location.assign("/signin");
  }

  return (
    <div className="account-menu" ref={ref}>
      <button
        className="avatar"
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
        onClick={() => setOpen((v) => !v)}
      >
        <CircleUserRound size={20} aria-hidden="true" />
      </button>
      {open && (
        <div className="account-popover" role="menu">
          {session && (
            <div className="account-identity">
              <strong>{session.user.name || session.user.email}</strong>
              <span>{session.user.email}</span>
            </div>
          )}
          <button type="button" role="menuitem" onClick={signOut}>
            <LogOut size={15} aria-hidden="true" />Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export function AppShell() {
  const location = useLocation();
  const [dark, setDark] = useTheme();
  const { gate: authGate, session } = useAuthGate();
  const connectionGate = useConnectionGate(location.pathname, authGate === "authed");
  const title = navigation.find((item) => item.path === location.pathname)?.label ?? "Home";
  const dateLabel = useMemo(() => new Intl.DateTimeFormat("en-IN", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(new Date()).toUpperCase(), []);

  if (authGate === "checking") {
    return <div className="screen-content"><LoadingState label="Checking your session…" /></div>;
  }
  if (authGate === "signed-out") {
    return <Navigate to="/signin" replace />;
  }

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
            <AccountMenu session={session} />
          </div>
        </header>
        <main id="main-content" tabIndex={-1}>
          {location.pathname === "/connect" ? (
            <Outlet />
          ) : connectionGate === "checking" ? (
            <div className="screen-content"><LoadingState label="Checking your connection…" /></div>
          ) : connectionGate === "blocked" ? (
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
