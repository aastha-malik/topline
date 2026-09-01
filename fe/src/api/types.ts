export type UUID = string;

export interface Customer {
  id: UUID;
  name: string;
  primary_email: string;
  domain: string | null;
  phone: string | null;
  match_confidence: number;
  match_method: string;
  first_seen_at: string | null;
  last_seen_at: string | null;
}

export interface Invoice {
  id: UUID;
  customer_id: UUID | null;
  invoice_number: string | null;
  amount_paise: number;
  amount_paid_paise: number;
  balance_paise: number;
  currency: string;
  issued_date: string | null;
  due_date: string | null;
  due_date_inferred: boolean;
  payment_state: string;
  reminder_state: string;
  effective_state: string;
  state_reason: string | null;
  evidence_strength: string;
  confidence: number;
  missing_fields: string[];
  dispute_note: string | null;
  payment_claim_note: string | null;
  razorpay_invoice_id: string | null;
  razorpay_payment_id: string | null;
  reminder_count: number;
  last_reminder_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface LedgerSummary {
  total_outstanding_paise: number;
  customer_count: number;
  invoice_count: number;
  by_state: Record<string, number>;
}

export interface ActivityLog {
  id: UUID;
  event_type: string;
  actor_type: string;
  actor_id: string | null;
  entity_type: string | null;
  entity_id: string | null;
  summary: string | null;
  decision: Record<string, unknown>;
  source_evidence: Array<Record<string, unknown>>;
  occurred_at: string;
}

export interface AgentDraft {
  id: UUID;
  owner_id: UUID;
  digest_id: UUID;
  customer_id: UUID;
  customer_email: string;
  invoice_ids: UUID[];
  subject: string;
  text_body: string;
  rationale: string;
  tone: string;
  status: string;
  source_snapshot: Record<string, unknown>;
  agent_decision: Record<string, unknown>;
  customer_thread_id: string | null;
  approved_at: string | null;
  sent_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GmailAccount {
  id: UUID;
  email_address: string;
  status: string;
  granted_scopes: string[];
  backfill_status: string;
  last_backfill_at: string | null;
  last_incremental_sync_at: string | null;
  connected_at: string | null;
}

export interface ConnectionStatus {
  connected: boolean;
  google_oauth_configured: boolean;
  razorpay_configured: boolean;
  accounts: GmailAccount[];
}

export interface SyncRun {
  id: UUID;
  mode: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  messages_listed: number;
  messages_content_fetched: number;
  messages_ignored: number;
  attachments_processed: number;
  invoices_upserted: number;
  history_fallback_used: boolean;
  error: string | null;
}

export interface AuthStart {
  authorization_url: string;
  state: string;
  scopes: string[];
}

export interface DraftUpdate {
  subject: string;
  text_body: string;
}

export interface DashboardData {
  summary: LedgerSummary;
  customers: Customer[];
  invoices: Invoice[];
  drafts: AgentDraft[];
  activity: ActivityLog[];
  syncRuns: SyncRun[];
}
