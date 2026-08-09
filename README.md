# leadpipe

Small-batch outbound lead pipeline:
discover → score → draft → **human approve** → governed send → triage replies → learn.

All modules live flat at the project root — no package directory, no
`__init__.py`. State lives in a single SQLite file, `leads.db`, which is
accumulated training data: **never commit it** (gitignored), and
`backup.sh` keeps 30 days of nightly copies.

## Core modules

| file | owns |
|---|---|
| `lead_finder.py` | Google Places discovery, website audit, heuristic score |
| `lead_brain.py` | SQLite ledger + core schema, quality gate, learned scorer, market bandit, send governor |
| `claude_tasks.py` | Anthropic calls: reply triage, opener drafting, weekly report |
| `orchestrator.py` | job runner with file locking (`discover\|triage\|report`) + `runs`/`send_queue`/`inbox` schema |

The schema is split: `lead_brain.db()` creates the core tables, and
`orchestrator.RUNS_SCHEMA` adds `runs`, `send_queue`, `inbox`. The
scaffold entry points apply both via their `db()` helper, so any of
them works on a fresh install.

## Scaffold

- `webhook.py` — Flask receiver for inbound replies. Verifies
  `X-Webhook-Secret`, extracts the lead id from the plus-address
  (`reply+L4821@domain` → `L4821`), stores into `inbox` unclassified.
  Triage classifies later, on its own schedule. `GET /health` for checks.
- `sender.py` — sends approved queue items: governor budget first, then
  best-score order, suppression re-checked per send, `Reply-To` plus-tagged.
  `--dry-run` prints everything and touches nothing.
- `approve.py` — queue review from any terminal: `list`, `approve`,
  `reject`, `stats`.
- `systemd/` + `install.sh` + `backup.sh` — deployment (see below).

## Setup (dev)

```sh
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env                    # fill in keys + LEADPIPE_SERVICE
cp markets.json.example markets.json    # set your real niche/geo targets
```

`markets.json` and `.env` are gitignored on purpose: what you sell and
where you hunt is private config, not code.

## Tests

```sh
venv/bin/pytest tests/test_smoke.py                       # offline, free
RUN_TRIAGE_TESTS=1 venv/bin/pytest tests/test_triage_fixtures.py -s   # real API calls
```

The triage fixtures are the regression guard on the classification
prompt. Labels come from `claude_tasks.TRIAGE_CATEGORIES` (`positive,
question, not_interested, remove_me, auto_reply, wrong_person, hostile`;
out-of-office is `auto_reply`) — the smoke suite verifies the fixtures
stay in sync with that list for free. The paid test hard-fails if any
autoresponder is classified `positive` (that error silently corrupts the
training labels) or if a `remove_me`/`hostile` reply comes back without
`suppress=true`.

## Daily driving

```sh
python approve.py list          # review pending openers
python approve.py approve L4821 L4830
python sender.py --dry-run      # see exactly what would go out
python sender.py --limit 10
python approve.py stats         # bounce rate + remaining budget
```

## Deploy (Linux, systemd)

Background and rationale live in `DEPLOY.md`; `install.sh` and
`systemd/` implement it (plus the always-on webhook and nightly backup).

```sh
sudo ./install.sh    # idempotent; installs to /opt/leadpipe, enables timers
```

| unit | schedule |
|---|---|
| `leadpipe@discover` | Mon–Fri 06:00 |
| `leadpipe@triage` | every 30 min |
| `leadpipe@report` | Mon 08:00 |
| `leadpipe-backup` | daily 02:15 |
| `leadpipe-webhook` | always on (port 8025, put TLS proxy in front) |

All timers are `Persistent=true`, so a run missed while the box was off
fires on boot instead of being skipped.
