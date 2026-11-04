-- apex_desk/docs/supabase_migration_v6.sql
-- Benchmark tab (S&P 500 vs our stocks). Idempotent. Run in the Supabase SQL editor BEFORE the Work PC pulls this release.
create table if not exists desk_bench_daily (
  date text primary key, spx_close double precision, spx_prev double precision, spx_pct double precision, ew_pct double precision, w_pct double precision,
  n_stocks int, equity double precision, equity_prev double precision, port_pct double precision, live int default 0, updated_at text);
create table if not exists desk_bench_stock (
  date text not null, ticker text not null, prev_close double precision, close double precision, pct double precision, weight double precision, live int default 0,
  primary key (date, ticker));

alter table desk_bench_daily enable row level security;
alter table desk_bench_stock enable row level security;
do $$ declare t text; begin
  foreach t in array array['desk_bench_daily','desk_bench_stock'] loop
    execute format('drop policy if exists "auth read" on %I', t);
    execute format('create policy "auth read" on %I for select to authenticated using (true)', t);
  end loop;
end $$;
revoke insert, update, delete on desk_bench_daily, desk_bench_stock from anon, authenticated;
