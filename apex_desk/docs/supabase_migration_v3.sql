-- apex_desk/docs/supabase_migration_v3.sql
-- v3: tier tag on positions (Positions / Closed tabs). Idempotent. Run BEFORE the Work PC pulls the new code.
alter table desk_positions add column if not exists tier text;
-- v3b: broker trade-mode flag on signals (close_only / disabled / short_only / no_symbol)
alter table desk_signals add column if not exists broker_mode text;
