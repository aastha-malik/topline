-- 20260827230000_core_schema.sql
-- Topline: core receivables schema (workspaces, Gmail connection, source evidence, ledger).
--
-- Authoritative schema for the Supabase project. `be/app/models.py` mirrors this file and
-- `be/tests/test_schema_parity.py` fails the build if the two drift.
--
-- Tables owned by the platform/ingestion side (this file):
--   workspaces, users, gmail_accounts, oauth_states, sync_runs,
--   source_messages, source_attachments, customers, invoices,
--   payment_events, invoice_source_links, activity_log
--
-- Tables owned by the agent/approval side (later migrations, not in this file):
--   agent_digests, agent_digest_items, agent_drafts, agent_review_tasks
--
-- Money is stored in paise as BIGINT. Never float.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS oauth_states (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    state TEXT NOT NULL,
    code_verifier TEXT,
    workspace_id UUID,
    redirect_after TEXT,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    consumed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (state)
);

CREATE TABLE IF NOT EXISTS workspaces (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    business_name TEXT,
    sender_name TEXT,
    primary_color TEXT DEFAULT '#155EEF' NOT NULL,
    logo_url TEXT,
    reply_to TEXT,
    default_currency TEXT DEFAULT 'INR' NOT NULL,
    timezone TEXT DEFAULT 'Asia/Kolkata' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS users (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    email TEXT NOT NULL,
    name TEXT,
    role TEXT DEFAULT 'owner' NOT NULL,
    supabase_user_id TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_users_workspace_email UNIQUE (workspace_id, email),
    CONSTRAINT ck_users_role CHECK (role IN ('owner', 'member')),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    UNIQUE (supabase_user_id)
);

CREATE INDEX IF NOT EXISTS ix_users_email ON users (email);
CREATE INDEX IF NOT EXISTS ix_users_workspace_id ON users (workspace_id);

CREATE TABLE IF NOT EXISTS activity_log (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    owner_id UUID,
    event_type TEXT NOT NULL,
    actor_type TEXT DEFAULT 'system' NOT NULL,
    actor_id TEXT,
    entity_type TEXT,
    entity_id TEXT,
    summary TEXT,
    decision JSONB DEFAULT '{}' NOT NULL,
    source_evidence JSONB DEFAULT '[]' NOT NULL,
    model_name TEXT,
    prompt_version TEXT,
    dedupe_key TEXT,
    occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_activity_log_dedupe UNIQUE (workspace_id, dedupe_key),
    CONSTRAINT ck_activity_log_actor_type CHECK (actor_type IN ('system', 'user', 'provider')),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_activity_log_entity ON activity_log (workspace_id, entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_activity_log_occurred ON activity_log (workspace_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_activity_log_workspace_id ON activity_log (workspace_id);

CREATE TABLE IF NOT EXISTS customers (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    owner_id UUID NOT NULL,
    name TEXT NOT NULL,
    primary_email TEXT NOT NULL,
    alt_emails JSONB DEFAULT '[]' NOT NULL,
    domain TEXT,
    phone TEXT,
    match_confidence FLOAT DEFAULT '1.0' NOT NULL,
    match_method TEXT DEFAULT 'email_exact' NOT NULL,
    is_archived BOOLEAN DEFAULT false NOT NULL,
    first_seen_at TIMESTAMP WITH TIME ZONE,
    last_seen_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_customers_workspace_email UNIQUE (workspace_id, primary_email),
    CONSTRAINT ck_customers_match_method CHECK (match_method IN ('email_exact', 'domain', 'name_fuzzy', 'manual')),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_customers_domain ON customers (workspace_id, domain);
CREATE INDEX IF NOT EXISTS ix_customers_owner_id ON customers (owner_id);
CREATE INDEX IF NOT EXISTS ix_customers_workspace_id ON customers (workspace_id);

CREATE TABLE IF NOT EXISTS gmail_accounts (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,
    email_address TEXT NOT NULL,
    google_sub TEXT,
    access_token_encrypted TEXT,
    refresh_token_encrypted TEXT,
    token_expiry TIMESTAMP WITH TIME ZONE,
    granted_scopes JSONB DEFAULT '[]' NOT NULL,
    status TEXT DEFAULT 'connected' NOT NULL,
    last_history_id TEXT,
    backfill_status TEXT DEFAULT 'pending' NOT NULL,
    backfill_window_start DATE,
    backfill_window_end DATE,
    last_backfill_at TIMESTAMP WITH TIME ZONE,
    last_incremental_sync_at TIMESTAMP WITH TIME ZONE,
    connected_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_gmail_accounts_workspace_email UNIQUE (workspace_id, email_address),
    CONSTRAINT ck_gmail_accounts_status CHECK (status IN ('connected', 'needs_reauth', 'disconnected')),
    CONSTRAINT ck_gmail_accounts_backfill_status CHECK (backfill_status IN ('pending', 'running', 'completed', 'failed')),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_gmail_accounts_google_sub ON gmail_accounts (google_sub);
CREATE INDEX IF NOT EXISTS ix_gmail_accounts_user_id ON gmail_accounts (user_id);
CREATE INDEX IF NOT EXISTS ix_gmail_accounts_workspace_id ON gmail_accounts (workspace_id);

CREATE TABLE IF NOT EXISTS source_messages (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    gmail_account_id UUID NOT NULL,
    gmail_message_id TEXT NOT NULL,
    gmail_thread_id TEXT,
    gmail_history_id TEXT,
    internal_date TIMESTAMP WITH TIME ZONE,
    from_email TEXT,
    from_name TEXT,
    from_domain TEXT,
    to_emails JSONB DEFAULT '[]' NOT NULL,
    cc_emails JSONB DEFAULT '[]' NOT NULL,
    subject TEXT,
    snippet TEXT,
    body_text TEXT,
    body_hash TEXT,
    label_ids JSONB DEFAULT '[]' NOT NULL,
    has_attachments BOOLEAN DEFAULT false NOT NULL,
    direction TEXT DEFAULT 'inbound' NOT NULL,
    is_finance_relevant BOOLEAN DEFAULT false NOT NULL,
    relevance_score INTEGER DEFAULT '0' NOT NULL,
    relevance_reasons JSONB DEFAULT '[]' NOT NULL,
    processing_state TEXT DEFAULT 'metadata_only' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_source_messages_account_msg UNIQUE (gmail_account_id, gmail_message_id),
    CONSTRAINT ck_source_messages_state CHECK (processing_state IN ('metadata_only', 'fetched', 'extracted', 'ignored', 'failed')),
    CONSTRAINT ck_source_messages_direction CHECK (direction IN ('inbound', 'outbound')),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    FOREIGN KEY(gmail_account_id) REFERENCES gmail_accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_source_messages_from ON source_messages (workspace_id, from_email);
CREATE INDEX IF NOT EXISTS ix_source_messages_from_domain ON source_messages (from_domain);
CREATE INDEX IF NOT EXISTS ix_source_messages_internal_date ON source_messages (workspace_id, internal_date);
CREATE INDEX IF NOT EXISTS ix_source_messages_thread ON source_messages (workspace_id, gmail_thread_id);
CREATE INDEX IF NOT EXISTS ix_source_messages_workspace_id ON source_messages (workspace_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    gmail_account_id UUID,
    mode TEXT NOT NULL,
    status TEXT DEFAULT 'running' NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    messages_listed INTEGER DEFAULT '0' NOT NULL,
    messages_metadata_fetched INTEGER DEFAULT '0' NOT NULL,
    messages_content_fetched INTEGER DEFAULT '0' NOT NULL,
    messages_ignored INTEGER DEFAULT '0' NOT NULL,
    attachments_processed INTEGER DEFAULT '0' NOT NULL,
    invoices_upserted INTEGER DEFAULT '0' NOT NULL,
    payment_events_upserted INTEGER DEFAULT '0' NOT NULL,
    start_history_id TEXT,
    end_history_id TEXT,
    history_fallback_used BOOLEAN DEFAULT false NOT NULL,
    error TEXT,
    details JSONB DEFAULT '{}' NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_sync_runs_mode CHECK (mode IN ('backfill', 'incremental', 'fallback_resync')),
    CONSTRAINT ck_sync_runs_status CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    FOREIGN KEY(gmail_account_id) REFERENCES gmail_accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_sync_runs_account_started ON sync_runs (gmail_account_id, started_at);
CREATE INDEX IF NOT EXISTS ix_sync_runs_workspace_id ON sync_runs (workspace_id);

CREATE TABLE IF NOT EXISTS source_attachments (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    source_message_id UUID NOT NULL,
    gmail_attachment_id TEXT,
    filename TEXT,
    mime_type TEXT,
    size_bytes INTEGER,
    content_sha256 TEXT,
    extraction_status TEXT DEFAULT 'pending' NOT NULL,
    extraction_method TEXT DEFAULT 'none' NOT NULL,
    extracted_text TEXT,
    page_count INTEGER,
    extraction_error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_source_attachments_message_att UNIQUE (source_message_id, gmail_attachment_id),
    CONSTRAINT ck_source_attachments_status CHECK (extraction_status IN ('pending', 'text_extracted', 'ocr_extracted', 'failed', 'skipped', 'ocr_unavailable')),
    CONSTRAINT ck_source_attachments_method CHECK (extraction_method IN ('none', 'pypdf', 'pymupdf', 'ocr_tesseract')),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    FOREIGN KEY(source_message_id) REFERENCES source_messages (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_source_attachments_sha ON source_attachments (workspace_id, content_sha256);
CREATE INDEX IF NOT EXISTS ix_source_attachments_source_message_id ON source_attachments (source_message_id);
CREATE INDEX IF NOT EXISTS ix_source_attachments_workspace_id ON source_attachments (workspace_id);

CREATE TABLE IF NOT EXISTS invoices (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    owner_id UUID NOT NULL,
    customer_id UUID,
    invoice_number TEXT,
    normalized_number TEXT,
    amount_paise BIGINT DEFAULT '0' NOT NULL,
    amount_paid_paise BIGINT DEFAULT '0' NOT NULL,
    balance_paise BIGINT DEFAULT '0' NOT NULL,
    currency TEXT DEFAULT 'INR' NOT NULL,
    issued_date DATE,
    due_date DATE,
    due_date_inferred BOOLEAN DEFAULT false NOT NULL,
    payment_state TEXT DEFAULT 'likely_unpaid' NOT NULL,
    reminder_state TEXT DEFAULT 'paused' NOT NULL,
    state_reason TEXT,
    evidence_strength TEXT DEFAULT 'gmail_inferred' NOT NULL,
    confidence FLOAT DEFAULT '0.5' NOT NULL,
    missing_fields JSONB DEFAULT '[]' NOT NULL,
    dispute_note TEXT,
    payment_claim_note TEXT,
    paused_until DATE,
    pause_reason TEXT,
    manually_paused BOOLEAN DEFAULT false NOT NULL,
    source_message_id UUID,
    source_attachment_id UUID,
    razorpay_invoice_id TEXT,
    razorpay_payment_id TEXT,
    reminder_count INTEGER DEFAULT '0' NOT NULL,
    last_reminder_at TIMESTAMP WITH TIME ZONE,
    dedupe_key TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_invoices_workspace_dedupe UNIQUE (workspace_id, dedupe_key),
    CONSTRAINT ck_invoices_payment_state CHECK (payment_state IN ('confirmed_paid', 'disputed', 'payment_claimed', 'needs_information', 'likely_unpaid')),
    CONSTRAINT ck_invoices_reminder_state CHECK (reminder_state IN ('ready_for_reminder', 'paused')),
    CONSTRAINT ck_invoices_evidence_strength CHECK (evidence_strength IN ('gmail_inferred', 'gmail_explicit', 'provider_confirmed')),
    CONSTRAINT ck_invoices_amount_non_negative CHECK (amount_paise >= 0),
    CONSTRAINT ck_invoices_balance_non_negative CHECK (balance_paise >= 0),
    CONSTRAINT ck_invoices_confirmed_paid_requires_provider CHECK (payment_state <> 'confirmed_paid' OR evidence_strength = 'provider_confirmed'),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(customer_id) REFERENCES customers (id) ON DELETE SET NULL,
    FOREIGN KEY(source_message_id) REFERENCES source_messages (id) ON DELETE SET NULL,
    FOREIGN KEY(source_attachment_id) REFERENCES source_attachments (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_invoices_customer ON invoices (workspace_id, customer_id);
CREATE INDEX IF NOT EXISTS ix_invoices_due_date ON invoices (workspace_id, due_date);
CREATE INDEX IF NOT EXISTS ix_invoices_normalized_number ON invoices (normalized_number);
CREATE INDEX IF NOT EXISTS ix_invoices_owner_id ON invoices (owner_id);
CREATE INDEX IF NOT EXISTS ix_invoices_queue ON invoices (workspace_id, payment_state, reminder_state);
CREATE INDEX IF NOT EXISTS ix_invoices_razorpay ON invoices (workspace_id, razorpay_invoice_id);
CREATE INDEX IF NOT EXISTS ix_invoices_workspace_id ON invoices (workspace_id);

CREATE TABLE IF NOT EXISTS payment_events (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    invoice_id UUID,
    customer_id UUID,
    source_message_id UUID,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    amount_paise BIGINT,
    currency TEXT DEFAULT 'INR' NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE,
    is_confirmation BOOLEAN DEFAULT false NOT NULL,
    evidence_snippet TEXT,
    reconciled_at TIMESTAMP WITH TIME ZONE,
    reconciliation_method TEXT,
    payload JSONB DEFAULT '{}' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_payment_events_provider_event UNIQUE (workspace_id, provider, provider_event_id),
    CONSTRAINT ck_payment_events_provider CHECK (provider IN ('gmail', 'razorpay', 'manual')),
    CONSTRAINT ck_payment_events_type CHECK (event_type IN ('payment_captured', 'payment_failed', 'invoice_paid', 'refund_created', 'email_payment_claim', 'email_receipt', 'email_dispute', 'manual_confirmation')),
    CONSTRAINT ck_payment_events_gmail_never_confirms CHECK (provider <> 'gmail' OR is_confirmation = false),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    FOREIGN KEY(invoice_id) REFERENCES invoices (id) ON DELETE SET NULL,
    FOREIGN KEY(customer_id) REFERENCES customers (id) ON DELETE SET NULL,
    FOREIGN KEY(source_message_id) REFERENCES source_messages (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_payment_events_invoice ON payment_events (workspace_id, invoice_id);
CREATE INDEX IF NOT EXISTS ix_payment_events_observed ON payment_events (workspace_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_payment_events_workspace_id ON payment_events (workspace_id);

CREATE TABLE IF NOT EXISTS invoice_source_links (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    invoice_id UUID NOT NULL,
    source_message_id UUID,
    source_attachment_id UUID,
    payment_event_id UUID,
    link_type TEXT NOT NULL,
    evidence_snippet TEXT,
    evidence_locator TEXT,
    evidence_hash TEXT NOT NULL,
    confidence FLOAT DEFAULT '0.5' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_invoice_source_links_evidence UNIQUE (invoice_id, link_type, evidence_hash),
    CONSTRAINT ck_invoice_source_links_type CHECK (link_type IN ('invoice_document', 'invoice_mention', 'payment_claim', 'payment_confirmation', 'dispute', 'reminder_context')),
    CONSTRAINT ck_invoice_source_links_has_source CHECK (source_message_id IS NOT NULL OR source_attachment_id IS NOT NULL OR payment_event_id IS NOT NULL),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
    FOREIGN KEY(invoice_id) REFERENCES invoices (id) ON DELETE CASCADE,
    FOREIGN KEY(source_message_id) REFERENCES source_messages (id) ON DELETE CASCADE,
    FOREIGN KEY(source_attachment_id) REFERENCES source_attachments (id) ON DELETE CASCADE,
    FOREIGN KEY(payment_event_id) REFERENCES payment_events (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_invoice_source_links_invoice ON invoice_source_links (invoice_id);
CREATE INDEX IF NOT EXISTS ix_invoice_source_links_workspace_id ON invoice_source_links (workspace_id);

-- --------------------------------------------------------------------------------------
-- Column documentation for the fields that carry product rules
-- --------------------------------------------------------------------------------------

COMMENT ON COLUMN gmail_accounts.refresh_token_encrypted IS
    'Fernet ciphertext (app.services.crypto). Plaintext never touches this column.';
COMMENT ON COLUMN gmail_accounts.last_history_id IS
    'Gmail sync cursor. Written only after a sync commits, so a crash re-reads rather than skipping.';
COMMENT ON COLUMN source_messages.body_text IS
    'Truncated evidence body. NULL for ignored/metadata-only messages: Topline retains evidence, not mailboxes.';
COMMENT ON COLUMN source_messages.relevance_reasons IS
    'Which scoring rules fired, so a skip decision is explainable to the owner.';
COMMENT ON COLUMN invoices.evidence_strength IS
    'gmail_inferred < gmail_explicit < provider_confirmed. confirmed_paid requires provider_confirmed.';
COMMENT ON COLUMN invoices.dedupe_key IS
    'Idempotency key for ingestion. See app.services.ledger.invoice_dedupe_key.';
COMMENT ON COLUMN payment_events.is_confirmation IS
    'True only for provider settlements. A CHECK constraint forces false for provider = gmail.';
COMMENT ON COLUMN invoice_source_links.evidence_hash IS
    'sha256(link_type, snippet, locator). Makes re-extraction over the same evidence a no-op.';
COMMENT ON COLUMN activity_log.dedupe_key IS
    'NULL means always insert. A value makes the audit write idempotent across replayed syncs.';

COMMIT;
