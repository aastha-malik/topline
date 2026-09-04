import { AlertTriangle, Check, FileText, Info, PenLine, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { DailyQueue, DigestItem } from "../api/types";
import { ErrorState, LoadingState } from "../components/FeedbackState";
import { daysOverdue, formatMoney, titleCase } from "../utils/format";

const TONES = ["polite", "normal", "firm", "final"] as const;

export function DailyQueueScreen() {
  const [queue, setQueue] = useState<DailyQueue | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      setQueue(await api.buildDailyQueue());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The daily queue could not be loaded.");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function handleDrafted(name: string) {
    setNotice(`A draft for ${name} is ready in the approval queue.`);
    void load();
  }

  if (!queue && !error) return <div className="screen-content"><LoadingState label="Building today’s reminder queue…" /></div>;
  if (!queue) return <div className="screen-content"><ErrorState message={error} onRetry={() => void load()} /></div>;

  return (
    <div className="screen-content queue-screen">
      <div className="screen-intro">
        <div>
          <h2>Daily queue</h2>
          <p>Invoices the rulebook flagged as due for a reminder today. Pick a tone and Topline drafts the email — nothing is sent until you approve it.</p>
        </div>
        <span className="safety-chip"><ShieldCheck size={16} aria-hidden="true" />Draft only</span>
      </div>

      {notice && (
        <div className="inline-notice" role="status">
          <span><Check size={16} aria-hidden="true" />{notice}</span>
          <button type="button" onClick={() => setNotice("")}>Dismiss</button>
        </div>
      )}

      <div className="queue-list">
        {queue.items.map((item) => (
          <QueueRow key={item.id} item={item} onDrafted={handleDrafted} />
        ))}
        {queue.items.length === 0 && (
          <div className="empty-state">
            <span className="empty-seal" aria-hidden="true"><Check size={27} /></span>
            <h2>Nothing due today.</h2>
            <p>No invoice currently passes Topline’s deterministic reminder checks. Paused, disputed, and payment-claimed invoices stay out of this queue.</p>
            <small>Topline re-checks each time you open this screen.</small>
          </div>
        )}
      </div>
    </div>
  );
}

function QueueRow({ item, onDrafted }: { item: DigestItem; onDrafted: (name: string) => void }) {
  const [tone, setTone] = useState<string>("normal");
  const [note, setNote] = useState("");
  const [showNote, setShowNote] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const days = daysOverdue(item.oldest_due_date);
  const invoiceCount = item.invoice_ids.length;
  const drafted = item.status === "drafted";
  const blocked = item.status === "skipped" || item.status === "paused";

  async function draft() {
    setBusy(true);
    setError("");
    try {
      await api.draftDigestItem(item.id, { tone, note: note.trim() || null });
      onDrafted(item.customer_name);
    } catch (caught) {
      const detail = caught instanceof ApiError ? caught.detail || caught.message : caught instanceof Error ? caught.message : "The draft could not be prepared.";
      setError(detail);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className={blocked ? "queue-row is-blocked" : "queue-row"}>
      <div className="queue-head">
        <div>
          <strong>{item.customer_name}</strong>
          <span>
            {formatMoney(item.amount_paise)}
            {days !== null ? ` · ${days} ${days === 1 ? "day" : "days"} overdue` : ""}
            {` · ${invoiceCount} invoice${invoiceCount === 1 ? "" : "s"}`}
          </span>
        </div>
        {drafted ? (
          <span className="queue-status drafted"><FileText size={13} aria-hidden="true" />Drafted</span>
        ) : blocked ? (
          <span className="queue-status blocked"><AlertTriangle size={13} aria-hidden="true" />{titleCase(item.status)}</span>
        ) : (
          <span className="queue-status ready">Ready</span>
        )}
      </div>

      <p className="queue-reason"><Info size={15} aria-hidden="true" />{item.recommendation_reason}</p>

      {drafted ? (
        <div className="queue-actions">
          <Link className="secondary-action" to="/approvals">Review the draft</Link>
          <button className="quiet-action neutral" type="button" onClick={() => void draft()} disabled={busy}>
            {busy ? "Redrafting…" : "Redraft"}
          </button>
        </div>
      ) : blocked ? (
        <p className="queue-blocked-note">Follow-up is paused for this customer. Clear it from the approval queue first.</p>
      ) : (
        <div className="queue-compose">
          <div className="tone-picker" role="group" aria-label={`Tone for ${item.customer_name}`}>
            {TONES.map((option) => (
              <button
                key={option}
                type="button"
                className={tone === option ? "tone-option active" : "tone-option"}
                aria-pressed={tone === option}
                onClick={() => setTone(option)}
              >
                {titleCase(option)}
              </button>
            ))}
          </div>
          {showNote ? (
            <label className="queue-note">
              <span>Note for the draft <small>(optional)</small></span>
              <textarea
                value={note}
                onChange={(event) => setNote(event.target.value)}
                rows={2}
                maxLength={2_000}
                placeholder="e.g. mention the PO number, keep it short"
              />
            </label>
          ) : (
            <button className="note-toggle" type="button" onClick={() => setShowNote(true)}>
              <PenLine size={14} aria-hidden="true" />Add a note
            </button>
          )}
          {error && <p className="action-error" role="alert"><AlertTriangle size={16} aria-hidden="true" />{error}</p>}
          <div className="queue-actions">
            <button className="primary-action" type="button" onClick={() => void draft()} disabled={busy}>
              {busy ? <span className="button-spinner" aria-hidden="true" /> : null}
              {busy ? "Drafting…" : "Draft reminder"}
            </button>
          </div>
        </div>
      )}
    </article>
  );
}
