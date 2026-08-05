#!/usr/bin/env python3
"""
Builds the forex widgets' rate file from the ECB's own reference rates.

    python3 fetch_ecb.py --out public/tools/rates.json

No API key, no account, no third party.

--------------------------------------------------------------------------
WHY THE ECB AND NOT A RATES API
--------------------------------------------------------------------------
The ECB publishes its euro foreign-exchange reference rates itself and states
plainly that they may be reused, free of charge, provided the ECB is cited as
the source. That is a licence you can point at, from an institution that is
not going to disappear or start charging.

The alternatives all have a catch:

  Frankfurter and the various free rate APIs are convenient wrappers around
  this same ECB data. They add a dependency and a rate limit without adding
  a number, and their INR leg traces back to FBIL.

  FBIL publishes the official USD/INR reference rate. Its terms are
  restrictive and its FAQ threatens action over unregistered commercial use.
  Nothing here touches it, and no widget calls its number "the RBI reference
  rate" — because that is precisely what FBIL licenses.

So USD/INR here is derived, not quoted: the ECB gives EUR/USD and EUR/INR,
and the cross is (EUR/INR) / (EUR/USD). It tracks the official rate closely
but it is our own arithmetic on published data, which is the point. Every
widget labels it a market rate for that reason.

--------------------------------------------------------------------------
WHAT IT WRITES
--------------------------------------------------------------------------
  updated   the ECB's own date for the latest rates, not today's date —
            they publish once a day around 16:00 CET and not at all on
            TARGET holidays, so "today" would frequently be a lie
  rates     EUR-based rates for every currency the ECB quotes, which is
            what lets the converter cross any pair without another request
  usdinr    the daily USD/INR series: every trading day for the last year,
            then weekly going back, which keeps the file small enough to
            bake into a widget while still drawing a smooth ten-year chart

--------------------------------------------------------------------------
HOW IT AVOIDS BREAKING
--------------------------------------------------------------------------
  * Two sources, tried in order: the daily XML for the freshest rates and
    the historical CSV for the series. Either one failing is survivable —
    the daily file alone refreshes the converter, the history alone still
    redraws the chart.
  * Nothing is written unless the parse produced a sane result: rates must
    be positive and finite, the series must be long enough and end recently.
  * A rate that moves more than 10% in a day is rejected as a bad parse
    rather than published. Real currencies do not do that, decimal-comma
    confusion does.
  * Atomic write, so a reader never sees half a file.
  * Non-zero exit on failure, leaving the last good file in place. Every
    widget also carries a baked copy, so a total outage degrades to
    slightly stale numbers rather than an empty screen.
"""

import argparse, csv, io, json, os, re, sys, tempfile, time
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta

DAILY = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
HIST = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"

TIMEOUT = 90
RETRIES = 3
DAILY_DAYS = 400        # every trading day for the last year or so
WEEKLY_YEARS = 12       # weekly beyond that, which the 5y and 10y views use
MAX_DAILY_MOVE = 0.10   # a bad parse, not a currency

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# What the converter offers. The ECB quotes about thirty; these are the ones
# an Indian reader actually converts to or from, and a short list keeps the
# dropdown shorter than the widget is tall.
WANTED = ["INR", "USD", "EUR", "GBP", "AED", "SGD", "AUD", "CAD", "JPY",
          "CHF", "NZD", "SAR", "QAR", "THB", "MYR", "CNY", "HKD", "ZAR"]

# The ECB does not quote pegged Gulf currencies. They are pegged to the USD
# at rates fixed by their own central banks, so the cross is exact rather
# than approximate — but it is still our arithmetic, and it is labelled.
USD_PEGS = {"AED": 3.6725, "SAR": 3.7500, "QAR": 3.6400}


def fetch(url, binary=False):
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "*/*",
                "Accept-Language": "en-GB,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
            return raw if binary else raw.decode("utf-8", "replace")
        except Exception as e:                          # noqa: BLE001
            last = e
            if attempt < RETRIES - 1:
                time.sleep(2 * (attempt + 1))
    raise last


# ------------------------------------------------------------------ daily --
def daily_rates():
    """EUR-based rates, and the ECB's own date for them."""
    root = ET.fromstring(fetch(DAILY))
    # The document is namespaced and the namespace has changed before now, so
    # match on the tag's local name rather than on a namespace that might move.
    out, when = {}, None
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag != "Cube":
            continue
        if el.get("time"):
            when = el.get("time")
        cur, rate = el.get("currency"), el.get("rate")
        if cur and rate:
            try:
                v = float(rate)
            except ValueError:
                continue
            if v > 0:
                out[cur] = v
    if not out or not when:
        raise RuntimeError("daily XML parsed to nothing — format changed?")
    out["EUR"] = 1.0
    return when, out


# ---------------------------------------------------------------- history --
def history():
    """
    The full daily history, as {date: {currency: eur_rate}}.

    The CSV has a trailing empty column and uses 'N/A' for days a currency
    was not quoted, both of which have to be tolerated rather than assumed
    away.
    """
    blob = fetch(HIST, binary=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        text = z.read(name).decode("utf-8", "replace")

    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        d = (row.get("Date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            continue
        day = {}
        for cur, val in row.items():
            if not cur or cur == "Date" or val is None:
                continue
            cur = cur.strip()
            val = val.strip()
            if not cur or not val or val.upper() == "N/A":
                continue
            try:
                v = float(val)
            except ValueError:
                continue
            if v > 0:
                day[cur] = v
        if day:
            out[d] = day
    if len(out) < 1000:
        raise RuntimeError("history parsed to %d days — too few to trust" % len(out))
    return out


def usdinr_series(hist):
    """USD/INR = (EUR/INR) / (EUR/USD), for every day the ECB quoted both."""
    out = []
    for d in sorted(hist):
        day = hist[d]
        usd, inr = day.get("USD"), day.get("INR")
        if not usd or not inr:
            continue
        out.append([d, round(inr / usd, 4)])
    return out


def sane(series):
    """
    Reject the series rather than publish a bad parse.

    A decimal separator read wrong, or a column shifting by one, shows up as
    an impossible one-day move. USD/INR's worst real single day is a few per
    cent; ten is not a currency move, it is a bug.
    """
    if len(series) < 500:
        return "only %d points" % len(series)
    last = datetime.strptime(series[-1][0], "%Y-%m-%d").date()
    if (date.today() - last).days > 10:
        return "newest point is %s, more than 10 days old" % series[-1][0]
    for i in range(1, len(series)):
        a, b = series[i - 1][1], series[i][1]
        if a <= 0 or b <= 0:
            return "non-positive rate at %s" % series[i][0]
        if abs(b - a) / a > MAX_DAILY_MOVE:
            return "implausible %.1f%% move on %s" % ((b / a - 1) * 100, series[i][0])
    return None


def thin(series):
    """
    Daily for the last DAILY_DAYS, weekly before that.

    The chart's 1-month view needs every day; its ten-year view does not, and
    carrying ten years of daily points would quadruple the file for pixels
    nobody can see.
    """
    if not series:
        return []
    last = datetime.strptime(series[-1][0], "%Y-%m-%d").date()
    daily_from = last - timedelta(days=DAILY_DAYS)
    oldest = last - timedelta(days=int(WEEKLY_YEARS * 365.25))

    out, seen_week = [], set()
    for d, v in series:
        day = datetime.strptime(d, "%Y-%m-%d").date()
        if day < oldest:
            continue
        if day >= daily_from:
            out.append([d, v])
            continue
        key = day.isocalendar()[:2]                    # (year, week)
        if key not in seen_week:
            seen_week.add(key)
            out.append([d, v])
    # Whatever the thinning did, the most recent point must survive it.
    if out and out[-1][0] != series[-1][0]:
        out.append(list(series[-1]))
    return out


# ------------------------------------------------------------------- main --
def build():
    problems = []

    try:
        when, rates = daily_rates()
    except Exception as e:                              # noqa: BLE001
        problems.append("daily rates unavailable (%s)" % str(e)[:80])
        when, rates = None, {}

    try:
        hist = history()
        series = usdinr_series(hist)
        bad = sane(series)
        if bad:
            problems.append("series rejected: %s" % bad)
            series = []
    except Exception as e:                              # noqa: BLE001
        problems.append("history unavailable (%s)" % str(e)[:80])
        series = []

    # The daily file is the freshest, but the history's last row carries the
    # same rates and arrives in the same request as ten years of them.
    if not rates and series:
        try:
            newest = max(hist)
            rates = dict(hist[newest]); rates["EUR"] = 1.0; when = newest
        except Exception:                               # noqa: BLE001
            pass

    if not rates:
        raise RuntimeError("; ".join(problems) or "no rates")

    keep = {c: round(v, 6) for c, v in rates.items() if c in WANTED}
    keep["EUR"] = 1.0
    usd = rates.get("USD")
    if usd:
        for cur, per_usd in USD_PEGS.items():
            keep[cur] = round(usd * per_usd, 6)         # EUR->USD->pegged

    missing = [c for c in WANTED if c not in keep]
    if missing:
        problems.append("not quoted today: " + ",".join(missing))

    payload = {"updated": when, "base": "EUR", "rates": keep,
               "usdinr": thin(series)}
    return payload, problems


def main():
    ap = argparse.ArgumentParser(description="Build the forex rate file from ECB data.")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    try:
        payload, problems = build()
    except Exception as e:                              # noqa: BLE001
        sys.exit("fetch failed (%s) — existing file left untouched" % e)

    for p in problems:
        print("warning: " + p, file=sys.stderr)

    # Never replace a good series with an empty one. A partial refresh that
    # keeps the rates current is welcome; one that blanks the chart is not.
    if not payload["usdinr"] and os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                payload["usdinr"] = json.load(fh).get("usdinr", [])
            print("kept the previous USD/INR series", file=sys.stderr)
        except (json.JSONDecodeError, OSError):
            pass

    d = os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, args.out)
    except BaseException:
        os.path.exists(tmp) and os.unlink(tmp)
        raise

    print("%s: %d currencies, %d USD/INR points, ECB date %s"
          % (args.out, len(payload["rates"]), len(payload["usdinr"]),
             payload["updated"]))

    if problems:
        sys.exit("finished with warnings above")


if __name__ == "__main__":
    main()
