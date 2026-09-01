import { AlertTriangle, ArrowRight, Check, RefreshCw, Send, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import type { DashboardData, Invoice } from "../api/types";
import { ErrorState, LoadingState } from "../components/FeedbackState";
import { formatDateTime, formatMoney, formatTime } from "../utils/format";

export function HomeScreen() {
  const navigate = useNavigate();
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try { setData(await api.getDashboard()); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "The dashboard could not be loaded."); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const pendingDrafts = data?.drafts.filter((draft) => ["pending", "paused", "approved", "failed"].includes(draft.status)) ?? [];
  const openInvoices = data?.invoices.filter((invoice) => invoice.balance_paise > 0 && invoice.payment_state !== "confirmed_paid") ?? [];
  const attentionInvoice = useMemo<Invoice | undefined>(() => data?.invoices.find((invoice) => invoice.payment_claim_note || invoice.dispute_note || ["payment_claimed", "disputed"].includes(invoice.effective_state)), [data]);
  const attentionCustomer = data?.customers.find((customer) => customer.id === attentionInvoice?.customer_id);
  const latestSync = data?.syncRuns[0];

  if (!data && !error) return <div className="screen-content"><LoadingState label="Balancing today’s ledger…" /></div>;
  if (!data) return <div className="screen-content"><ErrorState message={error} onRetry={() => void load()} /></div>;

  return (
    <div className="screen-content home-screen">
      <section className="stat-grid" aria-label="Outstanding invoice summary">
        <article className="stat-block">
          <p>Total outstanding</p>
          <strong>{formatMoney(data.summary.total_outstanding_paise)}</strong>
          <span>Across {data.summary.customer_count} {data.summary.customer_count === 1 ? "customer" : "customers"}</span>
        </article>
        <article className="stat-block">
          <p>Open invoices</p>
          <strong>{openInvoices.length}</strong>
          <span>{attentionInvoice ? "One claim needs checking" : "No payment claims waiting"}</span>
        </article>
        <button className="stat-block approval-stat" type="button" onClick={() => navigate("/approvals")}>
          <span className="stat-label">Waiting for your approval</span>
          <strong>{pendingDrafts.length}</strong>
          <span className="stat-cta">Review full drafts <ArrowRight size={17} aria-hidden="true" /></span>
        </button>
      </section>

      <section className="today-strip" aria-label="What happened today">
        <strong>Today</strong>
        <span><Check className="status-icon done" size={16} aria-hidden="true" />{latestSync ? `Last sync ${formatTime(latestSync.finished_at ?? latestSync.started_at)}` : "No sync yet"}</span>
        <span><Send className="status-icon ready" size={16} aria-hidden="true" />{pendingDrafts.length} drafts waiting</span>
        <span><Sparkles className="status-icon reply" size={16} aria-hidden="true" />{data.activity[0]?.summary ?? "No new activity"}</span>
      </section>

      <section className="home-focus">
        <div className="section-heading">
          <div>
            <h2>{attentionInvoice ? "One thing needs you" : "Your ledger is clear"}</h2>
            <p>{attentionInvoice ? "A customer’s message conflicts with the current payment record." : "No dispute or payment claim is waiting for review."}</p>
          </div>
          <button className="text-link" type="button" onClick={() => navigate(attentionInvoice ? "/approvals" : "/activity")}>
            {attentionInvoice ? "Open approval queue" : "See activity"}<ArrowRight size={16} aria-hidden="true" />
          </button>
        </div>
        {attentionInvoice ? (
          <button className="attention-row" type="button" onClick={() => navigate("/approvals")}>
            <span className="attention-icon" aria-hidden="true"><AlertTriangle size={19} /></span>
            <span><strong>{attentionCustomer?.name ?? "Customer"}</strong><small>{formatMoney(attentionInvoice.balance_paise, attentionInvoice.currency)} · {attentionInvoice.invoice_number ?? "Invoice"}</small></span>
            <span className="attention-copy">{attentionInvoice.payment_claim_note || attentionInvoice.dispute_note || attentionInvoice.state_reason}</span>
            <ArrowRight size={18} aria-hidden="true" />
          </button>
        ) : (
          <div className="clear-row"><Check size={19} aria-hidden="true" /><span><strong>No exception needs review.</strong><small>Topline will surface the next uncertain claim here.</small></span></div>
        )}
      </section>

      <section className="agent-pulse">
        <span className="pulse-dot" aria-hidden="true" />
        <div>
          <strong>Topline is keeping watch</strong>
          <p>{latestSync ? `Last checked Gmail ${formatDateTime(latestSync.finished_at ?? latestSync.started_at)}.` : "Run the first Gmail sync from the invoice ledger."}</p>
        </div>
        <button type="button" onClick={() => navigate("/activity")}><RefreshCw size={15} aria-hidden="true" />See activity</button>
      </section>
    </div>
  );
}
