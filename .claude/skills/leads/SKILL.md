---
name: leads
description: >
  Find local businesses with no website by running this repo's leads.py
  (OpenStreetMap Overpass query, CSV output). Use whenever the user wants
  leads, prospects, a call list, or "businesses without websites" in some
  city — even if they don't mention leads.py or type /leads. Examples:
  "find plumbers in Boise with no site", "get me restaurant leads for
  Madison", "/leads Boise hvac". Takes a city and one category:
  hvac, plumbing, roofing, landscaping, auto-repair, dentist, restaurant.
argument-hint: "<city> <category>"
---

# /leads — no-website lead finder

Run `leads.py` and hand back a usable lead list. The script does all the
real work; this skill is only argument handling, execution, and a clean
summary. The tool's scope is frozen (see CLAUDE.md) — never edit
leads.py to satisfy a request; work with what it outputs.

## Parse the arguments

The last word is the category; everything before it is the city
("Sioux Falls hvac" → city "Sioux Falls", category "hvac").

- Normalize near-miss categories: "auto repair"/"mechanic" → auto-repair,
  "plumber" → plumbing, "roofer" → roofing, "landscaper"/"lawn care" →
  landscaping, "hvac contractor"/"heating and air" → hvac.
- If the category isn't one of the seven presets (or close to one), don't
  guess and don't add a preset — show the valid list and stop.
- If no arguments were given, ask which city and category.

## Run it

```sh
venv/bin/python leads.py "<city>" <category> > /tmp/leads-run.csv \
  && mv /tmp/leads-run.csv <city-slug>-<category>.csv
```

(Plain `python` if there's no venv.) The city slug is the city
lowercased with spaces → hyphens ("Sioux Falls" → `sioux-falls`). Write
the final CSV into the repo root — `*.csv` is gitignored, so it stays
local. Going through a temp file matters: a plain `>` redirect leaves a
misleading 0-byte CSV behind if the script fails.

Overpass is a shared volunteer-run service: runs take a few seconds
normally, but 504 Gateway Timeouts are routine under load. On a 5xx
error, wait ~20 seconds and retry, up to 3 attempts, before reporting
failure. The lead count is printed to stderr.

## Report

- Lead count and the CSV path.
- A markdown table of the leads (name, phone, address) right in the
  chat. Show all of them if there are ~15 or fewer; otherwise show the
  first 10 so the user sees the quality at a glance.
- If there are more rows than the preview shows, also open the full CSV
  in the user's spreadsheet app (`open <file>.csv` on macOS, `xdg-open`
  on Linux) so the complete call list is in front of them without a
  manual step. Skip this when there are 0 leads or the preview already
  covers everything.
- If some rows have phones and others don't, mention how many have
  phones — that's usually what the user actually dials.

## When there are 0 leads

Zero almost always means thin OpenStreetMap coverage for that category
in that city, not a bug — restaurants are reliably dense, everything
else varies by city (even dentist can be zero in a well-mapped town).
Say so, and suggest concretely:
try a larger neighboring city, or a denser category to confirm the city
resolves (e.g. run restaurant as a probe). If even restaurant returns 0,
the city name likely didn't match an OSM area — suggest the exact
municipal name (e.g. "City of Los Angeles" vs a neighborhood name).
