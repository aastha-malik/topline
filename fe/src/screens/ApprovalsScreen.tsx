import { Check, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import type { AgentDraft, Customer, Invoice } from "../api/types";
import { DraftCard } from "../components/DraftCard";
import { ErrorState, LoadingState } from "../components/FeedbackState";

export function ApprovalsScreen() {
  const [drafts, setDrafts] = useState<AgentDraft[] | null>(null);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const [nextDrafts, nextCustomers, nextInvoices] = await Promise.all([api.listDrafts(), api.listCustomers(), api.listInvoices()]);
      setDrafts(nextDrafts.filter((draft) => ["pending", "paused", "approved", "failed"].includes(draft.status)));
      setCustomers(nextCustomers);
      setInvoices(nextInvoices);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The approval queue could not be loaded.");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function handleDone(message: string) {
    setNotice(message);
    void load();
  }

  if (!drafts && !error) return <div className="screen-content"><LoadingState label="Opening the approval queue…" /></div>;
  if (!drafts) return <div className="screen-content"><ErrorState message={error} onRetry={() => void load()} /></div>;

  return (
    <div className="screen-content approvals-screen">
      <div className="screen-intro">
        <div><h2>Approval queue</h2><p>Read every message in full. Topline approves first and sends only after that approval succeeds.</p></div>
        <span className="safety-chip"><ShieldCheck size={16} aria-hidden="true" />Approval required</span>
      </div>
      {notice && <div className="inline-notice" role="status"><span><Check size={16} aria-hidden="true" />{notice}</span><button type="button" onClick={() => setNotice("")}>Dismiss</button></div>}
      <div className="draft-stack">
        {drafts.map((draft) => (
          <DraftCard
            key={draft.id}
            draft={draft}
            customer={customers.find((customer) => customer.id === draft.customer_id)}
            invoices={invoices}
            onDone={handleDone}
          />
        ))}
        {drafts.length === 0 && (
          <div className="empty-state">
            <span className="empty-seal" aria-hidden="true"><Check size={27} /></span>
            <h2>Nothing waiting.</h2>
            <p>The agent will bring you a complete draft when there’s something safe to review.</p>
            <small>Topline will keep checking in the background.</small>
          </div>
        )}
      </div>
    </div>
  );
}
