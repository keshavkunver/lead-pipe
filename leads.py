#!/usr/bin/env python3
"""Find local businesses with no website, via OpenStreetMap's Overpass API.

Usage:  python leads.py "Boise" hvac
        python leads.py "Springfield, Missouri" restaurant
Output: CSV on stdout — name, phone, address, osm_id (phone-having rows first)

Local businesses only: anything tagged as a brand/chain is skipped, since
franchises have a corporate website even when the map element lacks one.
A social-media page (contact:facebook etc.) does not count as a website.
"""

import csv
import sys
import time

import requests

# Public Overpass instances, tried in order — the main one 504s under load.
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "leads.py (https://github.com/keshavkunver/lead-pipe)"

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

_last_request = 0.0


def overpass(query):
    """POST one Overpass query, at most one request per second.

    On failure (timeout, 5xx), waits briefly and falls through to the next
    mirror instead of crashing. If every mirror fails, waits out the load
    spike and makes one more pass before giving up — mirrors are often
    healthy again within a minute.
    """
    global _last_request
    last_error = None
    for attempt in range(2):
        if attempt:
            print("note: all mirrors failed, waiting 30s and retrying once",
                  file=sys.stderr)
            time.sleep(30)
        for url in ENDPOINTS:
            wait = _last_request + 1.0 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request = time.monotonic()
            try:
                r = requests.post(
                    url,
                    data={"data": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=90,
                )
                r.raise_for_status()
                return r.json()["elements"]
            except requests.RequestException as e:
                last_error = e
                print(f"note: {url.split('/')[2]} failed, trying next mirror",
                      file=sys.stderr)
                time.sleep(5)
    sys.exit(f"All Overpass mirrors failed ({last_error}). "
             "The servers are busy — try again in a minute or two.")


def fetch(city, selectors):
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
            f'out center tags;'
        )
        elements = overpass(query)
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
    if len(sys.argv) != 3 or sys.argv[2] not in CATEGORIES:
        cats = ", ".join(CATEGORIES)
        sys.exit(f'usage: python leads.py "<city>[, <state>]" <category>\n'
                 f'categories: {cats}')
    city, category = sys.argv[1], sys.argv[2]

    leads = []
    for el in fetch(city, CATEGORIES[category]):
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
