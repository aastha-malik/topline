import { Link2, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { api } from "../api/client";

/**
 * The unauthenticated landing. "Continue with Google" is the only action - connecting the
 * business Gmail account is what signs the owner in (the backend mints a session in the
 * OAuth callback). Every other route redirects here until that session exists.
 */
export function SignInScreen() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function signIn() {
    setBusy(true);
    setError("");
    try {
      const auth = await api.startGoogleOauth();
      window.location.assign(auth.authorization_url);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Google sign-in could not be started.");
      setBusy(false);
    }
  }

  return (
    <div className="signin-shell">
      <main className="signin-card">
        <span className="brand-mark" aria-hidden="true">T</span>
        <h1>Sign in to Topline</h1>
        <p>
          Topline connects to your business Gmail to track who owes you money and draft the
          reminders — you approve every message before it is sent.
        </p>
        <button className="google-button" type="button" onClick={signIn} disabled={busy}>
          <Link2 size={17} aria-hidden="true" />
          {busy ? "Opening Google…" : "Continue with Google"}
        </button>
        {error && <p className="signin-error" role="alert">{error}</p>}
        <div className="signin-assurance">
          <ShieldCheck size={18} aria-hidden="true" />
          <span>Read-only mailbox access. Topline cannot move money or send without your approval.</span>
        </div>
      </main>
    </div>
  );
}
