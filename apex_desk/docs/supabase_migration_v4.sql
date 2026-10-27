-- apex_desk/docs/supabase_migration_v4.sql
-- v4: link positions to their signal so the Executed tab can show entry / stop / exit on the chart. Idempotent.
-- Run BEFORE the Work PC pulls the new code.
alter table desk_positions add column if not exists sid text;
