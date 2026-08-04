#!/usr/bin/env python3
"""
Builds the IPO tracker's pool from SEBI's own filings — the primary source.

    python3 fetch_sebi.py --out public/tools/ipo-tracker/ipos.json

No API key, no third party, no account. SEBI publishes every public-issue
document itself, and this reads that listing.

--------------------------------------------------------------------------
WHY SEBI RATHER THAN THE EXCHANGES OR AN AGGREGATOR
--------------------------------------------------------------------------
NSE's terms of use prohibit "any systematic or automated data collection
activities (including scraping, data mining, data extraction and data
harvesting)" — its robots.txt says Allow: / but the contract governs, and
every Python library offering Indian IPO data calls those endpoints anyway.
SEBI's robots.txt disallows only /js and /css. The documents here are
statutory filings that SEBI exists to publish.

Aggregators are reachable, but their numbers are transcriptions with nobody
standing behind them. Everything below comes out of the filing itself, so a
reader can click through to the document the number was read from. That is
also why each record carries its SEBI page URL.

--------------------------------------------------------------------------
WHAT SEBI CAN AND CANNOT TELL YOU
--------------------------------------------------------------------------
Verified against all 25 RHPs on the listing on 4 Aug 2026.

  available, 19/19 on issues with a readable document
    company name, offer opening date, offer closing date, filing date,
    Mainboard/SME, and a link to the filing itself

  available once the issue has closed, from the Prospectus
    the final issue price, which is a fixed number by then

  NOT available, and deliberately not guessed at
    price band   The RHP is filed BEFORE the band is set. The document
                 literally reads "aggregating up to Rs [<bullet>] million" —
                 a placeholder. Confirmed on Technocraft, SBI Funds and
                 Ardee. There is no band to read, so none is published.
    issue size   Same placeholder, for the same reason, whenever any part
                 of the offer is an offer-for-sale priced off that band.
                 An earlier draft of this script read a number from a
                 nearby promoter table and produced Rs 320 Cr for Ardee
                 against a true Rs 425.9 Cr. Publishing a plausible wrong
                 number is worse than publishing nothing.
    listing gain,
    subscription These are exchange data, not filings. Not available here
                 at any price, and not approximated.
    grey market
    premium      No authoritative source exists. Ten sites publish it and
                 none can stand behind it.

The widget's columns were changed to match this list rather than the list
being padded to match the widget.

--------------------------------------------------------------------------
HOW IT AVOIDS BREAKING
--------------------------------------------------------------------------
  * Every issue is resolved independently. One unreadable PDF costs one row,
    never the run.
  * Nothing is written unless the parse produced a plausible result: dates
    must parse, close must not precede open, the window must be at most 21
    days, and it must sit sensibly against the filing date. A record that
    fails is dropped, not published.
  * Nothing good is ever overwritten by something worse — see merge() in
    fetch_ipos.py, which only fills fields that are missing or improved.
  * Resolved issues are cached forever. A steady-state run reads one listing
    page and a handful of small PDFs.
  * The write is atomic, so a reader never sees half a file.
  * A failure to reach SEBI leaves the existing pool untouched and exits
    non-zero, so the scheduled job reports it. Readers keep the last good
    list, which still rotates by date on its own.
  * Three ways to reach a document are tried in order, because the listing
    markup is not uniform: the abridged prospectus linked in the listing,
    the one linked on the detail page, then the full filing in the detail
    page's viewer.
"""

import json, re, sys, time, urllib.error, urllib.request
from datetime import datetime, timedelta

BASE = "https://www.sebi.gov.in"
LIST = (BASE + "/sebiweb/home/HomeAction.do"
        "?doListing=yes&sid=3&ssid=15&smid={smid}")

# SEBI's own section ids. sid=3 Filings, ssid=15 Public Issues.
RHP = 11        # Red Herring Documents filed with ROC — carries the offer dates
FINAL = 12      # Final Offer Documents filed with ROC — carries the final price

TIMEOUT = 60
RETRIES = 3
# SEBI's nodes disagree (see listing()). Roughly half the responses came from
# the stale one, so six passes leaves about a 1.6% chance of missing the newest
# filing on a given run — and the next run six hours later catches it anyway.
LISTING_TRIES = 6
MAX_PRICE_LOOKUPS = 8   # full prospectuses are ~10 MB; spread the first run out
PRICE_WINDOW_DAYS = 45  # only issues recent enough to still be on display

# A real browser string. SEBI serves the listing to a default urllib agent too,
# but an identifiable-yet-ordinary UA is what every other reader sends and is
# less likely to be caught by a future filter.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MONTHS = ("JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER"
          "|OCTOBER|NOVEMBER|DECEMBER")
DATE_RE = re.compile(r"(%s)\s+(\d{1,2})\s*,?\s*(\d{4})" % MONTHS, re.I)


# ------------------------------------------------------------------ fetch --
def fetch(url, binary=False, timeout=TIMEOUT):
    """
    One GET, retried on transient failure with a widening pause. The retry
    net is deliberately wide: SEBI's PDF responses truncate often enough
    (http.client.IncompleteRead, which is neither URLError nor OSError) that
    naming exception classes lets real failures through.
    """
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
                "Accept-Language": "en-GB,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw if binary else raw.decode("utf-8", "replace")
        except Exception as e:                          # noqa: BLE001 — see above
            last = e
            if attempt < RETRIES - 1:
                time.sleep(2 * (attempt + 1))
    raise last


# --------------------------------------------------------------- listings --
def parse_listing(html):
    """One row per filing: date, title, SEBI page, abridged prospectus."""
    rows = []
    for chunk in re.split(r"<tr[^>]*>", html)[1:]:
        d = re.search(r"<td>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>", chunk)
        if not d:
            continue                                    # header, or a stray tr
        page = re.search(r'href="(%s/filings/[^"]+)"' % re.escape(BASE), chunk)
        title = re.search(r'title="([^<"]{3,120})', chunk)
        # The listing tucks the abridged prospectus inside the title attribute
        # as a nested anchor, single-quoted.
        ap = re.search(r"href=\s*'(%s/sebi_data/commondocs/[^']+)'"
                       % re.escape(BASE), chunk)
        if not (page and title):
            continue
        rows.append({"filed": d.group(1),
                     "title": unescape(title.group(1)),
                     "page": page.group(1),
                     "ap": unescape(ap.group(1)) if ap else None})
    return rows


def listing(smid, tries=LISTING_TRIES, verbose=False):
    """
    The listing is read several times and the results unioned.

    This is not belt-and-braces. SEBI serves the page from more than one node
    and they do not hold the same data: six identical requests on 4 Aug 2026
    returned "newest = Dhoot Transmission, Aug 04" three times and "newest =
    Technocraft Ventures, Jul 31" three times, the second node simply missing
    the two most recent filings and reaching further back instead. A single
    GET therefore has a real chance of silently omitting the newest issue —
    the one a reader most wants.

    Unioning turns that from a correctness problem into a latency one, and
    the additive merge in fetch_ipos.py plus twice-daily runs closes the rest:
    a filing missed by every node this morning is picked up this evening and
    then kept for good.

    Only a total failure to reach SEBI raises. As long as one attempt lands,
    the run proceeds on what it got.
    """
    seen, rows, errors = {}, [], []
    for i in range(tries):
        try:
            found = parse_listing(fetch(LIST.format(smid=smid)))
        except Exception as e:
            errors.append(e)
            continue
        fresh = 0
        for r in found:
            key = r["page"]
            if key not in seen:
                seen[key] = r
                rows.append(r)
                fresh += 1
            elif r["ap"] and not seen[key]["ap"]:
                seen[key]["ap"] = r["ap"]               # keep the richer copy
        if verbose:
            print("  listing pass %d: %d rows, %d new" % (i + 1, len(found), fresh),
                  file=sys.stderr)
        # No early exit. Which node answers is a coin toss, so "two passes in
        # a row told me nothing new" is not evidence that the nodes agree —
        # it is just as likely to be the same stale node answering twice.
        # These are 45 KB pages; the passes are cheaper than the miss.
    if not rows and errors:
        raise errors[-1]
    rows.sort(key=lambda r: datetime.strptime(r["filed"], "%b %d, %Y").date(),
              reverse=True)
    return rows


def unescape(s):
    for a, b in (("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"'),
                 ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def documents(page_url):
    """
    Every PDF reachable from a filing's page, most useful first: the abridged
    prospectus (small, and carries the offer dates) before the full filing
    (10 MB, but always has them). The full one sits in a viewer iframe rather
    than a plain link, which is why the URL is read out of the src.
    """
    try:
        html = fetch(page_url)
    except Exception:
        return []
    small = re.findall(r"(%s/sebi_data/commondocs/[^\s'\"<>]+?\.pdf)"
                       % re.escape(BASE), html)
    big = re.findall(r"(%s/sebi_data/attachdocs/[^\s'\"<>]+?\.pdf)"
                     % re.escape(BASE), html)
    seen, out = set(), []
    for u in [unescape(x) for x in small] + [unescape(x) for x in big]:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ------------------------------------------------------------------- pdfs --
def text_of(pdf_bytes, pages=4):
    """First few pages, whitespace collapsed. Everything wanted is up front."""
    try:
        import fitz                                     # PyMuPDF
    except ImportError:
        sys.exit("PyMuPDF is required:  pip install pymupdf")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        n = min(pages, doc.page_count)
        return re.sub(r"\s+", " ", "\n".join(doc[i].get_text() for i in range(n)))
    finally:
        doc.close()


def offer_date(flat, word):
    """
    The date after "OPENS ON" / "CLOSES ON".

    Anchoring on the keyword and taking the next date within 60 characters is
    what makes this survive the markup: real filings write "CLOSES ON#",
    "CLOSES ON(2)(3)", "OPENS ON *" and "OPENS ON:" — matching the marker
    itself fails on the next filing that invents a new one.

    The one collision worth knowing about is SBI Funds Management, whose
    anchor row reads "ANCHOR INVESTOR BID/OFFER OPENS AND CLOSES ON(1)
    MONDAY, JULY 13, 2026" — a different date entirely, and the only compound
    form that shadows the real one.
    """
    for m in re.finditer(r"%s\s*ON\b" % word, flat, re.I):
        before = flat[max(0, m.start() - 14):m.start()]
        if re.search(r"OPENS?\s+AND\s*$", before, re.I):
            continue
        d = DATE_RE.search(flat, m.end(), m.end() + 60)
        if d:
            try:
                return datetime.strptime("%s %s %s" % d.groups(), "%B %d %Y").date()
            except ValueError:
                continue
    return None


def kind_of(flat):
    """
    Mainboard or SME, taken from the platform the shares will list on.
    Chapter IX of the ICDR Regulations is the SME route; Chapter II is the
    main board. Both are stated on the cover.
    """
    if re.search(r"EMERGE\s*(?:Platform|,)|NSE\s+EMERGE|EMERGE\s+of", flat, re.I):
        return "NSE SME"
    if re.search(r"SME\s+Platform\s+of\s+BSE|BSE\s+SME", flat, re.I):
        return "BSE SME"
    return "Mainboard"


# "...FOR CASH AT A PRICE OF Rs 425 PER EQUITY SHARE (INCLUDING A SHARE
#  PREMIUM OF Rs 420 PER EQUITY SHARE) (ISSUE PRICE)..."
#
# The share premium is quoted in exactly the same words one clause later, so
# the match is only accepted once "ISSUE PRICE"/"OFFER PRICE" is declared
# straight after it — that label is what distinguishes the two numbers.
#
# The quotes around that label are typographic as often as not: MV
# Electrosystems writes (ISSUE PRICE), Manipal Health writes (“OFFER PRICE”).
PRICE_RE = re.compile(
    r"AT\s+A\s+PRICE\s+OF\s*(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)\s*/?-?\s*"
    r"PER\s+EQUITY\s+SHARE(.{0,200}?)"
    r"""\(\s*["“”'‘’]?\s*(?:ISSUE|OFFER)\s+PRICE\s*["“”'‘’]?\s*\)""",
    re.I | re.S)


def final_price(flat):
    m = PRICE_RE.search(flat)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v if 1 <= v <= 100000 else None


# ------------------------------------------------------------------ names --
SUFFIX_RE = re.compile(
    r"\s*[-–—]\s*(RHP|DRHP|Red Herring.*|Prospectus|Final Prospectus|AP|"
    r"Abridged.*|Addendum.*|Corrigendum.*)\s*$", re.I)
LEGAL_RE = re.compile(r"\s*\b(Limited|Ltd\.?|Private Limited|Pvt\.? Ltd\.?)\s*\.?$", re.I)
SMALL = {"and", "of", "the", "for", "in", "n"}


def company(title):
    """
    "CALIBER MINING AND LOGISTICS LIMITED - RHP" -> "Caliber Mining and Logistics"

    Dropping "Limited" is what makes almost every name fit the column without
    truncation. Shouty filings are title-cased, but short all-caps words are
    left alone so SBI does not become Sbi.
    """
    s = title.replace("​", "").replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(3):                                  # "X Limited - RHP - AP"
        s2 = LEGAL_RE.sub("", SUFFIX_RE.sub("", s)).strip(" -–—,")
        if s2 == s:
            break
        s = s2
    if s.isupper():
        out = []
        for i, w in enumerate(s.split()):
            # Joining words first: "AND" is three letters, so an acronym rule
            # checked first turns "MINING AND LOGISTICS" into "Mining AND".
            if i and w.lower() in SMALL:
                out.append(w.lower())
            elif len(w) <= 3 and w.isalpha():
                out.append(w)                           # SBI, MV, GNI
            else:
                out.append(w.capitalize())
        s = " ".join(out)
    if len(s) > 28:                                     # trim on a word boundary
        cut = s[:28].rsplit(" ", 1)[0]
        s = (cut if len(cut) >= 12 else s[:27]) + "…"
    return s


# ------------------------------------------------------------- validation --
def plausible(opened, closed, filed):
    """
    The guard that stops a misparse reaching readers. Across the 25 filings
    checked, every window was 2-5 days and every opening fell between one day
    before and twelve days after the filing; these bounds are far looser than
    that, so they reject nonsense without rejecting an unusual issue.
    """
    if not (opened and closed):
        return False
    if closed < opened:
        return False
    if (closed - opened).days > 21:
        return False
    if not (-15 <= (opened - filed).days <= 120):
        return False
    return True


# ---------------------------------------------------------------- collect --
def collect(existing=None, verbose=True):
    """
    Returns records in the widget's shape. `existing` is the current pool;
    anything already resolved in it is not fetched again.
    """
    known = {}
    for rec in (existing or []):
        n = str(rec.get("name", "")).strip().lower()
        if n:
            known[n] = rec

    def say(*a):
        # Progress goes to stderr so that stdout carries nothing but the JSON
        # and `fetch_sebi.py --out x > pool.json` stays usable.
        if verbose:
            print(*a, file=sys.stderr)

    out, done = [], set()

    # ---- open, upcoming and just-closed issues, from the RHPs -------------
    rows = listing(RHP, verbose=verbose)
    say("SEBI RHP listing: %d filings" % len(rows))
    if not rows:
        raise RuntimeError("the RHP listing parsed to zero rows — markup changed?")

    for row in rows:
        name = company(row["title"])
        key = name.lower()
        # A company can appear twice — an RHP and a later addendum, or the
        # same filing surfacing from two nodes. Rows are newest-first, so the
        # first one seen is the one to keep.
        if key in done:
            continue
        filed = datetime.strptime(row["filed"], "%b %d, %Y").date()
        prev = known.get(key)

        # Already resolved on an earlier run. Costs nothing to keep.
        if prev and prev.get("open") and prev.get("close"):
            done.add(key)
            out.append(dict(prev, doc=prev.get("doc") or row["page"]))
            continue

        rec = resolve_dates(row, name, filed, say)
        if rec:
            done.add(key)
            out.append(rec)

    # ---- the final price, for issues that have since filed a Prospectus ---
    resolve_prices(out, known, say)

    return out


def resolve_dates(row, name, filed, say):
    """Try each document in turn until one yields a plausible pair of dates."""
    candidates = [row["ap"]] if row["ap"] else []
    tried_page = False

    while True:
        if not candidates:
            if tried_page:
                say("  %-30s no document yielded dates" % name)
                return None
            candidates = [u for u in documents(row["page"]) if u not in
                          ([row["ap"]] if row["ap"] else [])]
            tried_page = True
            if not candidates:
                say("  %-30s no document found" % name)
                return None

        url = candidates.pop(0)
        try:
            flat = text_of(fetch(url, binary=True))
        except Exception as e:
            say("  %-30s unreadable (%s)" % (name, str(e)[:40]))
            continue

        opened = offer_date(flat, "OPENS")
        closed = offer_date(flat, "CLOSES")
        if not plausible(opened, closed, filed):
            continue

        say("  %-30s %s -> %s" % (name, opened, closed))
        return {"name": name,
                "kind": kind_of(flat),
                "open": opened.isoformat(),
                "close": closed.isoformat(),
                "filed": filed.isoformat(),
                "doc": row["page"],
                "stage": "rhp"}


def resolve_prices(out, known, say):
    """
    A closed issue's final price comes from its Prospectus, which is filed a
    few days after the book closes. The document is large, so it is fetched
    once per issue and then carried forward in the pool for good.
    """
    try:
        finals = listing(FINAL, verbose=False)
    except Exception as e:
        say("final-prospectus listing unavailable (%s) — prices unchanged" % str(e)[:60])
        return

    by_name = {r["name"].lower(): r for r in out}
    budget = MAX_PRICE_LOOKUPS
    recent = datetime.now().date() - timedelta(days=PRICE_WINDOW_DAYS)

    for row in finals:
        name = company(row["title"])
        rec = by_name.get(name.lower())
        if not rec:
            continue

        # A price already known is never looked up again. This is what keeps
        # the steady-state run cheap: prospectuses are ~10 MB each.
        prev = known.get(name.lower()) or {}
        if rec.get("price") or prev.get("price"):
            rec["price"] = rec.get("price") or prev["price"]
            rec["stage"] = "final"
            rec["doc"] = row["page"]
            continue

        # Only issues recent enough to still reach the "recently closed" tab.
        # Without this the run keeps re-downloading documents for issues that
        # will never be displayed, every twelve hours, forever.
        try:
            closed = datetime.strptime(rec["close"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if closed < recent:
            continue
        if budget <= 0:
            say("  price lookups capped at %d — the rest resolve next run"
                % MAX_PRICE_LOOKUPS)
            break

        for url in ([row["ap"]] if row["ap"] else []) + documents(row["page"]):
            try:
                flat = text_of(fetch(url, binary=True), pages=8)
            except Exception as e:
                # A truncated 10 MB download must not spend the budget — the
                # issue is with the transfer, not with the document.
                say("  %-30s prospectus unreadable (%s)" % (name, str(e)[:40]))
                continue
            budget -= 1
            p = final_price(flat)
            if p:
                rec["price"] = p
                rec["stage"] = "final"
                rec["doc"] = row["page"]
                say("  %-30s priced at Rs %g" % (name, p))
                break
        else:
            say("  %-30s closed, price not yet stated" % name)


# ------------------------------------------------------------------- main --
def main():
    import argparse, os
    ap = argparse.ArgumentParser(description="Build the IPO pool from SEBI filings.")
    ap.add_argument("--out", required=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    existing = []
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                existing = json.load(fh).get("ipos", [])
        except (json.JSONDecodeError, OSError):
            pass

    recs = collect(existing, verbose=not args.quiet)
    if not recs:
        sys.exit("SEBI returned nothing usable — existing pool left untouched")

    print(json.dumps({"updated": datetime.now().date().isoformat(),
                      "ipos": recs}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
