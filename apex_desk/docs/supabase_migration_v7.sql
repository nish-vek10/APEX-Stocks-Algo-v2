-- apex_desk/docs/supabase_migration_v7.sql
-- Daily PnL: portfolio equity columns (start equity + all P&L since inception). Idempotent. Run BEFORE the Work PC pulls this release.
alter table desk_pnl_daily add column if not exists realised double precision;
alter table desk_pnl_daily add column if not exists equity   double precision;
