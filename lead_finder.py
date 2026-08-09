#!/usr/bin/env python3
"""
lead_finder.py - find local businesses that need a website (or a better one).

PIPELINE
  1. DISCOVER  Google Places API (New) Text Search -> businesses + their website (or lack of one)
  2. AUDIT     Fetch each site, check HTTPS / mobile / staleness / platform
  3. SCORE     0-100 "need" score, weighted by business viability (reviews)
  4. OUTPUT    Sorted CSV, hottest leads first

SETUP
  pip install requests beautifulsoup4
  export GOOGLE_API_KEY=...
  In Google Cloud console, enable: "Places API (New)" and "PageSpeed Insights API"

USAGE
  python lead_finder.py "HVAC contractor" "Springfield, IL"
  python lead_finder.py "dentist" "Boise, ID" --pagespeed --out boise_dentists.csv

COST NOTE
  Places Text Search bills per request, and the field mask controls the tier.
  Keep the mask tight. 60 results max per query (3 pages x 20), so slice your
  market by city + niche rather than trying to pull one huge query.
"""

import argparse
import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; LeadAudit/1.0; +https://example.com/bot)"
TIMEOUT = 12
CURRENT_YEAR = datetime.now().year


# ---------------------------------------------------------------- 1. DISCOVER

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "nextPageToken",
])


def discover(niche, location, api_key, max_pages=3):
    """Yield business dicts from Google Places Text Search."""
    businesses, token = [], None

    for page in range(max_pages):
        body = {"textQuery": f"{niche} in {location}", "pageSize": 20}
        if token:
            body["pageToken"] = token

        r = requests.post(
            PLACES_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": FIELD_MASK,
            },
            json=body,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"  ! Places API {r.status_code}: {r.text[:200]}", file=sys.stderr)
            break

        data = r.json()
        for p in data.get("places", []):
            businesses.append({
                "place_id": p.get("id", ""),
                "name": p.get("displayName", {}).get("text", ""),
                "address": p.get("formattedAddress", ""),
                "phone": p.get("nationalPhoneNumber", ""),
                "website": p.get("websiteUri", ""),
                "rating": p.get("rating", 0) or 0,
                "reviews": p.get("userRatingCount", 0) or 0,
            })

        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(2)  # next-page tokens need a moment to activate

    return businesses


# ------------------------------------------------------------------- 2. AUDIT

PLATFORMS = [
    ("GoDaddy Builder", ("img1.wsimg.com", "godaddysites.com")),
    ("Wix",             ("wix.com", "wixstatic.com", "_wixCssStates")),
    ("Squarespace",     ("squarespace.com",)),
    ("Weebly",          ("weebly.com", "editmysite.com")),
    ("Shopify",         ("cdn.shopify.com",)),
    ("WordPress",       ("/wp-content/", "/wp-includes/")),
]

# Platforms that usually signal a cheap DIY build = good upgrade pitch
WEAK_PLATFORMS = {"GoDaddy Builder", "Weebly"}


def audit_site(url):
    """Fetch a homepage and return a dict of problem flags."""
    a = {
        "reachable": False, "https": False, "mobile_ready": False,
        "platform": "", "last_year": None, "social_only": False,
        "legacy_html": False, "final_url": "", "error": "",
    }
    if not url:
        return a

    host = (urlparse(url).netloc or "").lower()
    if any(s in host for s in ("facebook.com", "instagram.com", "linktr.ee", "yelp.com")):
        a["social_only"] = True
        a["final_url"] = url
        return a

    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
        a["reachable"] = r.status_code < 400
        a["final_url"] = r.url
        a["https"] = r.url.startswith("https://")
        html = r.text[:400_000]
    except Exception as e:
        a["error"] = type(e).__name__
        return a

    soup = BeautifulSoup(html, "html.parser")

    # Mobile: a real responsive site declares a viewport
    vp = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    a["mobile_ready"] = bool(vp and "width" in (vp.get("content") or ""))

    # Staleness: newest 4-digit year near a copyright symbol
    years = re.findall(r"(?:©|&copy;|copyright)[^\d]{0,20}(\d{4})", html, re.I)
    if years:
        a["last_year"] = max(int(y) for y in years if 1990 <= int(y) <= CURRENT_YEAR + 1)

    # Platform fingerprint
    low = html.lower()
    for label, needles in PLATFORMS:
        if any(n.lower() in low for n in needles):
            a["platform"] = label
            break

    # Genuinely ancient markup
    a["legacy_html"] = bool(re.search(r"<font\b|<marquee\b|<center\b|\.swf", low))

    return a


def pagespeed(url, api_key):
    """Return mobile Lighthouse performance score 0-100, or None."""
    try:
        r = requests.get(
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
            params={"url": url, "key": api_key, "strategy": "mobile", "category": "performance"},
            timeout=90,
        )
        score = r.json()["lighthouseResult"]["categories"]["performance"]["score"]
        return round(score * 100)
    except Exception:
        return None


# ------------------------------------------------------------------- 3. SCORE

def score(biz, a, ps=None):
    """
    need  = how badly the website needs work (0-100)
    heat  = need weighted by whether the business can actually pay
    """
    pts, why = 0, []

    if not biz["website"]:
        pts += 55; why.append("no website at all")
    elif a["social_only"]:
        pts += 50; why.append("Facebook page instead of a site")
    elif not a["reachable"]:
        pts += 45; why.append("site is down/broken")
    else:
        if not a["https"]:
            pts += 25; why.append("no HTTPS")
        if not a["mobile_ready"]:
            pts += 25; why.append("not mobile-responsive")
        if a["legacy_html"]:
            pts += 20; why.append("ancient HTML")
        if a["last_year"] and a["last_year"] <= CURRENT_YEAR - 3:
            pts += 15; why.append(f"footer says {a['last_year']}")
        if a["platform"] in WEAK_PLATFORMS:
            pts += 10; why.append(f"{a['platform']} DIY build")
        if ps is not None:
            if ps < 30:
                pts += 25; why.append(f"PageSpeed {ps}/100")
            elif ps < 50:
                pts += 15; why.append(f"PageSpeed {ps}/100")

    need = min(pts, 100)

    # Viability: reviews prove foot traffic and revenue. No reviews = maybe dead.
    rc, rating = biz["reviews"], biz["rating"]
    if rc >= 100 and rating >= 4.0:
        mult = 1.0
    elif rc >= 30 and rating >= 3.8:
        mult = 0.85
    elif rc >= 10:
        mult = 0.65
    else:
        mult = 0.35

    return need, round(need * mult), "; ".join(why)


# ------------------------------------------------------------------ 4. RUNNER

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("niche", help='e.g. "HVAC contractor"')
    ap.add_argument("location", help='e.g. "Springfield, IL"')
    ap.add_argument("--out", default="leads.csv")
    ap.add_argument("--pagespeed", action="store_true", help="slower, adds Lighthouse scores")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        sys.exit("Set GOOGLE_API_KEY first.")

    print(f"Discovering: {args.niche} in {args.location} ...")
    businesses = discover(args.niche, args.location, key)
    print(f"  found {len(businesses)}")
    if not businesses:
        return

    print("Auditing websites ...")
    audits = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(audit_site, b["website"]): b["place_id"] for b in businesses}
        for f in as_completed(futs):
            audits[futs[f]] = f.result()

    ps_scores = {}
    if args.pagespeed:
        live = [b for b in businesses if audits[b["place_id"]]["reachable"]]
        print(f"Running PageSpeed on {len(live)} live sites (slow) ...")
        for i, b in enumerate(live, 1):
            ps_scores[b["place_id"]] = pagespeed(b["website"], key)
            print(f"  {i}/{len(live)}", end="\r")
            time.sleep(1)  # stay under the rate limit
        print()

    rows = []
    for b in businesses:
        a = audits[b["place_id"]]
        ps = ps_scores.get(b["place_id"])
        need, heat, why = score(b, a, ps)
        rows.append({
            "heat": heat, "need": need, "name": b["name"], "phone": b["phone"],
            "website": b["website"] or "(none)", "reviews": b["reviews"],
            "rating": b["rating"], "pagespeed": ps if ps is not None else "",
            "platform": a["platform"], "pitch": why, "address": b["address"],
        })

    rows.sort(key=lambda r: -r["heat"])
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"\nWrote {len(rows)} leads -> {args.out}\n")
    print("Top 10:")
    for r in rows[:10]:
        print(f"  {r['heat']:3d}  {r['name'][:34]:34s} {r['reviews']:>4}rev  {r['pitch'][:50]}")


if __name__ == "__main__":
    main()
