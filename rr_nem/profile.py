"""The long-run feed: a typical day for each month, per region (nem-profile.json).

Built only from the profile30 table, so it never needs the raw 5-minute history.

For each region and calendar month (up to the last 13, the current month included and marked
`partial`) it gives, by clock hour from midnight (NEM time, AEST):
  * price     average wholesale price, $/MWh
  * neg       share of 5-minute intervals with a price below zero, %
  * solar     average rooftop solar output, MW
  * demand    average operational demand, MW
plus a month summary: average price, midday (10am to 3pm) and evening (5pm to 9pm) averages,
the gap between them, hours of negative prices and the number of 5-minute price spikes above
$300/MWh. `recent` is the same typical day over the last 28 complete market days.

Only complete market days count (at least 280 of 288 prices), so a day with missing data
cannot skew a month. Months are calendar months of market days (a market day runs 4am to 4am).
"""

from datetime import timedelta

from .common import EVENING, MIDDAY, REGIONS, iso_nem, now_nem
from .derive import SPIKE

COMPLETE_MIN = 280
MONTHS = 13
RECENT_DAYS = 28


def _clock_hour(slot):
    """Clock hour (0 = midnight) of a profile slot (slot 0 = 04:00-04:30)."""
    return ((slot + 8) % 48) // 2


def _r(value, places=2):
    return None if value is None else round(value, places)


class _Hours:
    """Running totals by clock hour for one region over a set of days."""

    def __init__(self):
        self.n = [0] * 24
        self.s = [0.0] * 24
        self.neg = [0] * 24
        self.spike = 0
        self.dem = [[0.0, 0] for _ in range(24)]
        self.pv = [[0.0, 0] for _ in range(24)]
        self.days = set()

    def add(self, day, slot, n, avg, neg, spike, demand, rooftop):
        h = _clock_hour(slot)
        self.days.add(day)
        self.n[h] += n
        self.s[h] += avg * n
        self.neg[h] += neg
        self.spike += spike
        if demand is not None:
            self.dem[h][0] += demand
            self.dem[h][1] += 1
        if rooftop is not None:
            self.pv[h][0] += rooftop
            self.pv[h][1] += 1

    def _window(self, hours):
        n = sum(self.n[h] for h in range(*hours))
        return sum(self.s[h] for h in range(*hours)) / n if n else None

    def summary(self):
        n = sum(self.n)
        mid, eve = self._window(MIDDAY), self._window(EVENING)
        return {
            "days": len(self.days),
            "price": [_r(self.s[h] / self.n[h]) if self.n[h] else None for h in range(24)],
            "neg": [_r(100.0 * self.neg[h] / self.n[h], 1) if self.n[h] else None for h in range(24)],
            "solar": [_r(t / k, 0) if k else None for t, k in self.pv],
            "demand": [_r(t / k, 0) if k else None for t, k in self.dem],
            "avg": _r(sum(self.s) / n) if n else None,
            "midday_avg": _r(mid),
            "evening_avg": _r(eve),
            "gap": _r(eve - mid) if mid is not None and eve is not None else None,
            "negative_hours": _r(sum(self.neg) * 5 / 60, 1),
            "spike_intervals": self.spike,
        }


def build_profile_feed(store, now=None, months=MONTHS):
    """Return the nem-profile.json content as a dict, or None if the profile table is empty."""
    rows = list(store.rows("profile30"))
    if not rows:
        return None
    # which market days are complete, per region
    totals = {}
    for day, region, slot, n, *_ in rows:
        totals[(region, day)] = totals.get((region, day), 0) + n
    complete = {key for key, n in totals.items() if n >= COMPLETE_MIN}
    latest_day = max(day for _, day in totals)
    latest_complete = max((day for _, day in complete), default=None)

    month_keys = sorted({day.strftime("%Y-%m") for _, day in totals})[-months:]
    recent_from = latest_complete - timedelta(days=RECENT_DAYS - 1) if latest_complete else None

    acc = {}
    for day, region, slot, n, avg, _lo, _hi, neg, spike, demand, rooftop in rows:
        if region not in REGIONS or (region, day) not in complete or not n:
            continue
        month = day.strftime("%Y-%m")
        if month in month_keys:
            acc.setdefault((region, month), _Hours()).add(day, slot, n, avg, neg, spike, demand, rooftop)
        if recent_from and day >= recent_from:
            acc.setdefault((region, "recent"), _Hours()).add(day, slot, n, avg, neg, spike, demand, rooftop)

    current_month = latest_day.strftime("%Y-%m")
    regions = {}
    for region, name in REGIONS.items():
        out_months = []
        for month in month_keys:
            h = acc.get((region, month))
            if h is None:
                continue
            item = {"month": month, "partial": month == current_month}
            item.update(h.summary())
            out_months.append(item)
        recent = acc.get((region, "recent"))
        regions[region] = {
            "name": name,
            "months": out_months,
            "recent": None if recent is None else dict(recent.summary(), to=latest_complete.isoformat()),
        }
    return {
        "schema": 1,
        "source": "AEMO (public NEMWEB data)",
        "generated_at": iso_nem(now or now_nem()),
        "latest_complete_day": latest_complete.isoformat() if latest_complete else None,
        "hours": "clock hours from midnight, NEM time (AEST, no daylight saving)",
        "units": {"price": "$/MWh", "neg": "% of 5-minute intervals", "solar": "MW", "demand": "MW"},
        "windows": {"midday": list(MIDDAY), "evening": list(EVENING)},
        "spike_threshold": SPIKE,
        "regions": regions,
    }
