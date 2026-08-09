# leadpipe

Outbound lead-gen pipeline: discover → gate → score → draft opener → human
approve → governed send → triage replies → learn (labels feed a logistic
regression + market bandit). One SQLite file (`leads.db`) is all state.

## Module ownership — read this first

The four core modules are written and maintained **by the user directly**:
`lead_finder.py`, `lead_brain.py`, `claude_tasks.py`, `orchestrator.py`.
Do not rewrite, restructure, or stub them; propose changes as suggestions.
Claude owns the scaffolding around them: `webhook.py`, `sender.py`,
`approve.py`, `tests/`, `systemd/`, `install.sh`, `backup.sh`.

## Layout constraints

- Modules live **flat at the project root**. No `src/`, no package dir, no
  `__init__.py` — existing code does `import lead_brain as lb`.
- Schema is split across owners: `lead_brain.db()` creates
  `leads/outcomes/suppression/sends`; `orchestrator.RUNS_SCHEMA` adds
  `runs/send_queue/inbox`. Entry points must apply **both** (webhook/sender/
  approve each have a `db()` helper for this). Never redefine tables inline.
- `lead_brain.FEATURES` order is **frozen, append-only** — reordering
  misaligns every stored feature vector against fitted models.
- Reply categories come from `claude_tasks.TRIAGE_CATEGORIES` (positive,
  question, not_interested, remove_me, auto_reply, wrong_person, hostile).
  Out-of-office = `auto_reply`. Fixtures in `tests/triage_fixtures.json`
  must use exactly these labels (smoke test enforces it).

## Private config (gitignored — never commit, never hardcode)

- `markets.json` — live niche/geo targets (loaded by `job_discover`;
  tracked example: `markets.json.example`).
- `LEADPIPE_SERVICE` in `.env` — what the user sells, injected into the
  claude_tasks prompts.
- `TODO.md` — business strategy notes, local only.
- Keep it this way: business specifics go in untracked config, the repo
  stays generic.

## Data rules

- `leads.db` is accumulated training data — **never commit it**, never
  delete it. `backup.sh` keeps 30 days of nightly copies in `backup/`.
- `auto_reply` must never be recorded as engagement; a misclassified
  autoresponder silently poisons the training labels. The triage fixture
  test hard-fails on that specific error.
- Replies to `reply+<lead_id>@$MAIL_FROM_DOMAIN` are how inbound mail is
  attributed; webhook stores unattributable replies with `lead_id=NULL`
  rather than dropping them.

## Commands

```sh
venv/bin/pytest tests/                # offline smoke suite (free, ~10s)
RUN_TRIAGE_TESTS=1 venv/bin/pytest tests/test_triage_fixtures.py -s  # paid API calls
python orchestrator.py {discover|triage|report}   # jobs (file-locked)
python approve.py {list|approve|reject|stats}     # queue review
python sender.py --dry-run                        # governed send, preview
```

## Testing quirks

- Smoke tests `chdir` into a tmp dir per test — works because
  `lead_brain.DB` is the relative path `"leads.db"`. Don't make DB paths
  absolute without updating the `env` fixture.
- The triage test is deliberately excluded from plain `pytest` runs
  (env-var gated) because it costs Anthropic API calls.

## Deploy

Linux/systemd via `sudo ./install.sh` (idempotent, installs to
`/opt/leadpipe`, enables timers + always-on webhook). Rationale in
`DEPLOY.md`; units in `systemd/`. Timers use `Persistent=true` on purpose.
