"""Retail rules and reference prices (nem-retail.json).

The figures live in retail/<financial year>.json, one hand-checked file per year, each with
`effective_from` and `effective_to` dates. The feed carries the file in force today as
`current` and, once next year's file has been added, that file as `next`, so the widget can
switch over at midnight on 1 July without anyone touching the site.

`validate` checks a file before it is published: every source it names exists, and every
hour of every month, on weekdays and weekends, resolves to exactly one period in each plan.
"""

import json
import os
from datetime import date

from .common import REGIONS, iso_nem, now_nem

RETAIL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "retail")
KINDS = {"peak", "shoulder", "offpeak", "soak", "free"}
FIT_KINDS = {"benchmark", "regulated", "retailer"}


class RetailError(ValueError):
    pass


def _in_windows(hour, windows):
    return any(a <= hour < b for a, b in windows)


def period_at(plan, month, weekday, hour):
    """The period of `plan` that applies at `hour` (0-23) in `month` (1-12) on a weekday
    (True) or weekend day (False). Periods are matched in order; a `rest` period matches
    anything left over."""
    for p in plan["periods"]:
        if p.get("months") and month not in p["months"]:
            continue
        days = p.get("days", "all")
        if (days == "weekdays" and not weekday) or (days == "weekends" and weekday):
            continue
        if p.get("rest") or _in_windows(hour, p.get("windows", [])):
            return p
    return None


def validate(doc):
    """Raise RetailError on the first problem in a retail year file."""
    for key in ("fy", "effective_from", "effective_to", "sources", "networks"):
        if key not in doc:
            raise RetailError(f"missing {key}")
    start, end = date.fromisoformat(doc["effective_from"]), date.fromisoformat(doc["effective_to"])
    if end <= start:
        raise RetailError("effective_to must be after effective_from")
    sources = doc["sources"]
    for sid, s in sources.items():
        for key in ("name", "provider", "url", "about"):
            if not s.get(key):
                raise RetailError(f"source {sid}: missing {key}")
    seen = set()
    for n in doc["networks"]:
        nid = n.get("id")
        if not nid or nid in seen:
            raise RetailError(f"network id missing or repeated: {nid}")
        seen.add(nid)
        if n.get("region") not in REGIONS:
            raise RetailError(f"{nid}: unknown region {n.get('region')}")
        fit = n.get("fit") or {}
        if fit.get("kind") not in FIT_KINDS:
            raise RetailError(f"{nid}: feed-in kind must be one of {sorted(FIT_KINDS)}")
        if fit.get("source") and fit["source"] not in sources:
            raise RetailError(f"{nid}: unknown feed-in source {fit['source']}")
        if fit["kind"] == "regulated" and not isinstance(fit.get("rate"), (int, float)):
            raise RetailError(f"{nid}: a regulated feed-in tariff needs a rate")
        if fit["kind"] == "benchmark" and len(fit.get("range") or []) != 2:
            raise RetailError(f"{nid}: a benchmark feed-in tariff needs a range")
        exp = n.get("export")
        if exp and exp.get("source") not in sources:
            raise RetailError(f"{nid}: unknown export source")
        if not n.get("plans"):
            raise RetailError(f"{nid}: no plans")
        for plan in n["plans"]:
            label = f"{nid}/{plan.get('id')}"
            if plan.get("source") not in sources:
                raise RetailError(f"{label}: unknown source {plan.get('source')}")
            if not isinstance(plan.get("supply"), (int, float)):
                raise RetailError(f"{label}: supply charge missing")
            for p in plan["periods"]:
                if p.get("kind") not in KINDS:
                    raise RetailError(f"{label}: period {p.get('name')} has unknown kind {p.get('kind')}")
                if not isinstance(p.get("rate"), (int, float)) or not 0 <= p["rate"] < 200:
                    raise RetailError(f"{label}: period {p.get('name')} rate looks wrong: {p.get('rate')}")
                for a, b in p.get("windows", []):
                    if not (0 <= a < b <= 24):
                        raise RetailError(f"{label}: bad window {[a, b]}")
            for month in range(1, 13):
                for weekday in (True, False):
                    for hour in range(24):
                        if period_at(plan, month, weekday, hour) is None:
                            raise RetailError(f"{label}: no period covers {hour}:00 in month {month} "
                                              f"({'weekday' if weekday else 'weekend'})")
    return True


def load_years(directory=RETAIL_DIR):
    """All retail year files, validated, sorted by start date."""
    docs = []
    if not os.path.isdir(directory):
        return docs
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                doc = json.load(fh)
            try:
                validate(doc)
            except RetailError as exc:
                raise RetailError(f"{name}: {exc}") from None
            docs.append(doc)
    docs.sort(key=lambda d: d["effective_from"])
    for a, b in zip(docs, docs[1:]):
        if b["effective_from"] <= a["effective_to"]:
            raise RetailError(f"{a['fy']} and {b['fy']} overlap")
    return docs


def build_retail_feed(today=None, directory=RETAIL_DIR, now=None):
    """nem-retail.json content, or None when there are no retail files."""
    docs = load_years(directory)
    if not docs:
        return None
    today = today or now_nem().date()
    iso = today.isoformat()
    current = next((d for d in docs if d["effective_from"] <= iso <= d["effective_to"]), None)
    stale = False
    if current is None:
        past = [d for d in docs if d["effective_to"] < iso]
        current = past[-1] if past else docs[0]
        stale = bool(past)
    later = [d for d in docs if d["effective_from"] > current["effective_to"]]
    return {
        "schema": 1,
        "generated_at": iso_nem(now or now_nem()),
        "current": current,
        "next": later[0] if later else None,
        "stale": stale,
    }
