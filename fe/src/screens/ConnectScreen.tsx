import { AlertTriangle, Check, KeyRound, Link2, Mail, RefreshCw, ShieldCheck, Unplug } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { ConnectionStatus } from "../api/types";
import { ErrorState, LoadingState } from "../components/FeedbackState";
import { formatDateTime } from "../utils/format";

export function ConnectScreen() {
  const [searchParams] = useSearchParams();
  const [connection, setConnection] = useState<ConnectionStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"gmail" | "disconnect" | "backfill" | "razorpay" | null>(null);
  const [notice, setNotice] = useState(searchParams.get("connected") ? `Connected ${searchParams.get("connected")}.` : "");

  const load = useCallback(async () => {
    setError("");
    try { setConnection(await api.getConnection()); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Connection status could not be loaded."); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function connectGmail() {
    setBusy("gmail");
    setNotice("");
    try {
      const auth = await api.startGoogleOauth();
      window.location.assign(auth.authorization_url);
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : "Google connection could not be started.");
      setBusy(null);
    }
  }

  async function disconnect(accountId: string) {
    setBusy("disconnect");
    setNotice("");
    try {
      await api.disconnectGmail(accountId);
      setNotice("Gmail was disconnected. Existing ledger evidence was retained.");
      await load();
    } catch (caught) { setNotice(caught instanceof Error ? caught.message : "Gmail could not be disconnected."); }
    finally { setBusy(null); }
  }

  async function backfill(accountId: string) {
    setBusy("backfill");
    setNotice("");
    try {
      await api.runBackfill(accountId);
      setNotice("Gmail invoice history has been imported.");
      await load();
    } catch (caught) { setNotice(caught instanceof Error ? caught.message : "Gmail history could not be imported."); }
    finally { setBusy(null); }
  }

  async function reconcile() {
    setBusy("razorpay");
    setNotice("");
    try { await api.reconcileRazorpay(); setNotice("Razorpay payment evidence has been reconciled."); }
    catch (caught) { setNotice(caught instanceof Error ? caught.message : "Razorpay reconciliation did not finish."); }
    finally { setBusy(null); }
  }

  if (!connection && !error) return <div className="screen-content"><LoadingState label="Checking secure connections…" /></div>;
  if (!connection) return <div className="screen-content"><ErrorState message={error} onRetry={() => void load()} /></div>;

  const account = connection.accounts.find((item) => item.status === "connected") ?? connection.accounts[0];

  return (
    <div className="screen-content connect-screen">
      <div className="screen-intro"><div><h2>Connect Topline</h2><p>Gmail supplies approved invoice evidence. Razorpay confirms payment without giving Topline authority to move money.</p></div></div>
      {notice && <div className={/could not|failed|did not|unavailable/i.test(notice) ? "inline-notice notice-error" : "inline-notice"} role="status"><span>{/could not|failed|did not|unavailable/i.test(notice) ? <AlertTriangle size={16} aria-hidden="true" /> : <Check size={16} aria-hidden="true" />}{notice}</span><button type="button" onClick={() => setNotice("")}>Dismiss</button></div>}
      <div className="connection-grid">
        <section className="connection-card">
          <div className="connection-heading"><span className="service-mark google-mark" aria-hidden="true"><Mail size={22} /></span><div><h2>Gmail</h2><p>Read finance-relevant threads and send only owner-approved reminders.</p></div></div>
          {account?.status === "connected" ? (
            <>
              <div className="connected-row">
                <span className="status-check" aria-hidden="true"><Check size={17} /></span>
                <div><strong>Connected</strong><p>{account.email_address}</p><small>Last checked {formatDateTime(account.last_incremental_sync_at)}</small></div>
                <button type="button" onClick={() => void disconnect(account.id)} disabled={busy !== null}><Unplug size={15} aria-hidden="true" />{busy === "disconnect" ? "Disconnecting…" : "Disconnect"}</button>
              </div>
              {account.backfill_status !== "completed" && (
                <div className="connection-followup">
                  <div><strong>Import invoice history</strong><p>This explicit first scan uses the server’s limited history window.</p></div>
                  <button className="primary-action" type="button" onClick={() => void backfill(account.id)} disabled={busy !== null}>
                    <RefreshCw className={busy === "backfill" ? "spin" : ""} size={16} aria-hidden="true" />{busy === "backfill" ? "Importing…" : "Import history"}
                  </button>
                </div>
              )}
            </>
          ) : (
            <div className="connection-action">
              <p>{connection.google_oauth_configured ? "Google is ready for a secure consent flow." : "Google OAuth is not configured on the server yet."}</p>
              <button className="google-button" type="button" onClick={connectGmail} disabled={!connection.google_oauth_configured || busy !== null}>
                <Link2 size={17} aria-hidden="true" />{busy === "gmail" ? "Opening Google…" : "Continue with Google"}
              </button>
            </div>
          )}
        </section>

        <section className="connection-card">
          <div className="connection-heading"><span className="service-mark razorpay-mark" aria-hidden="true"><KeyRound size={22} /></span><div><h2>Razorpay</h2><p>Confirm payment events and reconcile claims against provider evidence.</p></div></div>
          <div className="server-connection">
            <span className={connection.razorpay_configured ? "status-check" : "status-pending"} aria-hidden="true">{connection.razorpay_configured ? <Check size={17} /> : <AlertTriangle size={16} />}</span>
            <div><strong>{connection.razorpay_configured ? "Configured on the server" : "Server setup required"}</strong><p>{connection.razorpay_configured ? "Keys remain outside the browser." : "Add test-mode credentials to the backend environment."}</p></div>
          </div>
          <button className="secondary-action" type="button" onClick={reconcile} disabled={!connection.razorpay_configured || busy !== null}>
            <RefreshCw className={busy === "razorpay" ? "spin" : ""} size={16} aria-hidden="true" />{busy === "razorpay" ? "Reconciling…" : "Reconcile payments"}
          </button>
        </section>
      </div>
      <section className="access-note">
        <span className="shield-shape" aria-hidden="true"><ShieldCheck size={23} /></span>
        <div><h2>Your approval is the final step</h2><p>Topline reads only the evidence needed for invoice follow-up. It cannot move money. A customer email is sent only after you approve the exact subject and body shown in the queue.</p></div>
      </section>
    </div>
  );
}
