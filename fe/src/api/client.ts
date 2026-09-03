import { demoApi } from "./demo";
import type {
  ActivityLog,
  AgentDraft,
  AuthStart,
  ConnectionStatus,
  Customer,
  DashboardData,
  DraftUpdate,
  Invoice,
  LedgerSummary,
  SessionInfo,
  SyncRun,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, "");

/**
 * Session token. The OAuth callback also sets an HttpOnly cookie, which is all that's
 * needed when the API is same-origin with the app. When they're on different origins
 * (a separate api host) the browser won't send that cookie, so the callback additionally
 * hands the token back in the redirect's URL fragment; we stash it and send it as a
 * bearer token. The fragment is stripped from the URL immediately.
 */
const TOKEN_KEY = "topline_session";

function readStoredToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function storeToken(token: string | null) {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private-mode / storage disabled - the cookie still covers same-origin deploys */
  }
}

function captureTokenFromUrl() {
  const match = window.location.hash.match(/[#&]s=([^&]+)/);
  if (!match) return;
  storeToken(decodeURIComponent(match[1]));
  const clean = window.location.pathname + window.location.search;
  window.history.replaceState(null, "", clean || "/");
}

captureTokenFromUrl();

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const token = readStoredToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "include" });
  } catch {
    throw new ApiError("Topline could not reach the API. Check that the backend is running, then try again.", 0);
  }

  if (!response.ok) {
    if (response.status === 401) storeToken(null);
    const body = await response.json().catch(() => ({})) as { detail?: string };
    const message =
      response.status === 401
        ? "Your session has ended. Sign in with Google to continue."
        : body.detail || `Request failed with status ${response.status}.`;
    throw new ApiError(message, response.status, body.detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function clearSession() {
  storeToken(null);
}

const liveApi = {
  async getDashboard(): Promise<DashboardData> {
    const [summary, customers, invoices, drafts, activity, syncRuns] = await Promise.all([
      request<LedgerSummary>("/ledger/summary"),
      request<Customer[]>("/customers"),
      request<Invoice[]>("/invoices"),
      request<AgentDraft[]>("/agent/drafts"),
      request<ActivityLog[]>("/activity?limit=30"),
      request<SyncRun[]>("/sync/runs?limit=10"),
    ]);
    return { summary, customers, invoices, drafts, activity, syncRuns };
  },
  getSession: () => request<SessionInfo>("/auth/session"),
  async logout() {
    try {
      await request<void>("/auth/logout", { method: "POST" });
    } finally {
      clearSession();
    }
  },
  listCustomers: () => request<Customer[]>("/customers"),
  listInvoices: () => request<Invoice[]>("/invoices"),
  listActivity: () => request<ActivityLog[]>("/activity?limit=100"),
  listDrafts: () => request<AgentDraft[]>("/agent/drafts"),
  listSyncRuns: () => request<SyncRun[]>("/sync/runs?limit=20"),
  async getConnection(): Promise<ConnectionStatus> {
    try {
      return await request<ConnectionStatus>("/auth/connection");
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        return { connected: false, google_oauth_configured: true, razorpay_configured: false, accounts: [] };
      }
      throw error;
    }
  },
  updateDraft: (id: string, value: DraftUpdate) => request<AgentDraft>(`/agent/drafts/${id}`, { method: "PATCH", body: JSON.stringify(value) }),
  approveDraft: (id: string) => request<AgentDraft>(`/agent/drafts/${id}/approve`, { method: "POST" }),
  sendDraft: (id: string) => request<AgentDraft>(`/agent/drafts/${id}/send`, { method: "POST" }),
  rejectDraft: (id: string, reason?: string) => request<AgentDraft>(`/agent/drafts/${id}/reject`, { method: "POST", body: JSON.stringify({ reason: reason || null }) }),
  runBackfill: (accountId?: string) => request<Record<string, unknown>>("/sync/backfill", { method: "POST", body: JSON.stringify({ gmail_account_id: accountId || null }) }),
  runIncrementalSync: () => request<Record<string, unknown>>("/sync/incremental", { method: "POST", body: JSON.stringify({}) }),
  reconcileRazorpay: () => request<Record<string, unknown>>("/sync/razorpay/reconcile", { method: "POST" }),
  startGoogleOauth: () => request<AuthStart>("/auth/google/start", { method: "POST" }),
  disconnectGmail: (accountId: string) => request<void>(`/auth/connection/${accountId}`, { method: "DELETE" }),
};

export const api = import.meta.env.VITE_DEMO_MODE === "true" ? demoApi : liveApi;
export const isDemoMode = import.meta.env.VITE_DEMO_MODE === "true";
