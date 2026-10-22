-- apex_desk/docs/supabase_schema.sql
-- Cloud UI datastore for APEX Desk. The Work PC (scanner + executor + sync) stays the system that TRADES;
-- Supabase only holds what the Netlify dashboard needs and relays your Trade / Don't trade decisions back.
-- Writes from the browser go ONLY through the SECURITY DEFINER functions at the bottom (they enforce the 13:30 London cut-off
-- server-side). The Work PC writes with the service_role key (bypasses RLS). Safe to re-run.

create table if not exists desk_signals (
  sid text primary key,                                   -- 'YYYY-MM-DD|TICKER' (signal_date|ticker)
  signal_date date not null, entry_date date not null, ticker text not null,
  tier text not null check (tier in ('A','B','W','C')),
  setup text, setup_tags text, stage int, stage_name text, prev_stage int,
  close double precision, atr double precision, atr_pct double precision,
  ema10 double precision, ema20 double precision, ema50 double precision, ema200 double precision,
  dist200 double precision, days_since_s2 int, donchian_upper double precision, bb_lower double precision, bb_mid double precision,
  volume_ratio double precision, rsi double precision, avg_dollar_vol double precision,
  ref_entry double precision, stop_price double precision, stop_dist double precision, stop_pct double precision,
  est_shares double precision, est_risk double precision, est_notional double precision, risk_mult double precision,
  cutoff_at timestamptz not null,                          -- decisions close here (09:30 ET minus 60 min)
  decision text not null default 'none' check (decision in ('none','trade','skip')),
  decided_by text, decided_email text, decided_at timestamptz, note text,
  exec_status text not null default 'none' check (exec_status in ('none','queued','executing','executed','failed','expired')),
  exec_detail text, batch_id bigint,
  updated_at timestamptz not null default now()
);
create index if not exists ix_desk_signals_entry on desk_signals(entry_date, tier);

create table if not exists desk_charts   (sid text primary key references desk_signals(sid) on delete cascade, payload jsonb not null, created_at timestamptz not null default now());
create table if not exists desk_status   (id int primary key default 1 check (id = 1), payload jsonb not null, updated_at timestamptz not null default now());
create table if not exists desk_positions(id bigint primary key, ticker text, broker_symbol text, ticket bigint, entry_date text, entry_price double precision, stop_price double precision,
                                          shares double precision, mt5_volume double precision, stop_distance double precision, status text, exit_date text, exit_price double precision,
                                          exit_reason text, pnl_total double precision, pnl_r double precision);
create table if not exists desk_audit    (id bigint primary key, ts timestamptz, actor text, action text, detail text);
create table if not exists desk_batches  (id bigint generated always as identity primary key, entry_date date not null, created_by text, created_at timestamptz not null default now(), n_signals int, est_risk double precision, est_notional double precision, status text default 'queued');
create table if not exists desk_commands (id bigint generated always as identity primary key, cmd text not null, requested_by text, created_at timestamptz not null default now(), done_at timestamptz);

-- ---------- row level security: read-only for signed-in users, no direct writes ----------
alter table desk_signals   enable row level security;  alter table desk_charts  enable row level security;
alter table desk_status    enable row level security;  alter table desk_positions enable row level security;
alter table desk_audit     enable row level security;  alter table desk_batches enable row level security;
alter table desk_commands  enable row level security;
do $$ declare t text; begin
  foreach t in array array['desk_signals','desk_charts','desk_status','desk_positions','desk_audit','desk_batches','desk_commands'] loop
    execute format('drop policy if exists "auth read" on %I', t);
    execute format('create policy "auth read" on %I for select to authenticated using (true)', t);
  end loop;
end $$;
revoke insert, update, delete on desk_signals, desk_charts, desk_status, desk_positions, desk_audit, desk_batches, desk_commands from anon, authenticated;

-- ---------- server-side rules (browser calls these via supabase.rpc) ----------
create or replace function desk_set_decision(p_sid text, p_decision text, p_by text, p_note text default '')
returns desk_signals language plpgsql security definer set search_path = public as $$
declare r desk_signals;
begin
  if auth.uid() is null then raise exception 'Not signed in'; end if;
  if p_decision not in ('trade','skip','none') then raise exception 'decision must be trade, skip or none'; end if;
  select * into r from desk_signals where sid = p_sid for update;
  if not found then raise exception 'Signal not found'; end if;
  if r.tier not in ('A','B','W') then raise exception 'Info only: this tier is not tradable'; end if;
  if r.exec_status <> 'none' then raise exception 'Signal already %', r.exec_status; end if;
  if now() >= r.cutoff_at then raise exception 'Cut-off passed: decisions are closed for this session'; end if;
  update desk_signals set decision = p_decision, decided_by = p_by, decided_email = (auth.jwt() ->> 'email'),
         decided_at = now(), note = p_note, updated_at = now() where sid = p_sid returning * into r;
  insert into desk_audit(id, ts, actor, action, detail)
    values ((extract(epoch from clock_timestamp()) * 1000000)::bigint, now(), p_by, 'web_decision:' || p_decision, p_sid || ' ' || coalesce(p_note, ''));
  return r;
end $$;

create or replace function desk_confirm_batch(p_entry_date date, p_by text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare n int; bid bigint; rk double precision; nt double precision; cut timestamptz;
begin
  if auth.uid() is null then raise exception 'Not signed in'; end if;
  select min(cutoff_at) into cut from desk_signals where entry_date = p_entry_date;
  if cut is null then raise exception 'No signals for that session'; end if;
  if now() >= cut then raise exception 'Cut-off passed: the batch can no longer be confirmed'; end if;
  select count(*), coalesce(sum(est_risk), 0), coalesce(sum(est_notional), 0) into n, rk, nt
    from desk_signals where entry_date = p_entry_date and tier in ('A','B','W') and decision = 'trade' and exec_status = 'none';
  if n = 0 then raise exception 'No confirmed (Trade) signals to queue'; end if;
  insert into desk_batches(entry_date, created_by, n_signals, est_risk, est_notional) values (p_entry_date, p_by, n, rk, nt) returning id into bid;
  update desk_signals set exec_status = 'queued', batch_id = bid, updated_at = now()
    where entry_date = p_entry_date and tier in ('A','B','W') and decision = 'trade' and exec_status = 'none';
  insert into desk_audit(id, ts, actor, action, detail)
    values ((extract(epoch from clock_timestamp()) * 1000000)::bigint, now(), p_by, 'web_batch_confirm', 'batch ' || bid || ' n=' || n);
  return jsonb_build_object('batch_id', bid, 'count', n, 'est_risk', rk, 'est_notional', nt);
end $$;

create or replace function desk_unlock_batch(p_entry_date date, p_by text)
returns int language plpgsql security definer set search_path = public as $$
declare n int; cut timestamptz;
begin
  if auth.uid() is null then raise exception 'Not signed in'; end if;
  select min(cutoff_at) into cut from desk_signals where entry_date = p_entry_date;
  if cut is null or now() >= cut then raise exception 'Cut-off passed: the batch is final'; end if;
  update desk_signals set exec_status = 'none', batch_id = null, updated_at = now() where entry_date = p_entry_date and exec_status = 'queued';
  get diagnostics n = row_count;
  update desk_batches set status = 'cancelled' where entry_date = p_entry_date and status = 'queued';
  insert into desk_audit(id, ts, actor, action, detail)
    values ((extract(epoch from clock_timestamp()) * 1000000)::bigint, now(), p_by, 'web_batch_unlock', 'n=' || n);
  return n;
end $$;

create or replace function desk_request_scan(p_by text)
returns void language plpgsql security definer set search_path = public as $$
begin
  if auth.uid() is null then raise exception 'Not signed in'; end if;
  insert into desk_commands(cmd, requested_by) values ('scan', p_by);
end $$;

revoke all on function desk_set_decision(text, text, text, text), desk_confirm_batch(date, text), desk_unlock_batch(date, text), desk_request_scan(text) from public, anon;
grant execute on function desk_set_decision(text, text, text, text), desk_confirm_batch(date, text), desk_unlock_batch(date, text), desk_request_scan(text) to authenticated;
