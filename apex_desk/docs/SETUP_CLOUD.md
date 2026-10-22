<!-- apex_desk/docs/SETUP_CLOUD.md -->
# APEX Desk: cloud setup and test runbook

Names in use: Supabase project `apexstocks-desk`, Netlify site `apexstocks-desk`. Netlify can also deploy from GitHub (Base directory `apex_desk`, env vars SUPABASE_URL / SUPABASE_ANON_KEY).
Work PC trades and keeps SQLite. Supabase relays. Netlify serves the page.

## A. Supabase (10 min)
1. supabase.com > New project. Name `apex-desk`. Region closest to London (eu-west-2). Set a DB password; save it in your password manager.
2. SQL Editor > New query > paste all of `apex_desk/docs/supabase_schema.sql` > Run. Expect "Success".
3. Authentication > Users > Add user > Create new user. Add yourself and your manager (email + password, tick Auto Confirm User).
4. Authentication > Sign In / Providers > Email: turn OFF "Allow new users to sign up".
5. Project Settings > API. Copy:
   - Project URL
   - `anon` / publishable key (goes in the website)
   - `service_role` / secret key (Work PC `.env` only; never in chat, git or the site)

## B. Main PC: push code
```powershell
cd C:\Users\ravil\PycharmProjects\APEX-Stocks_algo-v2
git add apex_desk research/dislocation_study
git status          # confirm nothing else is staged (not config/, prod/, outputs/)
git commit -m "Add APEX Desk + dislocation study"
git push origin main
```

## C. Netlify bundle (Main PC)
```powershell
cd C:\Users\ravil\PycharmProjects\APEX-Stocks_algo-v2\apex_desk
python scripts\build_netlify.py https://YOUR-REF.supabase.co YOUR_ANON_KEY
```
1. app.netlify.com > Add new site > Deploy manually.
2. Drag the `apex_desk\netlify_site` folder in.
3. Site configuration > Change site name > `apex-desk-nish`.
4. URL: `https://apex-desk-nish.netlify.app`.

## D. Work PC: install and test (local first, then cloud)
```powershell
cd C:\Users\AdminCC\PycharmProjects\APEX-Stocks-Algo_v2
git pull origin main
pip install -r apex_desk\requirements.txt
cd apex_desk
copy .env.example .env
notepad .env
```
Fill in `.env`: new DEMO account (DESK_MT5_LOGIN / PASSWORD / SERVER / TERMINAL_PATH), plus the two Supabase values (URL, service key). Install a second MT5 terminal logged in to the new demo account.

Test sequence:
```powershell
python -m desk status                    # clock + session + cut-off
python -m desk scan --allow-stale        # scan latest cache; drop the flag once the cache is fresh
python -m desk serve                     # open http://127.0.0.1:8000, check signals + charts
```
Then enable the relay: set `DESK_SYNC_ENABLED=true` in `.env` (no yaml edit needed).
```powershell
python -m desk sync                      # one cycle; expect signals > 0, charts_new > 0
python -m desk sync --loop               # leave running while testing the website
```
Open the Netlify URL, sign in, mark a few Trade/Don't trade, press "Trade all confirmed". Within about 1 minute `python -m desk sync` output shows `pulled`/`queued`.

Execution dry run (keep `mode: dry_run`):
```powershell
python -m desk execute --force           # processes queued signals with paper orders
```
Check the Positions/History tabs. Only after a clean dry run, set `mode: live` (demo account) for ONE session and verify the MT5 fills.

Daily run: `python -m desk scheduler` (scan 17:20 ET, execute 09:31 ET, sync every minute). It runs beside the old scheduler; the magic number differs (20261001).

## Notes
- With sync on, use the Netlify site for decisions. Batch queue state is owned by the cloud until the open.
- Cut-off is enforced server-side in Supabase (RPC), so a late click is refused even with a wrong browser clock.
- "OFFLINE" in the mode pill means the Work PC has not synced for 10+ minutes.
- Charts are uploaded for tiers A/B/W only; C is info-only without chart.
- Rotate the service key if it ever leaks (Supabase > API > regenerate).

## v2 upgrade (Watchlist + risk override)
1. Supabase SQL Editor: run each BLOCK of `docs/supabase_migration_v2.sql` in order (idempotent).
2. Main PC: commit + push. Netlify rebuilds.
3. Work PC: `git pull origin main`, stop the desk scheduler (Ctrl+C), start it again. The local database migrates itself.
4. `python -m desk sync` once: the first run pushes the watchlist rows and charts.
