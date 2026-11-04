-- apex_desk/docs/supabase_migration_v8.sql
-- Live PnL: marks the current (intraday) row of the Daily PnL table. Idempotent. Run BEFORE the Work PC pulls this release.
alter table desk_pnl_daily add column if not exists live int default 0;
