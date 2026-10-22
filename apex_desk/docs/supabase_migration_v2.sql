-- apex_desk/docs/supabase_migration_v2.sql
-- v2: persistent Watchlist, manual risk override, starred info setups. Idempotent; run block by block (or all at once).

-- BLOCK 1: new columns
alter table desk_signals add column if not exists sig_close double precision;
alter table desk_signals add column if not exists as_of date;
alter table desk_signals add column if not exists live_setup text;
alter table desk_signals add column if not exists risk_pct double precision;
alter table desk_signals add column if not exists risk_pct_default double precision;
alter table desk_signals add column if not exists risk_set_at timestamptz;
alter table desk_signals add column if not exists watch boolean not null default false;
alter table desk_signals add column if not exists last_decision text;
alter table desk_signals add column if not exists last_decision_date date;

-- BLOCK 2: decision + batch rules (starred setups are tradable)
create or replace function desk_set_decision(p_sid text, p_decision text, p_by text, p_note text default '')
returns desk_signals language plpgsql security definer set search_path = public as $$
declare r desk_signals;
begin
  if auth.uid() is null then raise exception 'Not signed in'; end if;
  if p_decision not in ('trade','skip','none') then raise exception 'decision must be trade, skip or none'; end if;
  select * into r from desk_signals where sid = p_sid for update;
  if not found then raise exception 'Signal not found'; end if;
  if r.tier not in ('A','B','W') and not r.watch then raise exception 'Info only: star it (Watch) to make it tradable'; end if;
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
    from desk_signals where entry_date = p_entry_date and (tier in ('A','B','W') or watch) and decision = 'trade' and exec_status = 'none';
  if n = 0 then raise exception 'No confirmed (Trade) signals to queue'; end if;
  insert into desk_batches(entry_date, created_by, n_signals, est_risk, est_notional) values (p_entry_date, p_by, n, rk, nt) returning id into bid;
  update desk_signals set exec_status = 'queued', batch_id = bid, updated_at = now()
    where entry_date = p_entry_date and (tier in ('A','B','W') or watch) and decision = 'trade' and exec_status = 'none';
  insert into desk_audit(id, ts, actor, action, detail)
    values ((extract(epoch from clock_timestamp()) * 1000000)::bigint, now(), p_by, 'web_batch_confirm', 'batch ' || bid || ' n=' || n);
  return jsonb_build_object('batch_id', bid, 'count', n, 'est_risk', rk, 'est_notional', nt);
end $$;

-- BLOCK 3: manual risk override + star
create or replace function desk_set_risk(p_sid text, p_risk_pct double precision, p_by text)
returns desk_signals language plpgsql security definer set search_path = public as $$
declare r desk_signals; f double precision; cur double precision;
begin
  if auth.uid() is null then raise exception 'Not signed in'; end if;
  if p_risk_pct < 0.0005 or p_risk_pct > 0.02 then raise exception 'Risk must be between 0.05%% and 2.00%% of equity'; end if;
  select * into r from desk_signals where sid = p_sid for update;
  if not found then raise exception 'Signal not found'; end if;
  if r.tier not in ('A','B','W') and not r.watch then raise exception 'Info only: star it (Watch) first'; end if;
  if r.exec_status <> 'none' then raise exception 'Signal already %', r.exec_status; end if;
  if now() >= r.cutoff_at then raise exception 'Cut-off passed: decisions are closed for this session'; end if;
  cur := coalesce(r.risk_pct, 0.01 * coalesce(r.risk_mult, 1));
  f := p_risk_pct / cur;                                   -- approximate preview; the Work PC recomputes exactly within a minute
  update desk_signals set risk_pct = p_risk_pct, risk_set_at = now(), est_shares = floor(coalesce(est_shares, 0) * f),
         est_risk = coalesce(est_risk, 0) * f, est_notional = coalesce(est_notional, 0) * f, updated_at = now()
   where sid = p_sid returning * into r;
  insert into desk_audit(id, ts, actor, action, detail)
    values ((extract(epoch from clock_timestamp()) * 1000000)::bigint, now(), p_by, 'web_risk_override', p_sid || ' ' || round((p_risk_pct * 100)::numeric, 3) || '%');
  return r;
end $$;

create or replace function desk_set_watch(p_sid text, p_by text)
returns desk_signals language plpgsql security definer set search_path = public as $$
declare r desk_signals;
begin
  if auth.uid() is null then raise exception 'Not signed in'; end if;
  update desk_signals set watch = true, updated_at = now() where sid = p_sid and exec_status = 'none' returning * into r;
  if not found then raise exception 'Signal not found or already executed'; end if;
  insert into desk_audit(id, ts, actor, action, detail)
    values ((extract(epoch from clock_timestamp()) * 1000000)::bigint, now(), p_by, 'web_watch', p_sid);
  return r;
end $$;

revoke all on function desk_set_decision(text, text, text, text), desk_confirm_batch(date, text), desk_set_risk(text, double precision, text), desk_set_watch(text, text) from public, anon;
grant execute on function desk_set_decision(text, text, text, text), desk_confirm_batch(date, text), desk_set_risk(text, double precision, text), desk_set_watch(text, text) to authenticated;
