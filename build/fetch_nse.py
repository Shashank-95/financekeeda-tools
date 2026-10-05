#!/usr/bin/env python3
"""
Reads NSE's "Offer Documents" RSS feed — the exchange's own announcement of
every IPO document as it is filed — and turns each one into a pool record.

    python3 fetch_nse.py --state nse-state.json        # print what it finds

fetch_ipos.py is the entry point that merges the result into the pool; this
is for looking at what the feed currently says.

--------------------------------------------------------------------------
WHY THIS FEED, AND NOT NSE'S WEBSITE
--------------------------------------------------------------------------
SEBI posts a Red Herring Prospectus days or weeks after it is filed —
Nityas Gems & Jewellery and Vishal Nirmiti opened on 30 September 2026 with
SEBI's newest RHP dated the 24th — and never posts SME offer documents at all.
NSE announces the same documents within hours, SME included.

NSE's terms of use forbid automated collection from its website, so its
issue-calendar API is off limits. The RSS feeds are a different thing: NSE
publishes them for exactly this, and its RSS page invites readers to
subscribe "by downloading feed reading software" or through "any online
aggregator of your choice". The documents each item links to sit in NSE's
public archive. Nothing here touches www.nseindia.com/api.

--------------------------------------------------------------------------
WHAT AN ITEM LOOKS LIKE
--------------------------------------------------------------------------
    title        Shah Investor's Home Limited
    description  Shah Investor's Home Limited has filled PROSP for its IPO
    link         https://nsearchives.nseindia.com/corporate/FP_INE029N01014_01OCT2026.pdf

The type word carries the meaning. Seen so far:

    ADV      price-band or post-issue advertisement — carries the dates
    PROSP    final Prospectus — carries the dates AND the final price
    DP_DRHP  draft, months ahead, no dates — skipped

plus trust deeds and commercial-paper disclosures that are not IPOs at all.
Any other "for its IPO" type is read, because a document type nobody has seen
yet is worth a look; one that yields no plausible dates is simply recorded.

--------------------------------------------------------------------------
THE FEED ONLY HOLDS THE LATEST ~17 ITEMS
--------------------------------------------------------------------------
So it is read often (the workflow runs every 30 minutes) and every item seen
is remembered in a small state file, which is what lets a document that
failed to download be retried after it has scrolled off the feed. A missed
item is not fatal: each IPO files several documents (advert before opening,
Prospectus after closing), and mainboard issues come through SEBI as well.
"""

import io, json, os, re, sys, tempfile, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import fetch_sebi as S

FEED = "https://nsearchives.nseindia.com/content/RSS/Offer_Documents.xml"
FEED_TRIES = 3          # union of reads, as with SEBI's load-balanced listing
MAX_DOCS = 12           # documents read per run; the rest wait for the next
RETRY_LIMIT = 5         # download attempts before a document is given up on
FORGET_DAYS = 30        # how long a seen item is remembered

TYPE_RE = re.compile(r"has\s+fil(?:l)?ed\s+(\S+)\s+for\s+its\s+IPO", re.I)
SKIP_TYPE_RE = re.compile(r"DRHP|^DP_|DRAFT", re.I)


def _say(*a):
    print(*a, file=sys.stderr)


def read_feed(tries=FEED_TRIES):
    """Every IPO-document item in the feed, newest first. Raises only if no
    read succeeds at all."""
    seen, errors = {}, []
    for _ in range(tries):
        try:
            root = ET.fromstring(S.fetch(FEED, binary=True))
        except Exception as e:                      # noqa: BLE001
            errors.append(e)
            continue
        for it in root.iter("item"):
            title, desc, link, pub = [(it.findtext(k) or "").strip()
                                      for k in ("title", "description", "link", "pubDate")]
            if link and link not in seen:
                seen[link] = {"title": title, "desc": desc, "link": link, "pub": pub}
    if not seen and errors:
        raise errors[-1]
    return list(seen.values())


def item_type(item):
    """The document type, or None if the item is not an IPO document."""
    m = TYPE_RE.search(item["desc"])
    return m.group(1).upper() if m else None


def filed_date(item):
    """NSE stamps items "01-Oct-2026". Today stands in if that ever changes —
    it only feeds the plausibility window, which is weeks wide."""
    m = re.search(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", item["pub"])
    if m:
        try:
            return datetime.strptime("-".join(m.groups()), "%d-%b-%Y").date()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def pdfs_in(blob):
    """The PDF itself, or the PDFs inside a zip, largest first."""
    if blob[:4] == b"%PDF":
        return [blob]
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = sorted((n for n in z.namelist() if n.lower().endswith(".pdf")),
                           key=lambda n: -z.getinfo(n).file_size)
            return [z.read(n) for n in names[:3]]
    return []


def record_from(item, typ, flat):
    """A pool record, or None if the document does not pin the issue down."""
    filed = filed_date(item)
    opened = S.offer_date(flat, "OPENS")
    closed = S.offer_date(flat, "CLOSES")
    if not S.plausible(opened, closed, filed):
        return None
    rec = {"name": S.company(item["title"]),
           "kind": S.kind_of(flat),
           "open": opened.isoformat(),
           "close": closed.isoformat(),
           "filed": filed.isoformat(),
           "doc": item["link"],
           "stage": "rhp"}
    # Only a final Prospectus states the price the issue was allotted at. A
    # price-band advert quotes a floor and a cap, and reading a price out of
    # one would print the cap as if it were final.
    if "PROSP" in typ:
        p = S.final_price(flat)
        if p:
            rec["price"] = p
            rec["stage"] = "final"
    return rec


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st.get("seen"), dict) else {"seen": {}}
    except (OSError, ValueError, AttributeError):
        return {"seen": {}}


def save_state(path, st):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        os.path.exists(tmp) and os.unlink(tmp)
        raise


def collect(state_path, verbose=True):
    """
    Records read from documents that are new since the last run, plus any
    still waiting on a retry. Returns (records, health).
    """
    say = _say if verbose else (lambda *a: None)
    st = load_state(state_path)
    seen = st["seen"]
    today = datetime.now(timezone.utc).date()

    items = read_feed()
    ipo_items = [(i, item_type(i)) for i in items]
    ipo_items = [(i, t) for i, t in ipo_items if t]
    say("NSE offer-documents feed: %d items, %d IPO documents" % (len(items), len(ipo_items)))

    out, read = [], 0
    for item, typ in ipo_items:
        link = item["link"]
        prev = seen.get(link)
        if prev and (prev.get("done") or prev.get("tries", 0) >= RETRY_LIMIT):
            continue
        entry = prev or {"first": today.isoformat(), "tries": 0,
                         "type": typ, "name": S.company(item["title"])}
        if SKIP_TYPE_RE.search(typ):
            entry.update(done=True, result="draft, no dates")
            seen[link] = entry
            continue
        if read >= MAX_DOCS:
            say("  document reads capped at %d — the rest wait for the next run" % MAX_DOCS)
            break

        read += 1
        entry["tries"] = entry.get("tries", 0) + 1
        try:
            texts = [S.text_of(b, pages=8) for b in pdfs_in(S.fetch(link, binary=True))]
        except Exception as e:                      # noqa: BLE001
            entry["result"] = "unreadable: %s" % str(e)[:60]
            seen[link] = entry
            say("  %-30s %-6s unreadable (%s) — retry next run" % (entry["name"], typ, str(e)[:40]))
            continue

        rec = None
        for flat in texts:
            rec = record_from(item, typ, flat)
            if rec:
                break
        entry["done"] = True
        if rec:
            entry["result"] = "%s -> %s%s" % (rec["open"], rec["close"],
                                              " Rs %g" % rec["price"] if rec.get("price") else "")
            out.append(rec)
            say("  %-30s %-6s %s %s" % (rec["name"], typ, entry["result"], rec["kind"]))
        else:
            entry["result"] = "no plausible dates"
            say("  %-30s %-6s no plausible dates" % (entry["name"], typ))
        seen[link] = entry

    # What a document said lives on in the pool. The state only has to
    # remember a link for as long as it might still be in the feed, so the
    # same 10 MB document is not fetched twice.
    cutoff = (today - timedelta(days=FORGET_DAYS)).isoformat()
    st["seen"] = {k: v for k, v in seen.items() if v.get("first", "") >= cutoff}
    st["types_seen"] = sorted(set(st.get("types_seen", [])) | {t for _, t in ipo_items})
    save_state(state_path, st)

    health = {"items": len(items), "ipo_docs": len(ipo_items), "read": read,
              "records": len(out), "types": st["types_seen"]}
    return out, health


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Read IPO records from NSE's offer-documents RSS feed.")
    ap.add_argument("--state", required=True, help="JSON file remembering items already read")
    args = ap.parse_args()
    recs, health = collect(args.state)
    json.dump({"health": health, "ipos": recs}, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
