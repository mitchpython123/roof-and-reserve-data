# Roof & Reserve: NEM data feed

This repository builds the small data file behind the "NEM at a glance" widget on
[roofandreserve.com](https://roofandreserve.com). It reads AEMO's public market data, keeps a
history of 5-minute prices, and publishes a summary for each NEM region.

## How it works

1. **Every hour**, at 7 and 22 minutes past, a cron job on the website hosting (cPanel) asks
   GitHub to start the Action in `.github/workflows/nem-data.yml`, which runs
   `python -m rr_nem update`. See "Scheduling" below.
2. The job downloads new AEMO files (5-minute prices and demand, daily final prices,
   rooftop solar estimates and the latest 30-minute pre-dispatch (P30) forecast) and adds
   them to the data store. Only the newest P30 run is kept, as `store/predispatch_latest.json`.
3. It then writes `nem-latest.json` and pushes it, with the store, to the **`data` branch**.
4. The widget on the website reads that file straight from GitHub:
   `https://raw.githubusercontent.com/mitchpython123/roof-and-reserve-data/data/nem-latest.json`

The website's hosting only sends a short "run now" request; all the downloading and
processing happens on GitHub, so the site stays fast.

The widget shows its "may be out of date" note when the data is more than about 2.5 hours
old. To run the job straight away, open the **Actions** tab, choose **Update NEM data** and
click **Run workflow**.

## Scheduling

GitHub's built-in schedule is best effort. In October 2026 it ran this hourly job only every
2 to 4.5 hours, so the workflow's own `schedule` is now just a backup, and the real trigger is
a cPanel cron job.

**One-off setup**

1. In GitHub, go to Settings → Developer settings → Personal access tokens →
   **Fine-grained tokens** → Generate new token.
   - Repository access: **Only select repositories** → `roof-and-reserve-data`
   - Permissions: **Actions: Read and write** (nothing else)
   - Expiration: up to 12 months. Diarise the expiry date: the feed silently falls back to
     GitHub's unreliable schedule when the token expires.
2. In cPanel File Manager, create `/home/CPANEL_USER/.gh_token` (in the home folder, **not**
   inside `public_html`) containing only the token. Set its permissions to 600.
3. In cPanel → **Cron Jobs**, add a job with Minute `7,22` and every other field `*`, and this
   command (replace `CPANEL_USER`):

   ```
   curl -s -o /dev/null -X POST -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $(cat /home/CPANEL_USER/.gh_token)" https://api.github.com/repos/mitchpython123/roof-and-reserve-data/actions/workflows/nem-data.yml/dispatches -d '{"ref":"main"}'
   ```

**Checking it works:** the Actions tab lists cron-started runs as `workflow_dispatch`, at
about 7 and 22 minutes past each hour.

**Why 7 and 22 minutes past:** the widget shows the last complete hour, so what matters is
running soon after the hour closes. AEMO's files for the interval ending on the hour are
normally out within a few minutes; the 22-minute run is a retry in case AEMO or GitHub was
slow.

## What the widget shows

| Figure | Meaning |
| --- | --- |
| Last hour's average price | Average of the twelve 5-minute prices in the most recent complete clock hour, in $/MWh and c/kWh |
| Today's average, low and high | Across the current market day, which runs 4am to 4am AEST |
| Negative prices today | Time priced below $0/MWh since 4am |
| Latest demand and rooftop solar | The most recent figures AEMO has published |
| Evening vs midday | Average 5pm to 9pm price minus average 10am to 3pm price, over the last 7 complete days |
| Best spread (`best_spread_last_day`) | Last complete market day, 4am to midnight only (after midnight is left out so the windows match a household's day): cheapest 3-hour block, and the dearest 3-hour block starting after it, chosen to maximise the difference. 3 hours suits the 13.5 kWh / 5 kW reference battery. Price spread only, no losses |
| Outlook (`outlook`) | AEMO's latest P30 forecast: average price over the next four half hours that have not started, against the last hour's average. Rising or falling if the difference is more than $10/MWh or 10%, otherwise steady. Also forecast demand change, today's highest forecast half hour, and the forecast by half hour for the chart (`forecast30`). Left out if the forecast run is over 2 hours old |
| Chart | Today's price by half hour, with the P30 forecast for the rest of the day |

All times are NEM time (AEST, UTC+10 all year). Prices are wholesale, not retail.

## Layout

| Path | Purpose |
| --- | --- |
| `rr_nem/` | The pipeline: download (`fetch.py`, `ingest.py`), read AEMO files (`mms.py`), store (`store.py`) and summarise (`derive.py`) |
| `tests/` | Unit tests: `python3 -m unittest discover -s tests -t .` |
| `widget/widget.html` | Source of the widget. The live copy sits in a Custom HTML block on the home page |
| `widget/sample-nem-latest.json`, `widget/preview.html` | Invented sample data and a local preview page |
| `scripts/` | `make_sample.py` and `build_preview.py` for the preview, and `backfill_nemosis.py` for loading history |

## Previewing the widget locally

```
python3 scripts/make_sample.py
python3 scripts/build_preview.py
cd widget && python3 -m http.server 8000    # then open http://localhost:8000/preview.html
```

## Updating the live widget

The widget code in WordPress is stored in a form WordPress cannot alter, because WordPress
otherwise rewrites characters such as `&&` and breaks the script. After changing
`widget/widget.html`, ask Claude to update the live copy, or replace the Custom HTML block by
hand and check the page afterwards.
