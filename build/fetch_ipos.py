#!/usr/bin/env python3
"""
Tops up the IPO tracker's pool. Run it on a schedule; it needs no supervision.

    python3 fetch_ipos.py --out /var/www/data/ipos.json

then point the widget at that file:

    CONFIG.dataUrl = 'https://yoursite.com/data/ipos.json'

Twice a week is plenty — mainboard issues are announced weeks ahead, and the
widget rotates its existing pool by itself between runs. A GitHub Action on a
cron works as well as a server cron; the only requirement is that the output
file ends up somewhere the widget can fetch over HTTPS from your own origin.

--------------------------------------------------------------------------
WHERE THE DATA COMES FROM, AND WHERE IT DELIBERATELY DOES NOT
--------------------------------------------------------------------------
This script never touches nseindia.com or bseindia.com. NSE's terms of use
prohibit "any systematic or automated data collection activities (including
scraping, data mining, data extraction and data harvesting)", and BSE's say
materially the same. Every Python library that offers Indian IPO data —
nsepython, jugaad-data, stock-nse-india and the rest — works by calling those
endpoints anyway. Using one of them puts the breach in your deployment rather
than removing it.

What is available:

  ipoguru   A third-party API whose published terms state plainly that
            commercial use is permitted with attribution. Free key, issued
            by email, 300 requests a day. Caveat worth knowing: they do not
            disclose their upstream source, so you are relying on their
            terms rather than on a chain you can audit.

  file      A JSON file you maintain or that another job produces. Zero
            dependency, zero terms question. If you would rather one person
            spend ten minutes a week than carry a third-party dependency,
            this is a legitimate answer, not a fallback.

IPO dates, price band, lot size and issue size are public record — they are in
the RHP and the company's own announcements. What the exchange terms restrict
is automated collection from THEIR site, not the underlying facts. Sourcing
the same calendar from filings is clean; scraping it off the exchange is not,
even though the numbers are identical.

--------------------------------------------------------------------------
WHAT IT DOES TO THE POOL
--------------------------------------------------------------------------
Merges rather than replaces, keyed on the company name:

  * new issues are added
  * existing ones are updated in place, so a listing price or a subscription
    figure fills in as it becomes known
  * issues that closed more than KEEP_DAYS ago are dropped, which is what
    stops the file growing without limit
  * closed issues inside that window are KEPT, because they are what the
    "recently closed" tab shows

It writes atomically and exits non-zero on two conditions, so a cron that
mails on error tells you about both: the fetch failing, and the pool running
out of open or upcoming issues. The second is the one that matters — a merge
is additive, so a silent feed never empties the pool, it just stops topping it
up, and the failure mode is the tracker quietly ageing into its empty state.
"""

import argparse, json, os, pathlib, sys, tempfile, urllib.request, urllib.error
from datetime import date, datetime, timedelta

KEEP_DAYS = 60          # how long a closed issue stays in the "recently closed" tab
TIMEOUT   = 20


# ---------------------------------------------------------------- helpers --
def iso(d):
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d or "")[:10]


def parse(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def clean(rec):
    """Keep only the fields the widget reads, and only when they are usable."""
    out = {"name": str(rec.get("name", "")).strip()[:28],
           "kind": str(rec.get("kind", "")).strip()[:10] or "Mainboard",
           "open": iso(rec.get("open")), "close": iso(rec.get("close"))}
    if not out["name"] or not parse(out["close"]):
        return None
    for k in ("listed", "band", "size", "subs"):
        v = rec.get(k)
        if v not in (None, "", "-"):
            out[k] = iso(v) if k == "listed" else str(v).strip()[:16]
    for k in ("price", "gain"):
        v = rec.get(k)
        if isinstance(v, (int, float)):
            out[k] = round(float(v), 2)
    return out


# ---------------------------------------------------------------- sources --
def from_file(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("ipos", data if isinstance(data, list) else [])


def from_ipoguru(key):
    """
    Their published terms permit commercial use with attribution. Field names
    are normalised here rather than in the widget, so swapping the source
    later touches this function and nothing else.
    """
    # Base URL and header spelling taken from their published API page. An
    # api.* subdomain looks right and does not resolve — worth checking rather
    # than assuming, because the failure only shows up once a key exists.
    req = urllib.request.Request(
        "https://www.ipoguru.in/api/v1/ipos",
        headers={"X-API-KEY": key, "Accept": "application/json",
                 "User-Agent": "financekeeda-ipo-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        payload = json.load(r)
    rows = payload.get("data", payload if isinstance(payload, list) else [])
    out = []
    for x in rows:
        out.append({
            "name":  x.get("ipo_name") or x.get("name"),
            "kind":  (x.get("ipo_type") or "").strip() or "Mainboard",
            "open":  x.get("open_date"), "close": x.get("close_date"),
            "listed": x.get("listing_date"),
            "band":  x.get("price_band"), "size": x.get("issue_size"),
            "price": x.get("issue_price"), "subs": x.get("total_subscription"),
            # Deliberately NOT importing grey market premium even where the
            # feed carries it. No authoritative source, nothing to stand behind.
        })
    return out


def from_url(url):
    """
    Any endpoint that already returns the widget's shape — your own scraper,
    a colleague's feed, a paid provider. Keeps you from being locked to one
    supplier: when a source goes bad, you point --url somewhere else rather
    than rewriting this script.
    """
    req = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "financekeeda-ipo-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
    return data.get("ipos", data if isinstance(data, list) else [])


SOURCES = {"file": from_file, "ipoguru": from_ipoguru, "url": from_url}


# ------------------------------------------------------------------ merge --
def merge(existing, incoming, today):
    pool = {}
    for rec in existing:
        c = clean(rec)
        if c:
            pool[c["name"].lower()] = c
    added = updated = 0
    for rec in incoming:
        c = clean(rec)
        if not c:
            continue
        k = c["name"].lower()
        if k in pool:
            before = dict(pool[k])
            pool[k].update({a: b for a, b in c.items() if b not in (None, "")})
            updated += pool[k] != before
        else:
            pool[k] = c
            added += 1

    cutoff = today - timedelta(days=KEEP_DAYS)
    kept = [v for v in pool.values() if (parse(v["close"]) or today) >= cutoff]
    kept.sort(key=lambda v: v["close"], reverse=True)
    return kept, added, updated


def seed_from_widget(path):
    """Lift the baked POOL out of the widget so run one starts with history."""
    import ast, re
    try:
        html = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"ipos:\s*\[(.*?)\n    \]", html, re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        # the widget is JS, so keys are bare — quote them, then it is JSON
        j = re.sub(r"(\w+):", r'"\1":', line).replace("'", '"')
        try:
            out.append(json.loads(j))
        except json.JSONDecodeError:
            continue
    return out


def main():
    ap = argparse.ArgumentParser(description="Refresh the IPO tracker pool.")
    ap.add_argument("--out", required=True, help="JSON file the widget fetches")
    ap.add_argument("--source", default="file", choices=sorted(SOURCES))
    ap.add_argument("--key", default=os.environ.get("IPO_API_KEY", ""),
                    help="API key, for sources that need one")
    ap.add_argument("--input", help="path, for --source file")
    ap.add_argument("--url", help="endpoint, for --source url")
    ap.add_argument("--seed", help="widget HTML to lift the baked pool from, "
                                   "used only when --out does not exist yet")
    args = ap.parse_args()

    today = date.today()

    existing = []
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                existing = json.load(fh).get("ipos", [])
        except (json.JSONDecodeError, OSError) as e:
            print(f"warning: existing pool unreadable ({e}) — starting fresh", file=sys.stderr)
    elif args.seed:
        # First run. Without this the "recently closed" tab would start empty,
        # because a feed of upcoming issues carries no history.
        existing = seed_from_widget(args.seed)
        print(f"seeded {len(existing)} issues from {args.seed}")

    try:
        if args.source == "file":
            if not args.input:
                sys.exit("--input is required with --source file")
            incoming = from_file(args.input)
        elif args.source == "url":
            if not args.url:
                sys.exit("--url is required with --source url")
            incoming = from_url(args.url)
        else:
            if not args.key:
                sys.exit(f"--key (or IPO_API_KEY) is required with --source {args.source}")
            incoming = from_ipoguru(args.key)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        # Leave the file alone. A stale pool still rotates and still dates
        # itself honestly; a truncated one would show five empty rows.
        sys.exit(f"fetch failed ({e}) — existing pool left untouched")

    pool, added, updated = merge(existing, incoming, today)

    future = sum(1 for v in pool if (parse(v["close"]) or today) >= today)

    payload = {"updated": iso(today), "ipos": pool}
    d = os.path.dirname(os.path.abspath(args.out)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, args.out)          # atomic: readers never see a half file
    except BaseException:
        os.path.exists(tmp) and os.unlink(tmp)
        raise

    print(f"{args.out}: {len(pool)} issues ({future} open or upcoming), "
          f"+{added} new, {updated} updated")

    # The merge is additive, so a quiet feed can never empty the pool — it just
    # stops topping it up. What CAN happen is the pool ageing out from under
    # you, and that is the state worth waking someone for: the widget will be
    # showing "no open issues left" to readers.
    if future == 0:
        sys.exit("ALERT: no open or upcoming issues left — the tracker is showing "
                 "its empty state to readers")
    if future <= 2:
        print(f"warning: only {future} open or upcoming issues left in the pool",
              file=sys.stderr)


if __name__ == "__main__":
    main()
