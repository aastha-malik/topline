import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("session token handoff", () => {
  it("captures the token from the URL fragment, strips it, and sends it as a bearer", async () => {
    window.history.replaceState(null, "", "/connect?connected=me%40x.in#s=tok-abc123");
    vi.resetModules();
    const { api: freshApi } = await import("./client");

    expect(window.localStorage.getItem("topline_session")).toBe("tok-abc123");
    expect(window.location.hash).toBe("");
    expect(window.location.search).toBe("?connected=me%40x.in");

    let seen: Headers | undefined;
    vi.stubGlobal("fetch", vi.fn(async (_input: unknown, init: RequestInit) => {
      seen = init.headers as Headers;
      return jsonResponse({ authenticated: true });
    }));
    await freshApi.getSession();

    expect(seen?.get("Authorization")).toBe("Bearer tok-abc123");
  });

  it("drops the stored token on a 401", async () => {
    window.localStorage.setItem("topline_session", "stale");
    vi.resetModules();
    const { api: freshApi, ApiError } = await import("./client");

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "nope" }, 401)));
    await expect(freshApi.listInvoices()).rejects.toBeInstanceOf(ApiError);
    expect(window.localStorage.getItem("topline_session")).toBeNull();
  });
});

describe("live API transport", () => {
  it("loads the dashboard from the six backend read models", async () => {
    const responses = new Map<string, unknown>([
      ["/api/v1/ledger/summary", { total_outstanding_paise: 0, customer_count: 0, invoice_count: 0, by_state: {} }],
      ["/api/v1/customers", []],
      ["/api/v1/invoices", []],
      ["/api/v1/agent/drafts", []],
      ["/api/v1/activity?limit=30", []],
      ["/api/v1/sync/runs?limit=10", []],
    ]);
    const fetchMock = vi.fn(async (input: string | URL | Request) => jsonResponse(responses.get(String(input))));
    vi.stubGlobal("fetch", fetchMock);

    const dashboard = await api.getDashboard();

    expect(dashboard.summary.total_outstanding_paise).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(6);
    expect(fetchMock.mock.calls.map(([url]) => String(url)).sort()).toEqual([...responses.keys()].sort());
  });

  it("sends the explicit Gmail backfill contract", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: "completed" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.runBackfill("account-123");

    expect(fetchMock).toHaveBeenCalledWith("/api/v1/sync/backfill", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ gmail_account_id: "account-123" }),
      credentials: "include",
    }));
  });

  it("treats a missing first owner as a disconnected workspace", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "No owner found" }, 404)));

    await expect(api.getConnection()).resolves.toMatchObject({ connected: false, accounts: [] });
  });
});
