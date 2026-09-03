import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getSession, getConnection } = vi.hoisted(() => ({
  getSession: vi.fn(),
  getConnection: vi.fn(),
}));

vi.mock("./api/client", () => ({
  api: { getSession, getConnection, startGoogleOauth: vi.fn(), logout: vi.fn() },
  isDemoMode: false,
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status = 0) {
      super(message);
      this.status = status;
    }
  },
}));

import App from "./App";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
});

describe("authentication gate", () => {
  beforeEach(() => {
    getSession.mockReset();
    getConnection.mockReset();
  });

  it("redirects to the sign-in screen when there is no session", async () => {
    getSession.mockRejectedValue(new Error("401"));
    render(<MemoryRouter initialEntries={["/"]}><App /></MemoryRouter>);
    expect(await screen.findByText("Sign in to Topline")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Continue with Google/ })).toBeInTheDocument();
  });

  it("renders the app shell once a session resolves", async () => {
    getSession.mockResolvedValue({
      authenticated: true,
      user: { id: "u1", email: "nina@northwind.in", name: "Nina" },
      workspace: { id: "w1", business_name: "Northwind" },
    });
    getConnection.mockResolvedValue({ connected: true, google_oauth_configured: true, razorpay_configured: false, accounts: [] });
    render(<MemoryRouter initialEntries={["/"]}><App /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("You stay in control")).toBeInTheDocument());
    expect(screen.queryByText("Sign in to Topline")).not.toBeInTheDocument();
  });
});
