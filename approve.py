"""Review and gate the send queue. Output fits an 80-column phone terminal.

Usage:
  python approve.py list [--limit N]
  python approve.py approve <lead_id>... | approve --all
  python approve.py reject <lead_id>...
  python approve.py stats
"""
import argparse
import sys

from dotenv import load_dotenv

import lead_brain as lb
import orchestrator


def db():
    """Full-schema connection: lead_brain's core tables plus orchestrator's
    runs/send_queue/inbox, so a fresh install works from any entry point."""
    conn = lb.db()
    conn.executescript(orchestrator.RUNS_SCHEMA)
    return conn


def cmd_list(conn, args):
    rows = conn.execute(
        "SELECT lead_id, score, selected_via, opener FROM send_queue "
        "WHERE approved = 0 ORDER BY score DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("queue is empty — nothing pending")
        return
    print(f"{'lead_id':<10} {'score':>5}  {'via':<9} opener")
    print("-" * 78)
    for r in rows:
        opener = " ".join((r["opener"] or "").split())  # collapse newlines for one-line view
        via = (r["selected_via"] or "")[:9]
        print(f"{r['lead_id']:<10} {r['score']:>5.2f}  {via:<9} {opener[:49]}")


def _pending_ids(conn, args):
    if getattr(args, "all", False):
        return [r["lead_id"] for r in conn.execute(
            "SELECT lead_id FROM send_queue WHERE approved = 0")]
    return args.lead_ids


def cmd_approve(conn, args):
    ids = _pending_ids(conn, args)
    if not ids:
        print("nothing to approve — give lead ids or --all")
        return
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE send_queue SET approved = 1 WHERE lead_id IN ({marks})", ids)
    conn.commit()
    print(f"approved {cur.rowcount}")


def cmd_reject(conn, args):
    # Reject only drops the draft — it is NOT suppression. The lead can
    # come back later with a better opener.
    marks = ",".join("?" * len(args.lead_ids))
    cur = conn.execute(
        f"DELETE FROM send_queue WHERE lead_id IN ({marks})", args.lead_ids)
    conn.commit()
    print(f"rejected {cur.rowcount}")


def cmd_stats(conn, args):
    def one(sql):
        return conn.execute(sql).fetchone()[0]

    pending = one("SELECT COUNT(*) FROM send_queue WHERE approved = 0")
    approved = one("SELECT COUNT(*) FROM send_queue WHERE approved = 1")
    contacted = one("SELECT COUNT(*) FROM outcomes WHERE stage = 'contacted'")
    bounced = one("SELECT COUNT(*) FROM outcomes WHERE stage = 'bounced'")
    rate = (bounced / contacted) if contacted else 0.0
    budget, why = lb.SendGovernor(conn).budget()
    print(f"pending:     {pending}")
    print(f"approved:    {approved}")
    print(f"bounce rate: {rate:.1%} ({bounced}/{contacted})")
    print(f"budget:      {budget} ({why})")


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Review the send queue.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="show pending items")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("approve", help="mark items sendable")
    p.add_argument("lead_ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("reject", help="drop drafts (does not suppress)")
    p.add_argument("lead_ids", nargs="+")
    p.set_defaults(fn=cmd_reject)

    p = sub.add_parser("stats", help="queue counts, bounce rate, budget")
    p.set_defaults(fn=cmd_stats)

    args = ap.parse_args(argv)
    conn = db()
    try:
        args.fn(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
