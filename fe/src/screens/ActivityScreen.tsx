import { Activity, AlertTriangle, Check, CircleDot } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import type { ActivityLog } from "../api/types";
import { ErrorState, LoadingState } from "../components/FeedbackState";
import { formatDateTime, formatTime, titleCase } from "../utils/format";

export function ActivityScreen() {
  const [events, setEvents] = useState<ActivityLog[] | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    setError("");
    try { setEvents(await api.listActivity()); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "The activity record could not be loaded."); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  if (!events && !error) return <div className="screen-content"><LoadingState label="Reading the audit trail…" /></div>;
  if (!events) return <div className="screen-content"><ErrorState message={error} onRetry={() => void load()} /></div>;

  return (
    <div className="screen-content activity-screen">
      <div className="screen-intro">
        <div><h2>Activity</h2><p>A time-stamped record of what Topline observed, decided, and changed.</p></div>
        <span className="live-label"><Activity size={15} aria-hidden="true" />Audit trail</span>
      </div>
      <ol className="timeline">
        {events.map((event, index) => {
          const attention = /claim|dispute|fail|pause|review|unsafe/i.test(`${event.event_type} ${event.summary}`);
          return (
            <li className={attention ? "attention-event" : ""} key={event.id}>
              <time dateTime={event.occurred_at}><strong>{formatTime(event.occurred_at)}</strong><span>{formatDateTime(event.occurred_at, "date")}</span></time>
              <span className="timeline-node" aria-hidden="true">{attention ? <AlertTriangle size={12} /> : index === 0 ? <CircleDot size={12} /> : <Check size={12} />}</span>
              <div><strong>{titleCase(event.event_type)}</strong><p>{event.summary || "The event was recorded without a summary."}</p></div>
            </li>
          );
        })}
      </ol>
      {events.length === 0 && <div className="empty-state compact-empty"><Check size={24} aria-hidden="true" /><h2>No activity yet.</h2><p>Connection, sync, approval, and sending events will appear here.</p></div>}
    </div>
  );
}
