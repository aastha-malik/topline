-- 20260827230100_updated_at_triggers.sql
-- Keep `updated_at` honest for writes that do not come through SQLAlchemy
-- (Supabase Studio, psql, edge functions). The ORM sets it too; the trigger is the backstop.

BEGIN;

CREATE OR REPLACE FUNCTION topline_set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'workspaces',
        'users',
        'gmail_accounts',
        'source_messages',
        'source_attachments',
        'customers',
        'invoices',
        'payment_events'
    ]
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%1$s_updated_at ON %1$s', target_table
        );
        EXECUTE format(
            'CREATE TRIGGER trg_%1$s_updated_at
                 BEFORE UPDATE ON %1$s
                 FOR EACH ROW EXECUTE FUNCTION topline_set_updated_at()',
            target_table
        );
    END LOOP;
END;
$$;

COMMIT;
