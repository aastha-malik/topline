import type {
  ActivityLog,
  AgentDraft,
  ConnectionStatus,
  Customer,
  DailyQueue,
  DashboardData,
  Digest,
  DigestItem,
  DraftItemRequest,
  DraftUpdate,
  Invoice,
  LedgerSummary,
  SessionInfo,
  SyncRun,
} from "./types";

const now = new Date();
const isoMinutesAgo = (minutes: number) => new Date(now.getTime() - minutes * 60_000).toISOString();
const dateDaysAgo = (days: number) => new Date(now.getTime() - days * 86_400_000).toISOString().slice(0, 10);

export const demoCustomers: Customer[] = [
  { id: "customer-acme", name: "Acme Industries", primary_email: "accounts@acme.in", domain: "acme.in", phone: null, match_confidence: 0.98, match_method: "email_domain", first_seen_at: isoMinutesAgo(5000), last_seen_at: isoMinutesAgo(17) },
  { id: "customer-nova", name: "Nova Textiles", primary_email: "finance@novatex.co.in", domain: "novatex.co.in", phone: null, match_confidence: 0.96, match_method: "email_domain", first_seen_at: isoMinutesAgo(8000), last_seen_at: isoMinutesAgo(1120) },
  { id: "customer-bharat", name: "Bharat Traders", primary_email: "bharat.traders@gmail.com", domain: null, phone: null, match_confidence: 0.94, match_method: "sender", first_seen_at: isoMinutesAgo(3000), last_seen_at: isoMinutesAgo(240) },
];

export const demoInvoices: Invoice[] = [
  { id: "invoice-acme", customer_id: "customer-acme", invoice_number: "INV-1043", amount_paise: 4_000_000, amount_paid_paise: 0, balance_paise: 4_000_000, currency: "INR", issued_date: dateDaysAgo(62), due_date: dateDaysAgo(32), due_date_inferred: false, payment_state: "payment_claimed", reminder_state: "paused", effective_state: "payment_claimed", state_reason: "Customer reply claims payment on 03 Aug", evidence_strength: "medium", confidence: 0.91, missing_fields: [], dispute_note: null, payment_claim_note: "Customer says payment was made on 03 Aug. Check Razorpay before sending.", razorpay_invoice_id: null, razorpay_payment_id: null, reminder_count: 1, last_reminder_at: isoMinutesAgo(1440), created_at: isoMinutesAgo(6000), updated_at: isoMinutesAgo(17) },
  { id: "invoice-nova", customer_id: "customer-nova", invoice_number: "INV-0987", amount_paise: 2_500_000, amount_paid_paise: 2_000_000, balance_paise: 500_000, currency: "INR", issued_date: dateDaysAgo(91), due_date: dateDaysAgo(61), due_date_inferred: false, payment_state: "partial", reminder_state: "eligible", effective_state: "partially_paid", state_reason: "Partial payment confirmed", evidence_strength: "strong", confidence: 0.98, missing_fields: [], dispute_note: null, payment_claim_note: null, razorpay_invoice_id: "inv_demo_nova", razorpay_payment_id: null, reminder_count: 2, last_reminder_at: isoMinutesAgo(4320), created_at: isoMinutesAgo(9000), updated_at: isoMinutesAgo(240) },
  { id: "invoice-bharat", customer_id: "customer-bharat", invoice_number: "INV-1051", amount_paise: 1_200_000, amount_paid_paise: 0, balance_paise: 1_200_000, currency: "INR", issued_date: dateDaysAgo(38), due_date: dateDaysAgo(8), due_date_inferred: false, payment_state: "unpaid", reminder_state: "eligible", effective_state: "unpaid", state_reason: "Due date passed", evidence_strength: "strong", confidence: 0.96, missing_fields: [], dispute_note: null, payment_claim_note: null, razorpay_invoice_id: null, razorpay_payment_id: null, reminder_count: 0, last_reminder_at: null, created_at: isoMinutesAgo(3500), updated_at: isoMinutesAgo(240) },
];

let demoDrafts: AgentDraft[] = [
  { id: "draft-acme", owner_id: "owner-demo", digest_id: "digest-demo", customer_id: "customer-acme", customer_email: "accounts@acme.in", invoice_ids: ["invoice-acme"], subject: "Payment overdue: Invoice INV-1043", text_body: "Hi Acme team,\n\nThis is a reminder that payment of ₹40,000 for invoice INV-1043 is now 32 days overdue.\n\nPlease arrange payment at the earliest, or share the payment reference if it has already been completed so we can update our records.\n\nRegards,\nAastha", rationale: "You asked for a firm tone. This is the second reminder.", tone: "firm", status: "paused", source_snapshot: {}, agent_decision: {}, customer_thread_id: "thread-acme", approved_at: null, sent_at: null, created_at: isoMinutesAgo(42), updated_at: isoMinutesAgo(17) },
  { id: "draft-nova", owner_id: "owner-demo", digest_id: "digest-demo", customer_id: "customer-nova", customer_email: "finance@novatex.co.in", invoice_ids: ["invoice-nova"], subject: "Final notice: Invoice INV-0987 remains unpaid", text_body: "Dear Nova Textiles team,\n\nDespite our previous reminders, the remaining payment of ₹5,000 for invoice INV-0987 is now 61 days overdue.\n\nPlease arrange payment within three business days or contact us if there is an issue preventing settlement.\n\nRegards,\nAastha", rationale: "Two earlier reminders received no response. The confirmed partial payment is reflected.", tone: "final", status: "pending", source_snapshot: {}, agent_decision: {}, customer_thread_id: "thread-nova", approved_at: null, sent_at: null, created_at: isoMinutesAgo(43), updated_at: isoMinutesAgo(43) },
];

const demoDigest: Digest = {
  id: "digest-demo", owner_id: "owner-demo", run_date: dateDaysAgo(0), status: "building",
  gmail_thread_id: null, owner_message_id: null, total_outstanding_paise: 1_700_000,
  customer_count: 2, created_at: isoMinutesAgo(95),
};

let demoQueueItems: DigestItem[] = [
  { id: "item-bharat", digest_id: "digest-demo", item_number: 1, customer_id: "customer-bharat", customer_name: "Bharat Traders", invoice_ids: ["invoice-bharat"], amount_paise: 1_200_000, oldest_due_date: dateDaysAgo(8), recommendation_reason: "Invoice INV-1051 has ₹12,000 outstanding; the oldest due date is 8 days overdue. Payment is not confirmed and no payment claim or dispute blocks follow-up. No prior reminder is recorded.", source_references: [], status: "actionable" },
  { id: "item-nova", digest_id: "digest-demo", item_number: 2, customer_id: "customer-nova", customer_name: "Nova Textiles", invoice_ids: ["invoice-nova"], amount_paise: 500_000, oldest_due_date: dateDaysAgo(61), recommendation_reason: "Invoice INV-0987 has ₹5,000 outstanding; the oldest due date is 61 days overdue. 2 prior reminder(s) are recorded.", source_references: [], status: "drafted" },
];

const demoActivity: ActivityLog[] = [
  { id: "activity-1", event_type: "customer.payment_claimed", actor_type: "customer", actor_id: null, entity_type: "invoice", entity_id: "invoice-acme", summary: "Acme’s reply says payment was made. Topline paused the draft for your review.", decision: {}, source_evidence: [], occurred_at: isoMinutesAgo(17) },
  { id: "activity-2", event_type: "agent.drafts_created", actor_type: "agent", actor_id: null, entity_type: "digest", entity_id: "digest-demo", summary: "Created three reminder drafts from today’s digest.", decision: {}, source_evidence: [], occurred_at: isoMinutesAgo(38) },
  { id: "activity-3", event_type: "digest.sent", actor_type: "agent", actor_id: null, entity_type: "digest", entity_id: "digest-demo", summary: "Sent your daily digest: three open invoices and ₹57,000 outstanding.", decision: {}, source_evidence: [], occurred_at: isoMinutesAgo(78) },
  { id: "activity-4", event_type: "sync.incremental_completed", actor_type: "system", actor_id: null, entity_type: "sync_run", entity_id: "sync-demo", summary: "Checked Gmail for invoice and payment evidence. One customer reply was new.", decision: {}, source_evidence: [], occurred_at: isoMinutesAgo(82) },
];

const demoSyncRuns: SyncRun[] = [
  { id: "sync-demo", mode: "incremental", status: "completed", started_at: isoMinutesAgo(84), finished_at: isoMinutesAgo(82), messages_listed: 7, messages_content_fetched: 2, messages_ignored: 5, attachments_processed: 0, invoices_upserted: 1, history_fallback_used: false, error: null },
];

const wait = () => new Promise((resolve) => window.setTimeout(resolve, 120));

export const demoApi = {
  async getDashboard(): Promise<DashboardData> {
    await wait();
    const summary: LedgerSummary = { total_outstanding_paise: 5_700_000, customer_count: 3, invoice_count: 3, by_state: { unpaid: 1, partially_paid: 1, payment_claimed: 1 } };
    return { summary, customers: demoCustomers, invoices: demoInvoices, drafts: [...demoDrafts], activity: demoActivity, syncRuns: demoSyncRuns };
  },
  async listCustomers() { await wait(); return demoCustomers; },
  async listInvoices() { await wait(); return demoInvoices; },
  async listActivity() { await wait(); return demoActivity; },
  async listDrafts() { await wait(); return [...demoDrafts]; },
  async listSyncRuns() { await wait(); return demoSyncRuns; },
  async buildDailyQueue(): Promise<DailyQueue> {
    await wait();
    return { digest: demoDigest, items: [...demoQueueItems], drafts: [...demoDrafts] };
  },
  async draftDigestItem(itemId: string, body: DraftItemRequest): Promise<AgentDraft> {
    await wait();
    const item = demoQueueItems.find((entry) => entry.id === itemId);
    if (!item) throw new Error("That queue item is no longer available.");
    const customer = demoCustomers.find((entry) => entry.id === item.customer_id);
    const amount = `₹${(item.amount_paise / 100).toLocaleString("en-IN")}`;
    const draft: AgentDraft = {
      id: `draft-${item.customer_id}`, owner_id: "owner-demo", digest_id: "digest-demo",
      customer_id: item.customer_id, customer_email: customer?.primary_email ?? "",
      invoice_ids: item.invoice_ids,
      subject: "A reminder about your outstanding balance",
      text_body: `Hi ${customer?.name ?? "there"},\n\nThis is a ${body.tone} reminder that ${amount} remains outstanding. Could you let us know when we can expect payment?${body.note ? `\n\n${body.note}` : ""}\n\nThank you,\nAastha`,
      rationale: `You asked for a ${body.tone} tone. The draft uses only ${customer?.name ?? "this customer"}'s own invoice evidence.`,
      tone: body.tone, status: "pending", source_snapshot: {}, agent_decision: {},
      customer_thread_id: null, approved_at: null, sent_at: null,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    };
    demoDrafts = [...demoDrafts.filter((entry) => entry.id !== draft.id), draft];
    demoQueueItems = demoQueueItems.map((entry) => entry.id === itemId ? { ...entry, status: "drafted" } : entry);
    return draft;
  },
  async getConnection(): Promise<ConnectionStatus> { await wait(); return { connected: true, google_oauth_configured: true, razorpay_configured: true, accounts: [{ id: "gmail-demo", email_address: "aastha@topline.in", status: "connected", granted_scopes: ["gmail.readonly", "gmail.send"], backfill_status: "completed", last_backfill_at: isoMinutesAgo(10000), last_incremental_sync_at: isoMinutesAgo(82), connected_at: isoMinutesAgo(12000) }] }; },
  async getSession(): Promise<SessionInfo> { await wait(); return { authenticated: true, user: { id: "owner-demo", email: "aastha@topline.in", name: "Aastha" }, workspace: { id: "workspace-demo", business_name: "Topline (demo)" } }; },
  async logout() { await wait(); },
  async updateDraft(id: string, value: DraftUpdate) { await wait(); demoDrafts = demoDrafts.map((draft) => draft.id === id ? { ...draft, ...value, updated_at: new Date().toISOString() } : draft); return demoDrafts.find((draft) => draft.id === id)!; },
  async approveDraft(id: string) { await wait(); demoDrafts = demoDrafts.map((draft) => draft.id === id ? { ...draft, status: "approved", approved_at: new Date().toISOString() } : draft); return demoDrafts.find((draft) => draft.id === id)!; },
  async sendDraft(id: string) { await wait(); demoDrafts = demoDrafts.map((draft) => draft.id === id ? { ...draft, status: "sent", sent_at: new Date().toISOString() } : draft); return demoDrafts.find((draft) => draft.id === id)!; },
  async rejectDraft(id: string, reason?: string) { void reason; await wait(); demoDrafts = demoDrafts.map((draft) => draft.id === id ? { ...draft, status: "rejected" } : draft); return demoDrafts.find((draft) => draft.id === id)!; },
  async runIncrementalSync() { await wait(); return { status: "completed" }; },
  async runBackfill(accountId?: string) { void accountId; await wait(); return { status: "completed", invoices_created: 3 }; },
  async reconcileRazorpay() { await wait(); return { matched: 1, unmatched: 0 }; },
  async startGoogleOauth() { await wait(); return { authorization_url: "/connect?connected=demo%40topline.in", state: "demo", scopes: ["gmail.readonly", "gmail.send"] }; },
  async disconnectGmail(accountId?: string) { void accountId; await wait(); },
};
