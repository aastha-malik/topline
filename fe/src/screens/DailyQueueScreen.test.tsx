import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

vi.stubEnv("VITE_DEMO_MODE", "true");
vi.mock("../api/client", async () => {
  const { demoApi } = await import("../api/demo");
  return { api: demoApi, isDemoMode: true, ApiError: class ApiError extends Error {} };
});

import { DailyQueueScreen } from "./DailyQueueScreen";

describe("DailyQueueScreen", () => {
  it("lists the day's actionable items and drafts a reminder on request", async () => {
    render(<MemoryRouter><DailyQueueScreen /></MemoryRouter>);

    const bharat = await screen.findByText("Bharat Traders");
    const row = bharat.closest("article")!;
    expect(within(row).getByText("Ready")).toBeInTheDocument();

    await userEvent.click(within(row).getByRole("button", { name: "Firm" }));
    await userEvent.click(within(row).getByRole("button", { name: "Draft reminder" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("A draft for Bharat Traders is ready"),
    );
    // The item flips to "Drafted" once the reload completes.
    await waitFor(() => expect(within(row).getByText("Drafted")).toBeInTheDocument());
  });

  it("does not offer a draft action for an already-drafted item", async () => {
    render(<MemoryRouter><DailyQueueScreen /></MemoryRouter>);

    const nova = await screen.findByText("Nova Textiles");
    const row = nova.closest("article")!;
    expect(within(row).getByText("Drafted")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Draft reminder" })).not.toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "Review the draft" })).toHaveAttribute("href", "/approvals");
  });
});
