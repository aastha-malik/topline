-- Additive Topline agent-layer state. Canonical owners/customers/invoices,
-- Gmail evidence, payments, and activity_log stay owned by the core schema.

BEGIN;

CREATE TABLE IF NOT EXISTS agent_digests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    run_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'building',
    gmail_thread_id TEXT,
    owner_message_id TEXT,
    total_outstanding_paise BIGINT NOT NULL DEFAULT 0,
    customer_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_agent_digests_owner_date UNIQUE (owner_id, run_date),
    CONSTRAINT ck_agent_digests_status CHECK (status IN ('building', 'sent', 'failed')),
    CONSTRAINT ck_agent_digests_total_non_negative CHECK (total_outstanding_paise >= 0),
    CONSTRAINT ck_agent_digests_count_non_negative CHECK (customer_count >= 0)
);
CREATE INDEX IF NOT EXISTS ix_agent_digests_workspace_id ON agent_digests(workspace_id);
CREATE INDEX IF NOT EXISTS ix_agent_digests_owner_id ON agent_digests(owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_digests_owner_thread
    ON agent_digests(owner_id, gmail_thread_id) WHERE gmail_thread_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_digest_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    digest_id UUID NOT NULL REFERENCES agent_digests(id) ON DELETE CASCADE,
    item_number INTEGER NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    customer_name TEXT NOT NULL,
    invoice_ids JSONB NOT NULL,
    amount_paise BIGINT NOT NULL,
    oldest_due_date DATE NOT NULL,
    recommendation_reason TEXT NOT NULL,
    source_references JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'actionable',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_agent_digest_items_number UNIQUE (digest_id, item_number),
    CONSTRAINT uq_agent_digest_items_customer UNIQUE (digest_id, customer_id),
    CONSTRAINT ck_agent_digest_items_status
        CHECK (status IN ('actionable', 'drafted', 'skipped', 'paused')),
    CONSTRAINT ck_agent_digest_items_number_positive CHECK (item_number > 0),
    CONSTRAINT ck_agent_digest_items_amount_positive CHECK (amount_paise > 0),
    CONSTRAINT ck_agent_digest_items_invoices CHECK (jsonb_array_length(invoice_ids) > 0)
);
CREATE INDEX IF NOT EXISTS ix_agent_digest_items_workspace_id ON agent_digest_items(workspace_id);
CREATE INDEX IF NOT EXISTS ix_agent_digest_items_digest_id ON agent_digest_items(digest_id);

CREATE TABLE IF NOT EXISTS agent_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    digest_id UUID NOT NULL REFERENCES agent_digests(id) ON DELETE CASCADE,
    digest_item_id UUID NOT NULL REFERENCES agent_digest_items(id) ON DELETE RESTRICT,
    draft_number INTEGER NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    customer_email TEXT NOT NULL,
    invoice_ids JSONB NOT NULL,
    subject TEXT NOT NULL,
    text_body TEXT NOT NULL,
    rationale TEXT NOT NULL,
    tone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    source_snapshot JSONB NOT NULL,
    agent_decision JSONB NOT NULL,
    prompt_version TEXT NOT NULL,
    model_name TEXT NOT NULL,
    rendered_html TEXT NOT NULL,
    customer_thread_id TEXT,
    approved_by TEXT,
    approval_source TEXT,
    approved_at TIMESTAMPTZ,
    approved_content_hash TEXT,
    sent_at TIMESTAMPTZ,
    send_result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_agent_drafts_number UNIQUE (digest_id, draft_number),
    CONSTRAINT ck_agent_drafts_status CHECK (
        status IN ('pending', 'approved', 'sending', 'rejected', 'sent', 'failed', 'paused')
    ),
    CONSTRAINT ck_agent_drafts_number_positive CHECK (draft_number > 0),
    CONSTRAINT ck_agent_drafts_invoices CHECK (jsonb_array_length(invoice_ids) > 0),
    CONSTRAINT ck_agent_drafts_approval CHECK (
        status NOT IN ('approved', 'sending', 'sent') OR
        (approved_by IS NOT NULL AND approved_at IS NOT NULL AND approved_content_hash IS NOT NULL)
    ),
    CONSTRAINT ck_agent_drafts_sent CHECK (
        status <> 'sent' OR (sent_at IS NOT NULL AND send_result IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_agent_drafts_workspace_id ON agent_drafts(workspace_id);
CREATE INDEX IF NOT EXISTS ix_agent_drafts_owner_id ON agent_drafts(owner_id);
CREATE INDEX IF NOT EXISTS ix_agent_drafts_digest_id ON agent_drafts(digest_id);
CREATE INDEX IF NOT EXISTS ix_agent_drafts_owner_status ON agent_drafts(owner_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_agent_drafts_customer_thread
    ON agent_drafts(owner_id, customer_thread_id) WHERE customer_thread_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_review_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    digest_id UUID REFERENCES agent_digests(id) ON DELETE CASCADE,
    customer_id UUID REFERENCES customers(id) ON DELETE SET NULL,
    invoice_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    CONSTRAINT ck_agent_review_tasks_kind CHECK (
        kind IN ('owner_command_clarification', 'customer_reply', 'send_failure')
    ),
    CONSTRAINT ck_agent_review_tasks_status CHECK (status IN ('open', 'resolved', 'dismissed'))
);
CREATE INDEX IF NOT EXISTS ix_agent_review_tasks_workspace_id ON agent_review_tasks(workspace_id);
CREATE INDEX IF NOT EXISTS ix_agent_review_tasks_owner_id ON agent_review_tasks(owner_id);
CREATE INDEX IF NOT EXISTS ix_agent_review_tasks_owner_open
    ON agent_review_tasks(owner_id, created_at DESC) WHERE status = 'open';

DO $$
DECLARE target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'agent_digests', 'agent_digest_items', 'agent_drafts'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_%1$s_updated_at BEFORE UPDATE ON %1$s '
            'FOR EACH ROW EXECUTE FUNCTION topline_set_updated_at()',
            target_table
        );
    END LOOP;
END;
$$;

DO $$
DECLARE target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'agent_digests', 'agent_digest_items', 'agent_drafts', 'agent_review_tasks'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target_table);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target_table);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM anon, authenticated', target_table);
    END LOOP;
END;
$$;

COMMIT;
