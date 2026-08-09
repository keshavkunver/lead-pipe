# Deployment

## Where to run it

A $5/mo VPS (Hetzner, DigitalOcean). You need a **persistent disk** because
`leads.db` is your accumulated training data — losing it resets the learning to
zero. That rules out most serverless options.

GitHub Actions works only if you push the DB to S3 each run. Not worth the
complexity for $5.

Back up the DB. It is the only irreplaceable thing here — the code you can
regenerate in an afternoon, the outcome history you cannot.

```bash
# in crontab, nightly
0 3 * * * sqlite3 /opt/leadpipe/leads.db ".backup /opt/leadpipe/backup/$(date +\%F).db"
```

## systemd (preferred — real logs, automatic retry)

`/etc/systemd/system/leadpipe@.service`

```ini
[Unit]
Description=Lead pipeline: %i
After=network-online.target

[Service]
Type=oneshot
User=leadpipe
WorkingDirectory=/opt/leadpipe
EnvironmentFile=/opt/leadpipe/.env
ExecStart=/opt/leadpipe/venv/bin/python orchestrator.py %i
StandardOutput=journal
StandardError=journal
```

`/etc/systemd/system/leadpipe-discover.timer`

```ini
[Unit]
Description=Daily lead discovery

[Timer]
OnCalendar=Mon..Fri 06:00
Persistent=true
Unit=leadpipe@discover.service

[Install]
WantedBy=timers.target
```

`leadpipe-triage.timer` → `OnCalendar=*:0/30` (every 30 min)
`leadpipe-report.timer` → `OnCalendar=Mon 08:00`

```bash
systemctl enable --now leadpipe-{discover,triage,report}.timer
systemctl list-timers 'leadpipe*'      # confirm next run times
journalctl -u leadpipe@discover -n 50  # read logs
```

`Persistent=true` matters: if the box was down at 06:00, the job runs on boot
instead of silently skipping the day.

## cron fallback

```cron
0  6 * * 1-5 cd /opt/leadpipe && ./venv/bin/python orchestrator.py discover >> logs/discover.log 2>&1
*/30 * * * * cd /opt/leadpipe && ./venv/bin/python orchestrator.py triage   >> logs/triage.log   2>&1
0  8 * * 1   cd /opt/leadpipe && ./venv/bin/python orchestrator.py report   >> logs/report.log   2>&1
```

cron does not load your shell profile, so put secrets in `.env` and load them
explicitly. The single most common cause of "it works when I run it manually."

## Getting replies into the inbox table

Triage needs inbound mail. Easiest path: a mail provider webhook (Postmark,
SendGrid, Mailgun) POSTing to a tiny Flask endpoint that inserts into `inbox`.
Match to `lead_id` via a plus-address (`you+L4821@yourdomain.com`) or a header
you set at send time. Do not try to match on sender address — people reply from
different addresses than the one you targeted.

## Approving the queue

`discover` queues sends; it does not send them. Approve from your phone:

```bash
# review
sqlite3 -box leads.db "SELECT lead_id, round(score) s, selected_via, opener
                       FROM send_queue WHERE approved=0 ORDER BY score DESC LIMIT 20;"
# approve all
sqlite3 leads.db "UPDATE send_queue SET approved=1 WHERE approved=0;"
```

Graduate to auto-send once you've watched ~200 openers and bounce rate has held
under 2% for a month. Not before.
