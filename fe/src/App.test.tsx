import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

vi.stubEnv("VITE_DEMO_MODE", "true");
vi.mock("./api/client", async () => {
  const { demoApi } = await import("./api/demo");
  return {
    api: demoApi,
    isDemoMode: true,
    ApiError: class ApiError extends Error {},
  };
});

import App from "./App";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

describe("Topline app shell", () => {
  it("renders the responsive navigation and product promise", async () => {
    render(<MemoryRouter initialEntries={["/"]}><App /></MemoryRouter>);
    expect(await screen.findByText("Total outstanding")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Topline home" })).toHaveLength(2);
    expect(screen.getByText("You stay in control")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Approvals" }).length).toBeGreaterThan(0);
  });
});
