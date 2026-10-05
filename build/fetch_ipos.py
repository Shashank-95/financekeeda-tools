#!/usr/bin/env python3
"""
Tops up the IPO tracker's pool. Run it on a schedule; it needs no supervision.

    python3 fetch_ipos.py --out /var/www/data/ipos.json

then point the widget at that file:

    CONFIG.dataUrl = 'https://yoursite.com/data/ipos.json'

Run it every 30 minutes. NSE's offer-documents feed holds only its latest
~17 items, so a slower schedule can let a filing scroll past unread; between
runs the widget rotates its existing pool by itself. A GitHub Action on a
cron works as well as a server cron; the only requirement is that the output
file ends up somewhere the widget can fetch over HTTPS.

--------------------------------------------------------------------------
WHERE THE DATA COMES FROM, AND WHERE IT DELIBERATELY DOES NOT
--------------------------------------------------------------------------
This script never calls NSE's or BSE's website APIs. NSE's terms of use
prohibit "any systematic or automated data collection activities (including
scraping, data mining, data extraction and data harvesting)", and BSE's say
materially the same. Every Python library that offers Indian IPO data —
nsepython, jugaad-data, stock-nse-india and the rest — works by calling those
endpoints anyway. Using one of them puts the breach in your deployment rather
than removing it.

What it does read:

  filings   The default. SEBI's public-issue filings (fetch_sebi.py) plus
            NSE's "Offer Documents" RSS feed (fetch_nse.py). An RSS feed is
            published to be read by software — NSE's RSS page tells readers
            to subscribe with feed-reading software or an aggregator — and
            every document it links to is in NSE's public archive. SEBI posts
            late and has no SME issues; the feed fills both gaps within hours.

  sebi      SEBI's filings alone. No key, no account, no third party, and
            every number traceable to the document it was read from — but
            days or weeks behind, and mainboard only.

  ipoguru   A third-party API. Free tier is evaluation-only; commercial use
            needs a paid plan (from Rs 99/month) and credit to IPO Guru.

  file      A JSON file you maintain or that another job produces.

  url       Any endpoint that already returns the widget's shape.

Not covered: SME issues listed ONLY on BSE. BSE announces their bidding
dates nowhere but its website's IPO pages; its RSS notices carry only the
draft filing and, after the issue has closed, the listing — with the price
but not the dates. Checked against BSE's notices for 29 September to
5 October 2026.

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
    """
    Keep only the fields the widget reads, and only when they are usable.

    Price band, issue size, listing gain and subscription used to be carried
    here. They are gone because SEBI's filings cannot support them — the band
    is not set when the RHP is filed, and gain and subscription are exchange
    data. fetch_sebi.py explains it at length. Anything still holding those
    keys is simply dropped rather than passed through, so a stale file cannot
    resurrect columns the widget no longer renders.
    """
    out = {"name": str(rec.get("name", "")).strip()[:28],
           "kind": str(rec.get("kind", "")).strip()[:10] or "Mainboard",
           "open": iso(rec.get("open")), "close": iso(rec.get("close"))}
    if not out["name"] or not parse(out["close"]):
        return None
    for k in ("stage", "doc", "filed"):
        v = rec.get(k)
        if v not in (None, "", "-"):
            out[k] = str(v).strip()[:200]
    v = rec.get("price")
    if isinstance(v, (int, float)):
        out["price"] = round(float(v), 2)
    return out


# ---------------------------------------------------------------- sources --
def from_file(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("ipos", data if isinstance(data, list) else [])


def from_ipoguru(key):
    """
    IPO Guru's v2 calendar. Commercial use needs a paid plan and credit to
    IPO Guru wherever the data is shown; the free tier is evaluation-only.
    v1 was retired on 30 September 2026.
    """
    req = urllib.request.Request(
        "https://www.ipoguru.in/api/v2/ipos?months=3",
        headers={"X-API-KEY": key, "Accept": "application/json",
                 "User-Agent": "financekeeda-ipo-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        payload = json.load(r)
    out = []
    for x in payload.get("data", []):
        price = None
        # issue_price is filled with the cap while bidding is open; it is only
        # the final price once the issue has closed.
        if x.get("status") == "Closed" or x.get("is_listed"):
            try:
                price = float(str(x.get("issue_price") or "").replace(",", ""))
            except ValueError:
                price = None
        out.append({
            "name": x.get("display_name") or x.get("name"),
            "kind": "SME" if x.get("type") == "SME" else "Mainboard",
            "open": x.get("open_date"), "close": x.get("close_date"),
            "price": price,
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


def from_sebi(existing):
    """
    SEBI's own public-issue filings — the default, and the only source here
    that needs no key, no account and no third party. fetch_sebi.py documents
    what it can and cannot read out of a filing, and why.

    `existing` is passed through so that issues already resolved are not
    fetched again: a resolved record costs nothing, an unresolved one costs a
    PDF.
    """
    from fetch_sebi import collect
    return collect(existing)


def from_filings(existing, nse_state):
    """
    The default: SEBI's filings plus NSE's offer-documents RSS feed.

    SEBI alone was not enough. It posts an RHP days or weeks after filing —
    on 5 October 2026 two mainboard issues were open and SEBI had posted
    neither — and it never posts SME offer documents. NSE's feed announces
    the same documents within hours, SME included. fetch_nse.py explains why
    the feed is fair to read when NSE's website API is not.

    Each source is tried on its own. One failing leaves the other's records
    standing; only both failing counts as a failed fetch.
    """
    import fetch_nse
    out, errors = [], []
    try:
        out += from_sebi(existing)
    except Exception as e:                      # noqa: BLE001
        errors.append("SEBI: %s" % str(e)[:120])
        print("warning: SEBI unavailable (%s) — continuing with NSE" % str(e)[:80], file=sys.stderr)
    try:
        recs, health = fetch_nse.collect(nse_state)
        LAST_HEALTH["nse"] = health
        out += recs
    except Exception as e:                      # noqa: BLE001
        errors.append("NSE: %s" % str(e)[:120])
        print("warning: NSE feed unavailable (%s) — continuing with SEBI" % str(e)[:80], file=sys.stderr)
    LAST_HEALTH["errors"] = errors
    if len(errors) == 2:
        raise RuntimeError("; ".join(errors))
    return out


LAST_HEALTH = {}

SOURCES = {"filings": from_filings, "sebi": from_sebi, "file": from_file,
           "ipoguru": from_ipoguru, "url": from_url}


# ------------------------------------------------------------------ merge --
def find(pool, c):
    """
    The pool record for the same issue, or None.

    Names are matched loosely because two sources rarely spell a company the
    same way — SEBI's "A One Steel" is NSE's "A-One Steels India", SEBI's
    "Manipal Payment and..." is NSE's "Manipal Payment & Identity..." — and an
    exact match would list each such issue twice. The close dates must also
    sit within ten days of each other, so two companies sharing a first word
    are never merged, and a loose match must be the only one.
    """
    from fetch_sebi import match_key, same_company
    if c["name"].lower() in pool:
        return pool[c["name"].lower()]
    k, close = match_key(c["name"]), parse(c["close"])
    hits = []
    for rec in pool.values():
        if not same_company(k, match_key(rec["name"])):
            continue
        other = parse(rec["close"])
        if close and other and abs((close - other).days) > 10:
            continue
        hits.append(rec)
    return hits[0] if len(hits) == 1 else None


def absorb(rec, c, notes):
    """
    Fold a newer reading into an existing record.

    Dates take the newer reading — an issue that extends its bidding window
    files a fresh document saying so. A price is never overwritten: every
    source reads it from a Prospectus, and two readings that disagree mean
    one parse is wrong, which is logged rather than guessed at. "Mainboard"
    is what kind_of() returns when a cover names no SME platform, so it never
    overwrites an SME tag. The name and the audit-trail document stay as
    first recorded, so a row does not change its name on screen.
    """
    for f in ("open", "close"):
        if c.get(f) and rec.get(f) and c[f] != rec[f]:
            notes.append("%s: %s %s -> %s" % (rec["name"], f, rec[f], c[f]))
        if c.get(f):
            rec[f] = c[f]
    if c.get("price") is not None:
        if rec.get("price") is None:
            rec["price"] = c["price"]
            rec["stage"] = "final"
        elif abs(rec["price"] - c["price"]) > 0.001:
            notes.append("%s: price %g kept, a source read %g" % (rec["name"], rec["price"], c["price"]))
    if c.get("kind") and c["kind"] != "Mainboard":
        rec["kind"] = c["kind"]
    for f in ("doc", "filed"):
        if c.get(f) and not rec.get(f):
            rec[f] = c[f]
    if c.get("stage") == "final":
        rec["stage"] = "final"


def merge(existing, incoming, today, notes=None):
    notes = [] if notes is None else notes
    pool = {}
    for rec in existing:
        c = clean(rec)
        if c:
            hit = find(pool, c)
            if hit:                                 # an older pool can hold a pair
                absorb(hit, c, notes)
            else:
                pool[c["name"].lower()] = c
    added = updated = 0
    for rec in incoming:
        c = clean(rec)
        if not c:
            continue
        hit = find(pool, c)
        if hit:
            before = dict(hit)
            absorb(hit, c, notes)
            updated += hit != before
        else:
            pool[c["name"].lower()] = c
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
        # The widget is JS, so keys are bare — quote them and it becomes JSON.
        # Anchored on the brace or comma that precedes a key, because records
        # now carry a `doc` URL and an unanchored \w+: happily rewrites the
        # "https:" inside it.
        j = re.sub(r"([{,]\s*)(\w+)\s*:", r'\1"\2":', line).replace("'", '"')
        try:
            out.append(json.loads(j))
        except json.JSONDecodeError:
            continue
    return out


def main():
    ap = argparse.ArgumentParser(description="Refresh the IPO tracker pool.")
    ap.add_argument("--out", required=True, help="JSON file the widget fetches")
    ap.add_argument("--source", default="filings", choices=sorted(SOURCES))
    ap.add_argument("--nse-state", default="",
                    help="state file for the NSE feed reader (default: beside --out)")
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
        if args.source == "filings":
            nse_state = args.nse_state or os.path.join(
                os.path.dirname(os.path.abspath(args.out)), "nse-feed-state.json")
            incoming = from_filings(existing, nse_state)
        elif args.source == "sebi":
            incoming = from_sebi(existing)
        elif args.source == "file":
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
    except Exception as e:                      # noqa: BLE001
        # Leave the file alone. A stale pool still rotates and still dates
        # itself honestly; a truncated one would show five empty rows.
        # Deliberately broad: SEBI can fail with an IncompleteRead or a
        # markup change as easily as with a URLError, and every one of those
        # has the same right answer — keep the last good file and exit loud.
        sys.exit(f"fetch failed ({e}) — existing pool left untouched")

    notes = []
    pool, added, updated = merge(existing, incoming, today, notes)

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

    # ----------------------------------------------------------------------
    # WHAT COUNTS AS A FAILED RUN
    # ----------------------------------------------------------------------
    # Not an empty market. This used to exit non-zero whenever no issue was
    # open or upcoming, and from 1 October 2026 it did — every run, twice a
    # day, for a pipeline that was working perfectly, because SEBI simply had
    # no new filings after a busy September. A failure email that fires on a
    # quiet week trains whoever receives it to ignore failure emails, which
    # is the worst thing an alert can do. The widget now tells readers "none
    # open or upcoming" on its own, honestly; nothing here needs to shout.
    #
    # What IS a failure is SEBI listing real RHPs that this run did not
    # capture. That is a parse breaking — new wording, a split word, a late
    # posting outside the date window — and it is silent unless checked for:
    # in September it dropped SRIT India and Tempsens Instruments while every
    # run stayed green.
    for n in notes:
        # Shown as an annotation on the run's page: a date that moved is
        # normally an extension, a price two sources disagree on never is.
        print(("::warning::" if "price" in n else "") + "merge: " + n)
    if args.source == "filings":
        h = LAST_HEALTH.get("nse")
        if h:
            print(f"NSE feed health: {h['items']} items, {h['ipo_docs']} IPO documents, "
                  f"{h['read']} read, {h['records']} records; types seen {', '.join(h['types'])}")
        for e in LAST_HEALTH.get("errors", []):
            # One source down is survivable — the other still feeds the pool
            # — but it should be visible on the run, not buried in a log.
            print("::warning::source unavailable this run: " + e)
    if args.source in ("sebi", "filings"):
        import fetch_sebi
        h = getattr(fetch_sebi, "LAST_RUN", {}) or {}
        listed, missing = h.get("listed", 0), h.get("missing", [])
        if h:
            print(f"SEBI health: {listed - len(missing)} of {listed} listed RHPs captured, "
                  f"newest filing {h.get('newest_filed')}")
        if future == 0:
            print("note: no issue is open or upcoming right now — the widget says so "
                  "itself; this is a quiet market, not a failure")
        if missing:
            print("missed: " + ", ".join(missing), file=sys.stderr)
        # One unreadable filing (a scanned PDF with no text layer, say) is a
        # gap worth logging, not a broken parser. Two or more is a pattern.
        if len(missing) >= 2:
            sys.exit(f"ALERT: {len(missing)} of {listed} real RHPs on SEBI's listing "
                     f"were not captured — the parser needs attention: "
                     + ", ".join(missing))


if __name__ == "__main__":
    main()
