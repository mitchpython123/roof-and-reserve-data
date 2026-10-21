"""Market price of small-scale technology certificates (STCs), for the rebate calculator.

The calculator on roofandreserve.com/battery-rebate-calculator/ reads `stc-price.json` from
the data branch and uses it as the STC price behind its discount estimates. If the file is
missing or unusable, the calculator falls back to the price written into its own script.

The price is the median of STC spot trades reported by Demand Manager over the 30 days to
the latest trade, so a single odd trade does not move it. It is refreshed at most once a
week, and an existing file is left alone if the source cannot be read or looks wrong.
"""

import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone

from .common import NEM_TZ, iso_nem

SOURCE_URL = "https://www.demandmanager.com.au/graphs/new_prices.php"
SOURCE_NAME = "Demand Manager STC spot trades"
FILE_NAME = "stc-price.json"
REFRESH_AFTER = timedelta(days=7)
WINDOW_DAYS = 30
# The STC clearing house price is fixed at $40, which caps the market in practice.
LOW, HIGH = 20.0, 40.0

_POINTS = re.compile(r'\[\{"x":.*?\}\]', re.S)
_LAST_PRICE = re.compile(r"STC\s*\$\s*([0-9]+(?:\.[0-9]+)?)")


def _stc_trades(html):
    """[(date, price)] from the STC series of the page's price chart."""
    marker = html.find('name: "STC"')
    if marker < 0:
        return []
    block_start = html.rfind('type: "scatter"', 0, marker)
    arrays = _POINTS.findall(html[max(block_start, 0):marker])
    if not arrays:
        return []
    trades = []
    for point in json.loads(arrays[-1]):
        try:
            when = datetime.fromtimestamp(point["x"] / 1000, tz=timezone.utc).astimezone(NEM_TZ).date()
            trades.append((when, float(point["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return trades


def parse_stc_price(html, today):
    """Return {"price", "as_of", "trades"} from the Demand Manager page, or None."""
    trades = [(d, p) for d, p in _stc_trades(html) if LOW < p <= HIGH]
    if trades:
        latest = max(d for d, _ in trades)
        window = [p for d, p in trades if d > latest - timedelta(days=WINDOW_DAYS)]
        return {"price": round(statistics.median(window), 2), "as_of": latest.isoformat(),
                "trades": len(window)}
    match = _LAST_PRICE.search(html)
    if match and LOW < float(match.group(1)) <= HIGH:
        return {"price": round(float(match.group(1)), 2), "as_of": today.isoformat(), "trades": 0}
    return None


def refresh_stc_price(data_dir, fetcher, now, force=False):
    """Update <data_dir>/stc-price.json if it is over a week old. Returns a short status."""
    path = os.path.join(data_dir, FILE_NAME)
    if not force and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                fetched = datetime.fromisoformat(json.load(fh)["fetched_at"]).replace(tzinfo=None)
            if now - fetched < REFRESH_AFTER:
                return "stc: up to date"
        except (OSError, ValueError, KeyError, TypeError):
            pass  # unreadable file: try to replace it
    try:
        html = fetcher.get(SOURCE_URL).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - never fail the NEM update over this
        print(f"warning: could not download the STC price: {exc}", file=sys.stderr)
        return "stc: source unavailable, kept existing file"
    found = parse_stc_price(html, now.date())
    if not found:
        print("warning: no usable STC price on the source page", file=sys.stderr)
        return "stc: no usable price, kept existing file"
    found.update({"currency": "AUD", "source": SOURCE_NAME, "source_url": SOURCE_URL,
                  "fetched_at": iso_nem(now)})
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(found, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return f"stc: ${found['price']:.2f} as at {found['as_of']} ({found['trades']} trades)"
