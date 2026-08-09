"""Send approved queue items, governed and suppression-checked.

Usage: python sender.py [--dry-run] [--limit N]
"""
import argparse
import os
import sys

import requests
from dotenv import load_dotenv

import lead_brain as lb
import orchestrator

POSTMARK_API = "https://api.postmarkapp.com/email"
# Used when the opener doesn't carry its own "Subject: ..." first line.
DEFAULT_SUBJECT = "quick question"


def db():
    """Full-schema connection: lead_brain's core tables plus orchestrator's
    runs/send_queue/inbox, so a fresh install works from any entry point."""
    conn = lb.db()
    conn.executescript(orchestrator.RUNS_SCHEMA)
    return conn


def reply_address(lead_id: str) -> str:
    # The plus-tag is what lets webhook.py attribute the eventual reply.
    return f"reply+{lead_id}@{os.environ['MAIL_FROM_DOMAIN']}"


def from_address() -> str:
    return f"outreach@{os.environ['MAIL_FROM_DOMAIN']}"


def split_opener(opener: str) -> tuple[str, str]:
    """Convention: an opener may set its own subject via a 'Subject: ...' first line."""
    first, _, rest = (opener or "").partition("\n")
    if first.lower().startswith("subject:"):
        return first[len("subject:"):].strip(), rest.lstrip("\n")
    return DEFAULT_SUBJECT, opener or ""


def deliver(to_addr: str, subject: str, body: str, reply_to: str) -> None:
    """Provider call, isolated so a provider swap touches only this. Raises on failure."""
    resp = requests.post(
        POSTMARK_API,
        headers={
            "X-Postmark-Server-Token": os.environ["MAIL_PROVIDER_TOKEN"],
            "Accept": "application/json",
        },
        json={
            "From": from_address(),
            "To": to_addr,
            "Subject": subject,
            "TextBody": body,
            "ReplyTo": reply_to,
            "MessageStream": "outbound",
        },
        timeout=30,
    )
    resp.raise_for_status()


def suppression_hit(conn, row):
    # Checked at send time, not queue time: the list may have grown since.
    keys = [k for k in (row["email"], row["domain"], row["lead_id"]) if k]
    marks = ",".join("?" * len(keys))
    return conn.execute(
        f"SELECT key, reason FROM suppression WHERE key IN ({marks})", keys
    ).fetchone()


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Send approved items from the queue.")
    ap.add_argument("--dry-run", action="store_true", help="print what would send; touch nothing")
    ap.add_argument("--limit", type=int, default=None, help="cap sends this run")
    args = ap.parse_args(argv)

    conn = db()
    gov = lb.SendGovernor(conn)
    budget, why = gov.budget()
    if budget == 0:
        print(why)
        return 0
    cap = budget if args.limit is None else min(budget, args.limit)

    rows = conn.execute(
        "SELECT q.lead_id, q.opener, q.score, l.email, l.domain, l.name "
        "FROM send_queue q JOIN leads l ON l.lead_id = q.lead_id "
        "WHERE q.approved = 1 ORDER BY q.score DESC LIMIT ?",
        (cap,),
    ).fetchall()
    if not rows:
        print("queue empty (nothing approved)")
        return 0

    sent = planned = 0
    for row in rows:
        hit = suppression_hit(conn, row)
        if hit:
            # Suppressed after queueing — drop the row so it can't send later either.
            print(f"skip {row['lead_id']}: suppressed ({hit['key']}: {hit['reason']})")
            if not args.dry_run:
                conn.execute("DELETE FROM send_queue WHERE lead_id = ?", (row["lead_id"],))
                conn.commit()
            continue

        subject, body = split_opener(row["opener"])
        if args.dry_run:
            planned += 1
            print(f"DRY RUN -> {row['email']} (lead {row['lead_id']}, score {row['score']:.2f})")
            print(f"  Subject:  {subject}")
            print(f"  Reply-To: {reply_address(row['lead_id'])}")
            print("  " + body.replace("\n", "\n  "))
            continue

        try:
            deliver(row["email"], subject, body, reply_address(row["lead_id"]))
        except Exception as exc:  # provider hiccup: keep the row, try the rest
            print(f"send failed for {row['lead_id']}: {exc}", file=sys.stderr)
            continue
        lb.record(conn, row["lead_id"], "contacted")
        gov.log_send(row["lead_id"])
        conn.execute("DELETE FROM send_queue WHERE lead_id = ?", (row["lead_id"],))
        conn.commit()
        sent += 1
        print(f"sent {row['lead_id']} -> {row['email']}")

    if args.dry_run:
        print(f"would send {planned} (budget {budget})")
    else:
        print(f"sent {sent} of {len(rows)} (budget was {budget})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
