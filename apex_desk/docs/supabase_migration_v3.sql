-- apex_desk/docs/supabase_migration_v3.sql
-- v3: tier tag on positions (Positions / Closed tabs). Idempotent. Run BEFORE the Work PC pulls the new code.
alter table desk_positions add column if not exists tier text;
