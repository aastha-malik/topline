import { AlertTriangle, LoaderCircle, RefreshCw } from "lucide-react";

export function LoadingState({ label = "Opening the ledger…" }: { label?: string }) {
  return (
    <div className="feedback-state" role="status" aria-live="polite">
      <LoaderCircle className="feedback-spinner" aria-hidden="true" />
      <div>
        <strong>{label}</strong>
        <p>Topline is gathering the latest evidence.</p>
      </div>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="feedback-state feedback-error" role="alert">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>We couldn’t open this record.</strong>
        <p>{message}</p>
      </div>
      <button type="button" className="secondary-action compact-action" onClick={onRetry}>
        <RefreshCw size={15} aria-hidden="true" />
        Try again
      </button>
    </div>
  );
}
