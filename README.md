# Roof & Reserve: "NEM right now" widget and market-data store

A homepage widget that shows wholesale price conditions for the visitor's state (a dropdown, NSW by default), fed by a small data store built from AEMO's public data. The store keeps full 5-minute history, so it's also the base for later backtests and region profiles.

```
rr_nem/                  the data store and updater (Python standard library only, nothing to install)
scripts/make_sample.py   makes invented SAMPLE data so you can preview the widget
scripts/build_preview.py builds widget/preview.html
scripts/backfill_nemosis.py   one-off history download (run on your own computer)
tests/                   25 automated tests (python3 -m unittest discover -s tests -t .)
widget/widget.html       the block you paste into WordPress
.github/workflows/       the scheduled job that updates the data every 10 minutes
```

## 1. Preview it on your computer (5 minutes)

```
python3 scripts/make_sample.py
python3 scripts/build_preview.py
cd widget
python3 -m http.server 8000
```

Open http://localhost:8000/preview.html. You'll see the widget with a "Sample data" banner. (Double-clicking the file won't work because browsers block it from loading the data file; the little web server fixes that.)

## 2. Make the live data feed (GitHub, free)

1. Create a free GitHub account and a **public** repository, for example `roof-and-reserve-data`. Public matters: the free scheduled-job allowance is generous for public repositories only. It will hold nothing but code and public market data.
2. Upload everything in this folder. **Don't miss the hidden `.github` folder** (on a Mac press Cmd+Shift+. in Finder to show it). If uploading it is awkward, use *Add file → Create new file*, type `.github/workflows/nem-data.yml` as the name and paste the file's contents.
3. In the repository go to **Settings → Actions → General**, scroll to *Workflow permissions* and choose **Read and write permissions**, then Save.
4. Go to the **Actions** tab, pick **Update NEM data**, and click **Run workflow**. The first run downloads about two days of 5-minute data and fills older days from AEMO's daily files, so allow a few minutes. A green tick means it worked, and a new branch called `data` appears.
5. Open `https://raw.githubusercontent.com/YOUR-USERNAME/YOUR-REPO/data/nem-latest.json` in your browser. You should see a line of JSON starting `{"schema":1,"sample":false,...`.

After that it updates itself every 10 minutes.

## 3. Put the widget on your homepage

1. Edit your homepage in WordPress and add a **Custom HTML** block (not the Classic editor, which can mangle scripts).
2. Paste in all of `widget/widget.html`.
3. Change `REPLACE_WITH_DATA_URL` to your data address from step 2.5. Check `data-disclaimer` points at your Disclaimer page's address.
4. Update the page and look at it on your phone and desktop.

### Better: serve the data file from your own site (recommended)

By default visitors' browsers fetch the data file from GitHub, which means GitHub can see their IP addresses. To keep everything on your own domain, use a cPanel cron job to copy the file across every 10 minutes:

1. In cPanel **File Manager**, create a folder `public_html/data`.
2. In cPanel **Cron Jobs**, add one that runs every 10 minutes with this command (swap in your cPanel username, GitHub username and repository):

```
curl -fsS -o /home/CPANELUSER/public_html/data/nem-latest.json.tmp "https://raw.githubusercontent.com/GITHUBUSER/REPO/data/nem-latest.json" && mv /home/CPANELUSER/public_html/data/nem-latest.json.tmp /home/CPANELUSER/public_html/data/nem-latest.json
```

3. In the widget block set `data-src="/data/nem-latest.json"`.

### Add to your Privacy page

> The market-data widget on our homepage loads a data file (from our own server / from GitHub) and remembers the state you choose in your browser. It doesn't collect or store your location.

(Use "from GitHub" if you skip the cron job above, since GitHub can then log visitor IP addresses.)

## 4. Load the history (on your own computer, once)

```
pip install nemosis pandas
python3 scripts/backfill_nemosis.py --from 2021-01 --to 2026-09 --data-dir data
```

This writes years of 5-minute prices, demand and rooftop solar into `data/store/`. Query it directly with DuckDB or pandas, for example `SELECT * FROM read_csv('data/store/dispatch_price/*.csv.gz')`. To make the live updater carry the history forward, put the `data/` folder contents on the repository's `data` branch before the next run.

## What the numbers mean

- **Price:** 5-minute dispatch price (RRP, $/MWh), using the non-intervention run, in NEM time (AEST all year). 10 $/MWh equals 1 c/kWh.
- **Today:** AEMO's market day, 4am to 4am.
- **Midday / evening:** intervals starting 10:00-15:00 and 17:00-21:00.
- **Last 7 days:** the seven most recent complete market days (at least 280 of 288 intervals).
- **Evening vs midday:** evening average minus midday average. It's the *gross* spread, so efficiency losses, fees and your actual tariff all reduce what a real battery captures. The widget says so.
- **Rooftop solar:** AEMO's measured estimate, which lags by 30 minutes or more.

## Know before you launch

- **Not yet run against live AEMO data.** My workspace couldn't reach AEMO, so the downloader and file parser were built from AEMO's published layout and tested with synthetic files in that layout. The first Actions run is the real test. If it fails, the log names the step; send me the message and I'll fix it. A failed run leaves the last good data published.
- **Backfill is untested against the real archive** for the same reason.
- **Check AEMO's current terms** for using and republishing their data, and keep the source line on the widget.
- **GitHub's scheduler isn't exact.** Runs can be a few minutes late, and GitHub pauses scheduled jobs in a repository with no activity for 60 days. If the widget shows "may be out of date", check the Actions tab and re-enable the job.
- If AEMO ever blocks GitHub's servers, the same code can run from your own computer or a small server using cron.
- Sample data always carries a visible banner, so it can't be mistaken for live data.
