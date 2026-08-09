#!/usr/bin/env python3
"""
orchestrator.py - the scheduled entry point. One command per job.

    python orchestrator.py discover   # daily 06:00  - find, gate, score, QUEUE
    python orchestrator.py triage     # every 30 min - classify inbound replies
    python orchestrator.py report     # Monday 08:00 - refit, health, email brief

Deliberate design choices:

  * Separate jobs at separate cadences, not one monolith. When the discover job
    breaks, triage keeps classifying replies, so your training labels stay clean.
  * A file lock, because cron will happily start a second copy while the first
    is still running and give you duplicate sends.
  * Every run is logged to a `runs` table, so the weekly report can tell you a
    job SILENTLY STOPPED. Automation fails by going quiet, not by erroring
    loudly - a job that never runs sends no alert unless you check for absence.
  * discover QUEUES sends. It does not send them. See send_queue below.
"""

import fcntl
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone

import lead_brain as lb
import claude_tasks as ct

LOCK_PATH = "/tmp/leadpipe.lock"
NOW = lambda: datetime.now(timezone.utc)

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  job TEXT, started TEXT, finished TEXT, status TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS send_queue (
  lead_id TEXT PRIMARY KEY, opener TEXT, score REAL,
  selected_via TEXT, queued_at TEXT, approved INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inbox (
  msg_id TEXT PRIMARY KEY, lead_id TEXT, subject TEXT, body TEXT,
  received TEXT, processed INTEGER DEFAULT 0
);
"""


class Lock:
    """Prevents overlapping runs. Exits quietly if another instance holds it."""
    def __enter__(self):
        self.f = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another run in progress; exiting")
            sys.exit(0)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def run_job(name, fn):
    conn = lb.db()
    conn.executescript(RUNS_SCHEMA)
    started = NOW().isoformat()
    try:
        detail = fn(conn) or ""
        status = "ok"
    except Exception:
        detail, status = traceback.format_exc()[-1500:], "error"
        print(detail, file=sys.stderr)
    conn.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                 (name, started, NOW().isoformat(), status, str(detail)))
    conn.commit()
    print(f"[{name}] {status}: {str(detail)[:300]}")
    return status


# ------------------------------------------------------------------ DISCOVER

def job_discover(conn):
    """Find -> gate -> score -> queue. Does NOT send."""
    from lead_finder import discover, audit_site, score as heuristic_score
    import os

    key = os.environ["GOOGLE_API_KEY"]
    scorer = lb.LearnedScorer()
    fit_msg = scorer.fit(conn)

    # Let the bandit choose where to look. This is the compounding part.
    markets = [("HVAC contractor", "Round Rock TX"), ("dentist", "Austin TX"),
               ("roofing contractor", "Georgetown TX"), ("plumber", "Cedar Park TX")]
    niche, geo = lb.MarketBandit(conn).next_market(markets)

    gate = lb.QualityGate(conn)
    gov = lb.SendGovernor(conn)
    budget, why = gov.budget()
    if budget == 0:
        return f"no send budget: {why}"

    found = discover(niche, geo, key)
    candidates, rejected = [], 0

    for b in found:
        audit = audit_site(b["website"])
        need, _, pitch = heuristic_score(b, audit)
        feats = {
            "no_website": int(not b["website"]), "social_only": int(audit["social_only"]),
            "unreachable": int(not audit["reachable"] and bool(b["website"])),
            "no_https": int(not audit["https"]), "not_mobile": int(not audit["mobile_ready"]),
            "legacy_html": int(audit["legacy_html"]),
            "years_stale": max(0, lb.datetime.now().year - (audit["last_year"] or lb.datetime.now().year)),
            "weak_platform": int(audit["platform"] in ("GoDaddy Builder", "Weebly")),
            "pagespeed": 0, "reviews_log": round(__import__("math").log1p(b["reviews"]), 2),
            "rating": b["rating"], "has_email": 0, "is_franchise": 0, "agency_built": 0,
        }
        lead = {"lead_id": b["place_id"], "domain": "", "email": "", "phone": b["phone"],
                "reviews": b["reviews"]}
        ok, reason = gate.check(lead)
        if not ok:
            rejected += 1
            continue

        final, prob = scorer.score(feats, need)
        candidates.append({**lead, "name": b["name"], "score": final,
                           "features": feats, "pitch": pitch, "niche": niche, "geo": geo})

    chosen = lb.select(candidates, min(budget, len(candidates)), epsilon=0.15)

    for c in chosen:
        lb.save_lead(conn, c["lead_id"], c["name"], "", "", c["phone"],
                     c["niche"], c["geo"], c["features"], c["score"],
                     selected_via=c["selected_via"])
        try:
            opener = ct.draft_opener(c["name"], c["niche"], c["pitch"].split("; "))
        except Exception as e:
            opener = f"[draft failed: {e}]"
        conn.execute(
            "INSERT OR REPLACE INTO send_queue (lead_id,opener,score,selected_via,queued_at)"
            " VALUES (?,?,?,?,?)",
            (c["lead_id"], opener, c["score"], c["selected_via"], NOW().isoformat()))
    conn.commit()
    return f"{niche}/{geo}: found {len(found)}, gated out {rejected}, queued {len(chosen)}. {fit_msg}"


# -------------------------------------------------------------------- TRIAGE

def job_triage(conn):
    """Classify unprocessed replies and convert them into training labels.
    This is the job that must never break - it is your label supply."""
    rows = conn.execute("SELECT * FROM inbox WHERE processed=0 LIMIT 50").fetchall()
    counts, humans = {}, 0

    for r in rows:
        try:
            v = ct.classify_reply(r["subject"], r["body"])
        except Exception as e:
            print(f"  triage failed {r['msg_id']}: {e}", file=sys.stderr)
            continue  # leave processed=0 so it retries next run

        cat = v["category"]
        counts[cat] = counts.get(cat, 0) + 1

        # auto_reply deliberately writes NO outcome. That is the whole point:
        # an out-of-office must not be recorded as engagement.
        if cat == "positive":
            lb.record(conn, r["lead_id"], "positive_reply")
        elif cat in ("question", "not_interested", "wrong_person"):
            lb.record(conn, r["lead_id"], "replied")

        if v.get("suppress"):
            row = conn.execute("SELECT domain,phone FROM leads WHERE lead_id=?",
                               (r["lead_id"],)).fetchone()
            if row:
                lb.suppress(conn, row["domain"] or r["lead_id"], f"reply: {cat}")
        if v.get("needs_human"):
            humans += 1

        conn.execute("UPDATE inbox SET processed=1 WHERE msg_id=?", (r["msg_id"],))
    conn.commit()
    return f"classified {len(rows)}: {counts}; {humans} need you"


# -------------------------------------------------------------------- REPORT

def job_report(conn):
    scorer = lb.LearnedScorer()
    fit_msg = scorer.fit(conn)
    gov = lb.SendGovernor(conn)
    wk = (NOW() - timedelta(days=7)).isoformat()

    def n(sql, *p):
        return conn.execute(sql, p).fetchone()[0]

    stats = {
        "sends_this_week": n("SELECT COUNT(*) FROM sends WHERE ts>?", wk),
        "new_leads": n("SELECT COUNT(*) FROM leads WHERE created_at>?", wk),
        "positive_replies": n(
            "SELECT COUNT(*) FROM outcomes WHERE stage='positive_reply' AND ts>?", wk),
        "bounces": n("SELECT COUNT(*) FROM outcomes WHERE stage='bounced' AND ts>?", wk),
        "bounce_rate": round(gov.bounce_rate(), 4),
        "queue_awaiting_approval": n("SELECT COUNT(*) FROM send_queue WHERE approved=0"),
        "model": fit_msg,
        "model_trust": round(scorer.blend_weight(), 2),
        "top_coefficients": [[f, round(c, 3)] for f, c in scorer.explain()[:6]],
        "markets": [[nm, g, tot, f"{r:.0%}"] for nm, g, tot, w, r
                    in lb.MarketBandit(conn).report()],
        "health": lb.health(conn, scorer),
    }

    # Dead-man check: automation fails by going quiet.
    stats["silent_jobs"] = [
        j for j in ("discover", "triage")
        if not conn.execute("SELECT 1 FROM runs WHERE job=? AND status='ok' AND started>?",
                            (j, (NOW() - timedelta(days=2)).isoformat())).fetchone()
    ]
    stats["failed_runs"] = n("SELECT COUNT(*) FROM runs WHERE status='error' AND started>?", wk)

    brief = ct.write_report(stats)
    open("weekly_brief.txt", "w").write(brief)
    print("\n" + brief + "\n")
    return f"report written ({stats['sends_this_week']} sends, {stats['positive_replies']} positive)"


JOBS = {"discover": job_discover, "triage": job_triage, "report": job_report}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        sys.exit(f"usage: orchestrator.py [{'|'.join(JOBS)}]")
    name = sys.argv[1]
    with Lock():
        sys.exit(0 if run_job(name, JOBS[name]) == "ok" else 1)
