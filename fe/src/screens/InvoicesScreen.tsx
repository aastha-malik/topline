import { AlertTriangle, CheckCircle2, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../api/client";
import type { Customer, Invoice, SyncRun } from "../api/types";
import { ErrorState, LoadingState } from "../components/FeedbackState";
import { daysOverdue, formatDateTime, formatMoney, titleCase } from "../utils/format";

export function InvoicesScreen() {
  const [invoices, setInvoices] = useState<Invoice[] | null>(null);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [syncRuns, setSyncRuns] = useState<SyncRun[]>([]);
  const [error, setError] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const [nextInvoices, nextCustomers, nextRuns] = await Promise.all([api.listInvoices(), api.listCustomers(), api.listSyncRuns()]);
      setInvoices(nextInvoices);
      setCustomers(nextCustomers);
      setSyncRuns(nextRuns);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "The invoice ledger could not be loaded."); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const customerMap = useMemo(() => new Map(customers.map((customer) => [customer.id, customer])), [customers]);

  async function sync() {
    setSyncing(true);
    setNotice("");
    try {
      await api.runIncrementalSync();
      setNotice("Gmail evidence is up to date.");
      await load();
    } catch (caught) { setNotice(caught instanceof Error ? caught.message : "The sync did not finish."); }
    finally { setSyncing(false); }
  }

  if (!invoices && !error) return <div className="screen-content"><LoadingState label="Opening customer records…" /></div>;
  if (!invoices) return <div className="screen-content"><ErrorState message={error} onRetry={() => void load()} /></div>;

  return (
    <div className="screen-content invoices-screen">
      <div className="screen-intro">
        <div><h2>Customers & invoices</h2><p>An evidence-backed view of open balances, due dates, and payment claims.</p></div>
        <div className="sync-control">
          <button className="secondary-action" type="button" onClick={sync} disabled={syncing}>
            <RefreshCw className={syncing ? "spin" : ""} size={16} aria-hidden="true" />{syncing ? "Checking Gmail…" : "Check for updates"}
          </button>
          <span>Last checked {formatDateTime(syncRuns[0]?.finished_at ?? syncRuns[0]?.started_at ?? null)}</span>
        </div>
      </div>
      {notice && <div className="inline-notice" role="status"><span>{notice}</span><button type="button" onClick={() => setNotice("")}>Dismiss</button></div>}
      <div className="table-shell">
        <table>
          <thead><tr><th>Customer</th><th>Email</th><th>Balance</th><th>Due</th><th>Status</th></tr></thead>
          <tbody>
            {invoices.map((invoice) => {
              const customer = invoice.customer_id ? customerMap.get(invoice.customer_id) : undefined;
              const days = daysOverdue(invoice.due_date);
              const attention = Boolean(invoice.payment_claim_note || invoice.dispute_note || ["payment_claimed", "disputed"].includes(invoice.effective_state));
              const urgency = attention ? "attention" : (days ?? 0) >= 45 ? "critical" : (days ?? 0) >= 15 ? "urgent" : "recent";
              return (
                <tr key={invoice.id}>
                  <td data-label="Customer"><strong>{customer?.name ?? "Unmatched customer"}</strong><small>{invoice.invoice_number ?? "Invoice number missing"}</small></td>
                  <td data-label="Email">{customer?.primary_email ?? "Not available"}</td>
                  <td data-label="Balance" className="amount-cell">{formatMoney(invoice.balance_paise, invoice.currency)}</td>
                  <td data-label="Due"><span className={`overdue ${urgency}`}>{days === null ? "Unknown" : days === 0 ? "Due now" : `${days} days`}<small>{attention ? "Review evidence" : urgency === "critical" ? "Very overdue" : urgency === "urgent" ? "Overdue" : "Recent"}</small></span></td>
                  <td data-label="Status"><span className={`invoice-status ${invoice.effective_state.replaceAll("_", "-")}`}>{attention ? <AlertTriangle size={13} aria-hidden="true" /> : invoice.payment_state === "confirmed_paid" ? <CheckCircle2 size={13} aria-hidden="true" /> : null}{titleCase(invoice.effective_state)}</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {invoices.length === 0 && <div className="table-empty"><CheckCircle2 size={24} aria-hidden="true" /><strong>No invoices yet.</strong><span>Connect Gmail, then run the first backfill from the API.</span></div>}
      </div>
      <p className="reference-note">Email and PDF evidence can pause follow-up, but only confirmed provider evidence marks an invoice paid.</p>
    </div>
  );
}
