# lead-pipe

Find local businesses that have **no website**, using OpenStreetMap's
free [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API).
One script, no API keys, no paid services, no database.

```sh
pip install requests
python leads.py "Boise" restaurant
```

Output is CSV on stdout — `name, phone, address, osm_id` — with
phone-having rows sorted first, since those are the ones you can call:

```csv
name,phone,address,osm_id
Bombay Grill,+1-208-345-7888,"928 West Main Street, Boise",node/7184834489
Cowboy Burger,+1-208-373-0020,"7000 West Fairview Avenue, Boise",way/590310065
```

Pipe it wherever: `python leads.py "Boise" roofing > roofing.csv`

## What it does

1. Queries Overpass for businesses of the given category inside the
   city's boundary (falls back to any OSM area with that name if the
   city has no administrative boundary mapped).
2. Keeps only businesses with a **name** AND (a **phone** OR an
   **address**) AND **no website tag** — i.e. reachable businesses that
   plausibly need a site.
3. Prints CSV, deduplicated and phone-first. The lead count goes to
   stderr so it doesn't pollute pipes.

Requests are rate-limited to one per second, per Overpass usage policy,
and fall back across public Overpass mirrors when the primary times out.

## Categories

`hvac, plumbing, roofing, landscaping, auto-repair, dentist, restaurant`

Each maps to one or more OSM tags (e.g. `dentist` → `amenity=dentist` or
`healthcare=dentist`), since real-world tagging is inconsistent. To add
a category, add a line to `CATEGORIES` in [leads.py](leads.py).

## Caveats

- Coverage is whatever volunteers have mapped. Dense categories
  (restaurants) are well covered; trade categories (hvac) can be thin
  in some cities. 0 results usually means thin OSM coverage, not a bug.
- "No website tag" isn't proof there's no website — it may just be
  unmapped. Treat the output as a call list, not ground truth.

Data © OpenStreetMap contributors, available under the
[ODbL](https://www.openstreetmap.org/copyright).

---

*History note: this repo was once a full discover→score→approve→send
pipeline with a learning loop. It was deliberately stripped down to this
single script; the old version lives in git history before commit
`leads.py: strip pipeline down to single-file OSM lead finder`.*
