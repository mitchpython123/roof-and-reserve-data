# Roof & Reserve: NEM data feed

This repository builds the small data file behind the "NEM at a glance" widget on
[roofandreserve.com](https://roofandreserve.com). It reads AEMO's public market data, keeps a
history of 5-minute prices, and publishes a summary for each NEM region.

## How it works

1. **Every hour**, at about 7 minutes past, the GitHub Action in
   `.github/workflows/nem-data.yml` runs `python -m rr_nem update`.
2. The job downloads new AEMO files (5-minute prices and demand, daily final prices and
   rooftop solar estimates) and adds them to the data store.
3. It then writes `nem-latest.json` and pushes it, with the store, to the **`data` branch**.
4. The widget on the website reads that file straight from GitHub:
   `https://raw.githubusercontent.com/mitchpython123/roof-and-reserve-data/data/nem-latest.json`

Nothing runs on the website's own hosting, so the site stays fast.

GitHub starts scheduled jobs on a best-effort basis, so a run can start late. The widget only
shows its "may be out of date" note when the data is more than about 2.5 hours old. To run the
job straight away, open the **Actions** tab, choose **Update NEM data** and click
**Run workflow**.

## What the widget shows

| Figure | Meaning |
| --- | --- |
| Last hour's average price | Average of the twelve 5-minute prices in the most recent complete clock hour, in $/MWh and c/kWh |
| Today's average, low and high | Across the current market day, which runs 4am to 4am AEST |
| Negative prices today | Time priced below $0/MWh since 4am |
| Latest demand and rooftop solar | The most recent figures AEMO has published |
| Evening vs midday | Average 5pm to 9pm price minus average 10am to 3pm price, over the last 7 complete days |
| Chart | Today's price by half hour |

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
