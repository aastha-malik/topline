import { AlertTriangle, Edit3, Info, Save, Send, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";

import { api, ApiError } from "../api/client";
import type { AgentDraft, Customer, Invoice } from "../api/types";
import { daysOverdue, formatMoney, titleCase } from "../utils/format";

interface DraftCardProps {
  draft: AgentDraft;
  customer?: Customer;
  invoices: Invoice[];
  onDone: (message: string) => void;
}

export function DraftCard({ draft, customer, invoices, onDone }: DraftCardProps) {
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [subject, setSubject] = useState(draft.subject);
  const [body, setBody] = useState(draft.text_body);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"save" | "send" | "reject" | null>(null);
  const [error, setError] = useState("");

  const linkedInvoices = useMemo(() => invoices.filter((invoice) => draft.invoice_ids.includes(invoice.id)), [draft.invoice_ids, invoices]);
  const total = linkedInvoices.reduce((sum, invoice) => sum + invoice.balance_paise, 0);
  const oldest = linkedInvoices.reduce<number | null>((current, invoice) => {
    const days = daysOverdue(invoice.due_date);
    return days === null ? current : Math.max(current ?? 0, days);
  }, null);
  const invoiceNumbers = linkedInvoices.map((invoice) => invoice.invoice_number).filter(Boolean).join(", ");
  const warning = linkedInvoices.find((invoice) => invoice.payment_claim_note || invoice.dispute_note);
  const warningText = warning?.payment_claim_note || warning?.dispute_note || (draft.status === "paused" ? "This draft is paused for human review." : "");
  const toneClass = draft.tone.toLowerCase().replaceAll("_", "-").replaceAll(" ", "-");

  async function saveDraft() {
    setBusy("save");
    setError("");
    try {
      await api.updateDraft(draft.id, { subject: subject.trim(), text_body: body.trim() });
      setEditing(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The draft could not be saved.");
    } finally {
      setBusy(null);
    }
  }

  async function approveAndSend() {
    setBusy("send");
    setError("");
    let approved = false;
    try {
      if (subject !== draft.subject || body !== draft.text_body) {
        await api.updateDraft(draft.id, { subject: subject.trim(), text_body: body.trim() });
      }
      await api.approveDraft(draft.id);
      approved = true;
      await api.sendDraft(draft.id);
      onDone(`${customer?.name ?? draft.customer_email} was approved and sent.`);
    } catch (caught) {
      const detail = caught instanceof ApiError ? caught.detail || caught.message : caught instanceof Error ? caught.message : "Unknown error";
      setError(approved ? `The draft is approved, but sending failed: ${detail}` : detail);
    } finally {
      setBusy(null);
    }
  }

  async function rejectDraft() {
    setBusy("reject");
    setError("");
    try {
      await api.rejectDraft(draft.id, reason.trim());
      onDone(`${customer?.name ?? draft.customer_email}’s draft was rejected.`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The draft could not be rejected.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className={warningText ? "draft-card warning-card" : "draft-card"}>
      {warningText && (
        <div className="warning-banner">
          <AlertTriangle size={19} aria-hidden="true" />
          <strong>Needs your attention</strong>
          <span>{warningText}</span>
        </div>
      )}
      <div className="draft-meta">
        <div>
          <strong>{customer?.name ?? "Customer"}</strong>
          <span>{draft.customer_email}</span>
        </div>
        <span className={`tone-tag ${toneClass}`}>{titleCase(draft.tone)}</span>
      </div>
      <p className="invoice-context">
        {total ? formatMoney(total, linkedInvoices[0]?.currency) : "Amount unavailable"}
        {oldest !== null ? ` · ${oldest} days overdue` : ""}
        {invoiceNumbers ? ` · ${invoiceNumbers}` : ""}
      </p>
      <div className="email-sheet">
        {editing ? (
          <div className="draft-editor">
            <label>
              <span>Subject</span>
              <input value={subject} onChange={(event) => setSubject(event.target.value)} maxLength={200} required />
            </label>
            <label>
              <span>Email body</span>
              <textarea value={body} onChange={(event) => setBody(event.target.value)} rows={12} maxLength={20_000} required />
            </label>
          </div>
        ) : (
          <>
            <p><b>Subject:</b> {subject}</p>
            {body.split(/\n\s*\n/).map((paragraph, index) => <p key={`${draft.id}-${index}`}>{paragraph}</p>)}
          </>
        )}
      </div>
      <p className="reasoning"><Info size={16} aria-hidden="true" />{draft.rationale}</p>
      {error && <p className="action-error" role="alert"><AlertTriangle size={16} aria-hidden="true" />{error}</p>}
      {rejecting && (
        <div className="reject-panel">
          <label>
            <span>Reason for rejecting <small>(optional)</small></span>
            <textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={3} placeholder="What should the agent do differently?" maxLength={2_000} />
          </label>
          <div>
            <button className="danger-action" type="button" onClick={rejectDraft} disabled={busy !== null}>
              <Trash2 size={16} aria-hidden="true" />{busy === "reject" ? "Rejecting…" : "Confirm rejection"}
            </button>
            <button className="secondary-action compact-action" type="button" onClick={() => setRejecting(false)} disabled={busy !== null}>
              <X size={16} aria-hidden="true" />Cancel
            </button>
          </div>
        </div>
      )}
      <div className="draft-actions">
        <button className="primary-action" type="button" onClick={approveAndSend} disabled={busy !== null || !subject.trim() || !body.trim()}>
          {busy === "send" ? <span className="button-spinner" aria-hidden="true" /> : <Send size={16} aria-hidden="true" />}
          {busy === "send" ? "Sending…" : "Approve & send"}
        </button>
        {editing ? (
          <button className="secondary-action" type="button" onClick={saveDraft} disabled={busy !== null || !subject.trim() || !body.trim()}>
            <Save size={16} aria-hidden="true" />{busy === "save" ? "Saving…" : "Save draft"}
          </button>
        ) : (
          <button className="secondary-action" type="button" onClick={() => setEditing(true)} disabled={busy !== null}>
            <Edit3 size={16} aria-hidden="true" />Edit
          </button>
        )}
        <button className="quiet-action" type="button" onClick={() => setRejecting(!rejecting)} disabled={busy !== null} aria-expanded={rejecting}>
          {rejecting ? <X size={16} aria-hidden="true" /> : <Trash2 size={16} aria-hidden="true" />}
          {rejecting ? "Close" : "Reject"}
        </button>
      </div>
    </article>
  );
}
