# leadpipe

A small, self-hosted outbound lead-gen pipeline for local-service businesses
that **gets smarter the longer you run it** — and never sends an email a
human didn't approve.

```
discover → gate → score → draft opener → human approve → governed send
    ↑                                                        │
    └────────── learn (labels → model + bandit) ← triage replies
```

No SaaS, no dashboard, no framework. A handful of flat Python modules, one
SQLite file (`leads.db`) for all state, systemd timers for scheduling, and
your terminal for the approve queue.

## How it works

**Discover** — `lead_finder.py` pulls businesses from Google Places for the
niche/geo targets in your `markets.json`, audits each website (SSL, mobile
viewport, page weight, contact info, copyright staleness…), and computes a
heuristic opportunity score.

**Gate** — `lead_brain.QualityGate` drops leads that aren't worth anyone's
time: too few reviews, no reachable email domain (MX check), on the
suppression list, or contacted too recently.

**Score & select** — early on, ranking is pure heuristic. As reply labels
accumulate, a logistic regression (`LearnedScorer`) trains on the stored
feature vectors and its weight in the blend grows with the amount of
training data. Selection is epsilon-greedy: most sends go to top scorers, a
slice goes to random leads below the cut so the model keeps getting
counterfactual data.

**Market bandit** — `MarketBandit` tracks reply rate per niche×geo market
and steers future discovery runs toward the markets that actually respond.
This is the compounding part.

**Draft & approve** — `claude_tasks.py` drafts a short, specific opener per
lead (using the audit findings, not generic flattery). Nothing sends
automatically: every opener sits in a queue until you `approve.py approve`
it.

**Governed send** — `sender.py` sends approved items under a
`SendGovernor`: hard daily cap, automatic halt if the rolling bounce rate
crosses the limit, suppression re-checked per send. `--dry-run` shows
exactly what would go out and touches nothing.

**Triage & learn** — replies come back to a plus-tagged address
(`reply+<lead_id>@yourdomain`), land in `webhook.py`, and are classified by
Claude into one of: `positive, question, not_interested, remove_me,
auto_reply, wrong_person, hostile`. `remove_me`/`hostile` auto-suppress.
`auto_reply` (out-of-office) is explicitly *not* engagement — a test
hard-fails if an autoresponder is ever labeled positive, because that error
silently poisons the training data. Labels feed the scorer and the bandit,
closing the loop.

## Module map

| file | role |
|---|---|
| `lead_finder.py` | Google Places discovery, website audit, heuristic score |
| `lead_brain.py` | SQLite ledger + schema, quality gate, learned scorer, epsilon-greedy select, market bandit, send governor |
| `claude_tasks.py` | Anthropic API calls: reply triage, opener drafting, weekly report |
| `orchestrator.py` | file-locked job runner (`discover\|triage\|report`) + `runs`/`send_queue`/`inbox` schema |
| `webhook.py` | Flask receiver for inbound replies (secret-verified, fail-closed) |
| `sender.py` | governed sender for the approved queue |
| `approve.py` | terminal queue review: `list`, `approve`, `reject`, `stats` |
| `systemd/`, `install.sh`, `backup.sh` | deployment: timers, always-on webhook, nightly DB backups |

Modules live flat at the project root by design — no package dir, no
`__init__.py`. The schema is split between `lead_brain.db()` (core tables)
and `orchestrator.RUNS_SCHEMA`; every entry point applies both, so any of
them works on a fresh install.

## Quickstart

```sh
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env                    # API keys, mail domain, what you sell
cp markets.json.example markets.json    # your real niche/geo targets
venv/bin/pytest tests/                  # offline smoke suite, ~10s, free

venv/bin/python orchestrator.py discover   # find + queue drafted openers
venv/bin/python approve.py list            # review them
venv/bin/python approve.py approve L4821 L4830
venv/bin/python sender.py --dry-run        # preview, then drop --dry-run
```

You'll need: a [Google Places API](https://developers.google.com/maps/documentation/places/web-service) key,
an [Anthropic API](https://console.anthropic.com/) key, and a transactional
mail provider (Postmark-shaped API) with inbound webhooks pointed at
`webhook.py`.

**Your business config stays out of the repo.** `markets.json` (where you
hunt), `LEADPIPE_SERVICE` in `.env` (what you sell), and `leads.db` (your
accumulated training data) are all gitignored. The code is generic; the
edge is in your data.

## Tests

```sh
venv/bin/pytest tests/                                                # offline, free
RUN_TRIAGE_TESTS=1 venv/bin/pytest tests/test_triage_fixtures.py -s   # real API calls
```

The triage fixtures are the regression guard on the classification prompt.
The paid test is env-var gated so a plain `pytest` run never costs money;
the free smoke suite still verifies the fixtures stay in sync with
`TRIAGE_CATEGORIES`.

## Deploy (Linux, systemd)

```sh
sudo ./install.sh    # idempotent; installs to /opt/leadpipe, enables units
```

| unit | schedule |
|---|---|
| `leadpipe@discover` | Mon–Fri 06:00 |
| `leadpipe@triage` | every 30 min |
| `leadpipe@report` | Mon 08:00 |
| `leadpipe-backup` | daily 02:15 (30 days retained) |
| `leadpipe-webhook` | always on (port 8025 — put a TLS proxy in front) |

Timers are `Persistent=true`, so a run missed while the box was off fires
on boot instead of being skipped. Rationale in `DEPLOY.md`.

## Design choices, briefly

- **Human approval is load-bearing**, not a demo-mode toggle. The tool
  drafts and ranks; you decide what ships.
- **One SQLite file** is the whole state. Back it up (`backup.sh` does),
  never delete it — it's your training data.
- **Send governor over send velocity.** A hard daily cap and a bounce-rate
  circuit breaker protect your domain reputation from your own enthusiasm.
- **Fail closed.** Unset webhook secret → reject everything. Unattributable
  replies are stored (with `lead_id=NULL`), never dropped.
- **`lead_brain.FEATURES` is append-only.** Reordering it would misalign
  every stored feature vector against fitted models.
