#!/usr/bin/env python3
"""Find local businesses with no website, via OpenStreetMap's Overpass API.

Usage:  python leads.py "Boise" hvac
        python leads.py "Springfield, Missouri" restaurant
        python leads.py "Boise" hvac --fresh      (skip the response cache)
Output: CSV on stdout — name, phone, address, osm_id (phone-having rows first)

Raw Overpass responses are cached in .leads_cache/ for 24 hours, so
repeating a query is instant and costs the public servers nothing.

Local businesses only: anything tagged as a brand/chain is skipped, since
franchises have a corporate website even when the map element lacks one.
A social-media page (contact:facebook etc.) does not count as a website.
"""

import csv
import hashlib
import json
import pathlib
import queue
import sys
import threading
import time

import requests

# Public Overpass instances, tried in order — the main one 504s under load.
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "leads.py (https://github.com/keshavkunver/lead-pipe)"

# One session so hedges and follow-up queries reuse TLS connections.
SESSION = requests.Session()

CACHE_DIR = pathlib.Path(__file__).with_name(".leads_cache")
CACHE_TTL = 24 * 3600  # the mirrors themselves lag OSM by hours anyway

# Each category can match several OSM tagging conventions; all selectors go
# into one query. Trade businesses especially get tagged inconsistently.
CATEGORIES = {
    "hvac": ['["craft"="hvac"]', '["shop"="hvac"]',
             '["shop"="trade"]["trade"="hvac"]',
             '["craft"="air_conditioning"]', '["craft"="heating_engineer"]'],
    "plumbing": ['["craft"="plumber"]', '["shop"="trade"]["trade"="plumbing"]'],
    "roofing": ['["craft"="roofer"]', '["shop"="trade"]["trade"="roofing"]'],
    "landscaping": ['["craft"="gardener"]'],
    "auto-repair": ['["shop"="car_repair"]'],
    "dentist": ['["amenity"="dentist"]', '["healthcare"="dentist"]'],
    "restaurant": ['["amenity"="restaurant"]'],
}

# How long to give a mirror before racing the query on the next one too.
# Healthy mirrors answer in 2-4s; much lower than this and we'd be
# routinely sending duplicate queries to free servers.
HEDGE_DELAY = 10.0

_last_request = 0.0


def _post(url, query, results):
    try:
        r = SESSION.post(
            url,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            # The server self-limits at 60s ([timeout:60]); no point
            # waiting much longer than that for a hung connection.
            timeout=70,
        )
        r.raise_for_status()
        results.put((url, r.json()["elements"], None))
    except (requests.RequestException, ValueError, KeyError) as e:
        results.put((url, None, e))


def overpass(query, fresh=False):
    """Answer one Overpass query, from cache when possible.

    Successful responses are cached on disk for CACHE_TTL; `fresh`
    forces a refetch. On the network path, hedge across the public
    mirrors: queue time on the free instances varies from ~2s to minutes, so
    instead of waiting out a full timeout per mirror in sequence, start
    on the first mirror and launch the same query on the next one after
    HEDGE_DELAY (or immediately once a mirror fails), taking whichever
    answers first. The winning mirror moves to the front of the list for
    the rest of the run. Launches stay throttled to one per second. If
    every mirror fails, waits out the load spike and makes one more pass
    before giving up — mirrors are often healthy again within a minute.
    """
    global _last_request
    cached = CACHE_DIR / (hashlib.sha256(query.encode()).hexdigest()[:16] + ".json")
    if not fresh:
        try:
            age_h = (time.time() - cached.stat().st_mtime) / 3600
            if age_h * 3600 < CACHE_TTL:
                print(f"note: using {age_h:.1f}h-old cached response "
                      "(pass --fresh to refetch)", file=sys.stderr)
                return json.loads(cached.read_text())
        except (OSError, ValueError):
            pass  # no cache entry, or an unreadable one — fetch normally
    last_error = None
    for attempt in range(2):
        if attempt:
            print("note: all mirrors failed, waiting 30s and retrying once",
                  file=sys.stderr)
            time.sleep(30)
        results = queue.Queue()
        started = pending = 0
        next_start = time.monotonic()
        while started < len(ENDPOINTS) or pending:
            now = time.monotonic()
            if started < len(ENDPOINTS) and now >= next_start:
                wait = _last_request + 1.0 - now
                if wait > 0:
                    time.sleep(wait)
                _last_request = time.monotonic()
                threading.Thread(
                    target=_post, args=(ENDPOINTS[started], query, results),
                    daemon=True,
                ).start()
                started += 1
                pending += 1
                next_start = time.monotonic() + HEDGE_DELAY
                continue
            try:
                timeout = (next_start - now if started < len(ENDPOINTS)
                           else None)
                url, elements, err = results.get(timeout=timeout)
            except queue.Empty:
                continue
            pending -= 1
            if err is None:
                ENDPOINTS.remove(url)
                ENDPOINTS.insert(0, url)
                CACHE_DIR.mkdir(exist_ok=True)
                cached.write_text(json.dumps(elements))
                return elements
            last_error = err
            print(f"note: {url.split('/')[2]} failed", file=sys.stderr)
            next_start = time.monotonic()  # a failure frees a hedge slot
    sys.exit(f"All Overpass mirrors failed ({last_error}). "
             "The servers are busy — try again in a minute or two.")


def fetch(city, selectors, fresh=False):
    # "Springfield, Missouri" scopes the search to that state's Springfield;
    # a bare name matches every administrative area called that, worldwide.
    state = None
    if "," in city:
        city, state = (part.strip() for part in city.split(",", 1))
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    city = esc(city)
    union = "".join(
        f"node{s}(area.a); way{s}(area.a); " for s in selectors
    )
    # Prefer the administrative boundary; fall back to any area with the name
    # (some cities are only mapped as a place, not a boundary).
    for area_filter in ('["boundary"="administrative"]', ""):
        if state:
            area = (
                f'area["name"="{esc(state)}"]["boundary"="administrative"]->.s;\n'
                f'rel["name"="{city}"]{area_filter}(area.s);\n'
                f'map_to_area ->.a;\n'
            )
        else:
            area = f'area["name"="{city}"]{area_filter}->.a;\n'
        query = (
            f'[out:json][timeout:60];\n'
            f'{area}'
            f'( {union});\n'
            # qt = quadtile (storage) order: skips the server-side sort
            # by object id, and we re-sort by phone/name anyway.
            f'out center tags qt;'
        )
        elements = overpass(query, fresh)
        if elements:
            return elements
    return []


def address(tags):
    street = " ".join(
        p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p
    )
    parts = [p for p in (street, tags.get("addr:city")) if p]
    return ", ".join(parts) or tags.get("addr:full", "")


def main():
    args = sys.argv[1:]
    fresh = "--fresh" in args
    if fresh:
        args.remove("--fresh")
    if len(args) != 2 or args[1] not in CATEGORIES:
        cats = ", ".join(CATEGORIES)
        sys.exit(f'usage: python leads.py "<city>[, <state>]" <category>'
                 f' [--fresh]\ncategories: {cats}')
    city, category = args

    leads = []
    for el in fetch(city, CATEGORIES[category], fresh):
        tags = el.get("tags", {})
        # Chains and franchises have a corporate website even when the
        # element carries no website tag — they're never leads.
        if tags.get("brand") or tags.get("brand:wikidata"):
            continue
        name = tags.get("name", "").strip()
        phone = tags.get("phone") or tags.get("contact:phone") or ""
        addr = address(tags)
        website = tags.get("website") or tags.get("contact:website")
        if name and (phone or addr) and not website:
            leads.append([name, phone, addr, f"{el['type']}/{el['id']}"])

    # Phone-having leads first (they're the callable ones), then by name.
    leads.sort(key=lambda r: (r[1] == "", r[0].lower()))

    # Same business sometimes exists as both a point and a building outline;
    # sorting first means the phone-bearing copy is the one kept.
    seen, unique = set(), []
    for r in leads:
        key = (r[0].lower(), r[2].lower())
        if key not in seen:
            seen.add(key)
            unique.append(r)

    out = csv.writer(sys.stdout)
    out.writerow(["name", "phone", "address", "osm_id"])
    out.writerows(unique)
    n_phones = sum(1 for r in unique if r[1])
    print(f"{len(unique)} leads ({n_phones} with phone)", file=sys.stderr)


if __name__ == "__main__":
    main()
