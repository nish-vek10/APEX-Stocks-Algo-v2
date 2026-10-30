-- apex_desk/docs/supabase_migration_v5.sql
-- Daily P&L history (portfolio roll-up + per-position decks). Idempotent. Run in the Supabase SQL editor BEFORE the Work PC pulls this release.
create table if not exists desk_pnl_daily (
  date text primary key, pnl double precision, n_open int, n_opened int, risk_opened double precision, risk_open double precision);
create table if not exists desk_pnl_pos (
  position_id bigint not null, date text not null, ticker text, close double precision, pnl double precision, status text,
  primary key (position_id, date));

alter table desk_pnl_daily enable row level security;
alter table desk_pnl_pos   enable row level security;
do $$ declare t text; begin
  foreach t in array array['desk_pnl_daily','desk_pnl_pos'] loop
    execute format('drop policy if exists "auth read" on %I', t);
    execute format('create policy "auth read" on %I for select to authenticated using (true)', t);
  end loop;
end $$;
revoke insert, update, delete on desk_pnl_daily, desk_pnl_pos from anon, authenticated;
