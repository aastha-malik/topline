-- 20260827230200_row_level_security.sql
-- Supabase exposes every table through PostgREST on the anon key. These tables hold
-- encrypted mailbox tokens and customer financial data, so RLS is enabled and left
-- with **no permissive policy for anon/authenticated**: the only way in is the FastAPI
-- backend, which connects as the database owner (or service role) and bypasses RLS.
--
-- When Supabase Auth is wired to `users.supabase_user_id`, replace the deny-all posture
-- below with per-workspace policies rather than loosening the grants.

BEGIN;

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'workspaces',
        'users',
        'gmail_accounts',
        'oauth_states',
        'sync_runs',
        'source_messages',
        'source_attachments',
        'customers',
        'invoices',
        'payment_events',
        'invoice_source_links',
        'activity_log'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target_table);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target_table);
        -- `anon` and `authenticated` exist on Supabase but not on a plain Postgres
        -- (local development, CI), so the revoke is applied per role only if present.
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', target_table);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', target_table);
        END IF;
    END LOOP;
END;
$$;

COMMIT;
