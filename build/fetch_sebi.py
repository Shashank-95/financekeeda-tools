#!/usr/bin/env python3
"""
Builds the IPO tracker's pool from SEBI's own filings — the primary source.

Normally driven by fetch_ipos.py, which merges what this returns into the
pool file and writes it. Run directly to see what SEBI currently says:

    python3 fetch_sebi.py --pool public/tools/ipo-tracker/ipos.json > new.json

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
from datetime import datetime, timedelta, timezone

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

# Filled in by collect(): how many real RHPs SEBI listed and which of them did
# not make it into the pool. fetch_ipos.py reads this to decide whether a run
# actually worked — see the health check at the end of collect().
LAST_RUN = {}

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
        # The date regex accepts any capitalised three-letter word, so a typo
        # in one cell would otherwise take down the whole run when the rows
        # are sorted. Parse it here and drop the row if it is not a date.
        try:
            datetime.strptime(d.group(1), "%b %d, %Y")
        except ValueError:
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
    # The workflow installs the latest PyMuPDF on every run, and PyMuPDF has
    # announced that the old `fitz` module name "will be removed in future" —
    # the warning is in every run log since September. On the day it goes,
    # `import fitz` fails and the whole refresh dies. The real package name
    # comes first; `fitz` stays only as a fallback for older installs.
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz                                 # PyMuPDF < 1.24.3
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
    The date after "OPENS" / "CLOSES" (or, in a final Prospectus, "OPENED" /
    "CLOSED"), with or without a following "ON".

    Anchoring on the keyword and taking the next date within 60 characters is
    what makes this survive the markup: real filings write "CLOSES ON#",
    "CLOSES ON(2)(3)", "OPENS ON *" and "OPENS ON:" — matching the marker
    itself fails on the next filing that invents a new one.

    The "ON" is optional because it is not always there. SRIT India's RHP
    reads "BID/ ISSUE OPENS MONDAY, SEPTEMBER 28, 2026" — no "ON" at all —
    and an earlier version of this function required it, so the open date
    never matched, validation failed for want of a pair, and the whole filing
    was dropped in silence. Tempsens Instruments went the same way. What
    keeps this honest is not the "ON" but the date having to appear within a
    few dozen characters of the keyword, which is checked below.

    The one collision worth knowing about is SBI Funds Management, whose
    anchor row reads "ANCHOR INVESTOR BID/OFFER OPENS AND CLOSES ON(1)
    MONDAY, JULY 13, 2026" — a different date entirely, and the only compound
    form that shadows the real one.
    """
    # PDF text extraction occasionally splits a word at a kerning boundary:
    # Tempsens Instruments' RHP comes out as "BID / OFFER CLOSE S ON(1)".
    # Allowing one optional space before the final S catches that without
    # loosening the keyword enough to match anything else.
    #
    # A final Prospectus is written after the event, so the same line reads
    # "BID/OFFER OPENED ON ... CLOSED ON" — 22 of 25 prospectuses checked in
    # October 2026 used the past tense, and the exchanges' feeds deliver the
    # Prospectus long before SEBI posts anything.
    stem = {"OPENS": r"OPEN\s?(?:S|ED)", "CLOSES": r"CLOS\s?(?:ES|ED)"}.get(
        word, re.escape(word[:-1]) + r"\s?" + re.escape(word[-1]))
    for m in re.finditer(r"%s\b(?:\s*ON\b)?" % stem, flat, re.I):
        before = flat[max(0, m.start() - 14):m.start()]
        if re.search(r"OPEN(?:S|ED)?\s+AND\s*$", before, re.I):
            continue
        # "ANCHOR INVESTOR BID/OFFER OPENED AND CLOSED ON(1) ..." (NSE, Hero
        # Motors) is a different, earlier day; so is "Anchor Bid opened on:"
        # (Amtech Esters). The anchor line is skipped, the next one is real.
        if re.match(r"\s+AND\s+CLOS", flat[m.end():], re.I):
            continue
        near = flat[max(0, m.start() - 60):m.start()]
        a = near.upper().rfind("ANCHOR")
        if a >= 0 and not DATE_RE.search(near, a):
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


# The final price is stated on the prospectus cover, in wordings that vary
# from one lead manager's template to the next:
#
#   MV Electrosystems  AT A PRICE OF Rs 425 PER EQUITY SHARE (INCLUDING A SHARE
#                      PREMIUM OF Rs 420 PER EQUITY SHARE) (ISSUE PRICE)
#   A ONE Steels       AT A PRICE OF Rs405.00 PER EQUITY SHARE ("OFFER PRICE")
#                      (INCLUDING A PREMIUM OF Rs395.00 PER EQUITY SHARE)
#   Moneyview          AT A PRICE OF Rs 34 PER EQUITY SHARE INCLUDING A
#                      SECURITIES PREMIUM OF Rs 33 ... (THE "OFFER PRICE")
#   Rays of Belief     AT A PRICE OF Rs 239 PER EQUITY SHARE ("ISSUE PRICE")
#   Runwal             for a cash price at Rs 305 per Equity Share (including
#                      a premium of Rs 303 per Equity Share)
#
# The same pages also quote OTHER per-share prices in the very same grammar —
# pre-IPO and private placements above all:
#
#   German Green Steel  Pre-IPO Placement of 18,38,000 ... Equity Shares at an
#                       issue price of Rs270 per Equity Share (including a
#                       premium of Rs260 ...)          <- the offer was at 139
#   Swastika Infra      private placement of 24,24,242 Equity Shares ... at a
#                       price of Rs165 per Equity Share (including a premium
#                       of Rs155 ...)                  <- the offer was at 185
#
# So no single pattern is safe: one keyed to the label misses Runwal, one
# keyed to the premium picks the placement price (an earlier version of this
# parser did exactly that). Instead every "price of Rs X per equity share" in
# the text is a candidate, and a candidate counts only when
#
#   * it is tied to THE offer — followed closely by the defined term
#     ("ISSUE PRICE"/"OFFER PRICE"), or by a premium that sits exactly one
#     real face value below it; and
#   * the words just before it are not about some other transaction
#     (placement, pre-IPO, anchor, preferential, allotment, transfer).
#
# A premium that is stated but does NOT reconcile to a face value disqualifies
# the candidate outright: that is a number this parser has misread.
# Footnote markers ride on the number itself — NSE wrote "Rs 1,785.00*" and
# SS Retail "Rs 424^" — so they are allowed between the figure and "PER".
PRICE_RE = re.compile(
    r"PRICE\s+(?:OF|AT)\s*(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)\s*[*^#†]*\s*/?-?\s*"
    r"PER\s+EQUITY\s+SHARE", re.I)
PREMIUM_RE = re.compile(
    r"\(?\s*INCLUDING\s+(?:A|THE)\s+(?:SHARE\s+|SECURITIES\s+)?PREMIUM\s+OF\s*"
    r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)", re.I)
# The bracket must open straight onto the label (quotes optional — SS Retail
# has none), so "(ANCHOR INVESTOR OFFER PRICE)" does not count.
LABEL_RE = re.compile(
    r"\(\s*(?:THE\s+)?[\"“”'‘’]?\s*(?:ISSUE|OFFER)\s+PRICE\s*[\"“”'‘’]?\s*\)", re.I)
OTHER_DEAL_RE = re.compile(
    r"PLACEMENT|PRE[\s-]*IPO|ANCHOR|PREFERENTIAL|ALLOT|TRANSFER|BONUS|RIGHTS\s+ISSUE|"
    r"ESOP|EMPLOYEE|ACQUIRED|PURCHASED|WEIGHTED\s+AVERAGE", re.I)

# Face values an Indian listed equity share actually carries.
FACE_VALUES = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0)

LOOK_AHEAD = 200     # chars after the price in which its label/premium sits
LOOK_BEHIND = 160    # chars before it that must not name another deal


def _num(s):
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def final_price(flat):
    """The offer price on the cover, or None if it cannot be pinned down."""
    votes = {}
    found = list(PRICE_RE.finditer(flat))
    for i, m in enumerate(found):
        price = _num(m.group(1))
        if price is None or not (1 <= price <= 100000):
            continue

        # What follows, up to the next quoted price — so a label belonging to
        # the next sentence's price is not credited to this one.
        end = m.end() + LOOK_AHEAD
        if i + 1 < len(found):
            end = min(end, found[i + 1].start())
        after = flat[m.end():end]

        # What precedes, back to the previous quoted price at most.
        start = max(m.start() - LOOK_BEHIND, found[i - 1].end() if i else 0)
        if OTHER_DEAL_RE.search(flat[start:m.start()]):
            continue

        tied = False
        pm = PREMIUM_RE.search(after)
        if pm:
            prem = _num(pm.group(1))
            if prem is None or prem >= price or not any(
                    abs((price - prem) - fv) < 0.01 for fv in FACE_VALUES):
                continue
            tied = True
        if LABEL_RE.search(after):
            tied = True
        if not tied:
            continue

        # The cover price is restated on later pages; the one stated most
        # often wins, and "for cash" in front of it breaks a tie.
        cash = bool(re.search(r"CASH\s+(?:AT\s+A\s+)?$", flat[max(0, m.start() - 20):m.start()], re.I))
        v = votes.setdefault(price, [0, 0])
        v[0] += 1
        v[1] += cash
    if not votes:
        return None
    return max(votes, key=lambda p: (votes[p][0], votes[p][1]))


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
    The guard that stops a misparse reaching readers. Across the filings
    checked, every window was 2-5 days and openings usually fell between one
    day before and twelve days after SEBI posted the document.

    The lower bound is wide on purpose. "Filed" here is the date SEBI POSTED
    the RHP to its listing, not the date the company filed it, and SEBI is
    sometimes late: Tempsens Instruments' RHP appeared on 8 September for an
    issue that opened on 20 August, nineteen days earlier. A -15 floor
    rejected it, silently, and the issue never reached the tracker.

    What actually protects against a misparse is that the date has to sit
    within a few dozen characters of "OPENS"/"CLOSES" and the window has to
    be a real issue window — not this bound. 45 days back still rejects a
    stray financial-year date (31 March is months off), while letting a
    late-posted RHP through.
    """
    if not (opened and closed):
        return False
    if closed < opened:
        return False
    if (closed - opened).days > 21:
        return False
    if not (-45 <= (opened - filed).days <= 120):
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
        # and `fetch_sebi.py --pool x > new.json` stays usable.
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
    # SEBI's RHP page shows only the latest 25 filings, and the Prospectus
    # comes a week or more after the RHP — so by then an issue's RHP has often
    # scrolled off, and the issue is no longer in `out`. Pricing only `out`
    # left Karamtara, Pranav and Rays of Belief unpriced for good although
    # their prospectuses were sitting on SEBI's site. Pool records that have
    # scrolled off are offered for pricing too, and returned if they gain a
    # price; fetch_ipos.py merges the price into the record it already holds.
    carried = [dict(rec) for k, rec in known.items()
               if k not in done and rec.get("open") and rec.get("close")
               and not rec.get("price")]
    resolve_prices(out + carried, known, say)
    out.extend(c for c in carried if c.get("price"))

    # ---- health: did we capture every real RHP SEBI is showing? -----------
    # Addenda and corrigenda are filed under the same heading but carry no
    # offer dates, so they are excluded from the count — their original RHP
    # is what has to be in the pool. Anything else SEBI lists that we did not
    # capture is a parse failure, and that is the signal worth alerting on.
    # It is exactly what went unnoticed when SRIT India and Tempsens were
    # dropped in September: the run was green and two issues were missing.
    real = {company(r["title"]).lower(): r for r in rows
            if not re.search(r"addendum|corrigendum", r["title"], re.I)}
    have = {x["name"].lower() for x in out}
    LAST_RUN.clear()
    LAST_RUN.update({
        "listed": len(real),
        "missing": sorted(k for k in real if k not in have),
        "newest_filed": rows[0]["filed"] if rows else None,
    })
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


def match_key(name):
    """
    The same company is not always spelled the same way in its RHP and its
    Prospectus, and an exact match silently left issues unpriced:

        RHP                       Prospectus
        A One Steel               A ONE Steels
        Adroit Industries         Adroit Industries (India)
        Manipal Payment and...    Manipal Payment & Identity...

    So both are reduced to bare letters and digits — "&" read as "and",
    "(India)" and the truncation mark dropped — and a pair matches when one is
    a prefix of the other. A prefix alone could pair two different companies
    that share a first word, which is why resolve_prices() also requires the
    Prospectus to have been filed around when the issue closed, and refuses a
    match that is not unique.
    """
    s = name.lower().replace("&", " and ").replace("…", "")
    s = re.sub(r"\(\s*india\s*\)", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def same_company(a, b):
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 8 and long_.startswith(short)


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

    recent = datetime.now().date() - timedelta(days=PRICE_WINDOW_DAYS)
    keyed = [(match_key(r["name"]), r) for r in out]

    def find(name, filed):
        k = match_key(name)
        hits = []
        for rk, rec in keyed:
            if not same_company(k, rk):
                continue
            try:
                closed = datetime.strptime(rec["close"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            # A Prospectus is filed after the book closes — allow a little
            # slack either side for SEBI's posting date, no more.
            if -5 <= (filed - closed).days <= 60:
                hits.append(rec)
        exact = [h for h in hits if match_key(h["name"]) == k]
        if exact:
            return exact[0]
        return hits[0] if len(hits) == 1 else None

    pending, seen = [], set()
    for row in finals:
        name = company(row["title"])
        try:
            filed = datetime.strptime(row["filed"], "%b %d, %Y").date()
        except (KeyError, ValueError):
            continue
        rec = find(name, filed)
        if not rec or id(rec) in seen:
            continue
        seen.add(id(rec))

        # A price already known is never looked up again. This is what keeps
        # the steady-state run cheap: prospectuses are ~10 MB each.
        prev = known.get(rec["name"].lower()) or {}
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
        pending.append((row, rec))

    # The download budget used to be spent newest-first, every run. A few
    # recent prospectuses the parser could not read then ate it on every run,
    # and older issues behind them — Karamtara, Rays of Belief — were never
    # reached at all. Starting from a different place each run (the job runs
    # twice a day) means every pending issue gets its turn within a few runs,
    # however many ahead of it keep failing.
    if pending:
        turn = int(datetime.now(timezone.utc).timestamp() // (12 * 3600)) % len(pending)
        pending = pending[turn:] + pending[:turn]

    budget = MAX_PRICE_LOOKUPS
    for row, rec in pending:
        if budget <= 0:
            say("  price lookups capped at %d — the rest resolve next run"
                % MAX_PRICE_LOOKUPS)
            break
        for url in ([row["ap"]] if row["ap"] else []) + documents(row["page"]):
            if budget <= 0:
                break
            try:
                flat = text_of(fetch(url, binary=True), pages=8)
            except Exception as e:
                # A truncated 10 MB download must not spend the budget — the
                # issue is with the transfer, not with the document.
                say("  %-30s prospectus unreadable (%s)" % (rec["name"], str(e)[:40]))
                continue
            budget -= 1
            p = final_price(flat)
            if p:
                rec["price"] = p
                rec["stage"] = "final"
                rec["doc"] = row["page"]
                say("  %-30s priced at Rs %g" % (rec["name"], p))
                break
        else:
            say("  %-30s closed, price not yet stated" % rec["name"])


# ------------------------------------------------------------------- main --
def main():
    import argparse, os
    ap = argparse.ArgumentParser(
        description="Read the IPO pool from SEBI's filings and print it as JSON.",
        epilog="Prints to stdout. fetch_ipos.py is the entry point that merges "
               "the result into a pool file and writes it; this is for looking "
               "at what SEBI currently says.")
    ap.add_argument("--pool", help="an existing pool file, read only so that "
                                   "issues already resolved are not fetched again")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    existing = []
    if args.pool and os.path.exists(args.pool):
        try:
            with open(args.pool, encoding="utf-8") as fh:
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
