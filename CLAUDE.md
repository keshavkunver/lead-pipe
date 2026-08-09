# lead-pipe

One deliberately-frozen script: `leads.py` queries the Overpass API for
businesses in a city+category that have a name, a phone or address, and
no website tag, and prints CSV.

- Scope is frozen on purpose — the user stripped a much larger pipeline
  down to this. Do **not** add scoring, enrichment, auditing, databases,
  config files, a UI, or new dependencies (stdlib + `requests` only).
- Untracked local files (`leads.db`, `backup/`, `markets.json`,
  `TODO.md`, `.env`) are leftovers from the old pipeline. Leave them
  alone; `leads.db` especially must never be deleted.
- The old pipeline is in git history (pre-strip commits) if ever needed.
