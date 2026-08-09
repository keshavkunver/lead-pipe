"""Inbound reply receiver.

Small Flask app sitting behind the mail provider's inbound webhook.
Deliberately dumb: verify, parse, store, return 200. Classification
happens later in the `triage` job, so a slow LLM call can never make
the provider time out and retry (which would duplicate rows).
"""
import hashlib
import hmac
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request

import lead_brain as lb
import orchestrator

load_dotenv()
app = Flask(__name__)


def db():
    """Full-schema connection. lead_brain owns the core tables;
    runs/send_queue/inbox belong to orchestrator — apply both so a fresh
    install works from any entry point."""
    conn = lb.db()
    conn.executescript(orchestrator.RUNS_SCHEMA)
    return conn

# Matches the plus-tag in "reply+L4821@domain.com", including inside
# display-name forms like "Ops <reply+L4821@domain.com>".
_PLUS_TAG = re.compile(r"\+([A-Za-z0-9][\w-]*)@")


def _authorized() -> bool:
    secret = os.environ.get("WEBHOOK_SECRET", "")
    supplied = request.headers.get("X-Webhook-Secret", "")
    # Fail closed: an unset secret rejects everything rather than
    # quietly accepting the whole internet.
    return bool(secret) and hmac.compare_digest(secret, supplied)


def parse_provider_payload(payload: dict) -> dict:
    """Map a Postmark-shaped inbound payload onto our inbox columns.

    Every provider-specific field name lives here; swapping providers
    should touch only this function.
    """
    body = payload.get("TextBody") or payload.get("HtmlBody") or ""
    to_field = payload.get("To") or ""
    msg_id = payload.get("MessageID")
    if not msg_id:
        # Retry-dedup still has to work when the provider omits an id,
        # so derive a stable one from the content itself.
        digest = hashlib.sha256(
            f"{to_field}|{payload.get('Subject', '')}|{body}".encode()
        ).hexdigest()
        msg_id = f"derived-{digest[:32]}"
    return {
        "msg_id": msg_id,
        "to": to_field,
        "subject": payload.get("Subject") or "",
        "body": body,
        "received": payload.get("Date") or datetime.now(timezone.utc).isoformat(),
    }


def lead_id_from_address(to_field: str | None) -> str | None:
    m = _PLUS_TAG.search(to_field or "")
    return m.group(1) if m else None


@app.post("/inbound")
def inbound():
    if not _authorized():
        abort(401)
    msg = parse_provider_payload(request.get_json(force=True, silent=True) or {})
    # lead_id=NULL is fine: a reply we can't attribute is still a reply,
    # and triage can try harder to match it later. Never drop mail.
    lead_id = lead_id_from_address(msg["to"])
    conn = db()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO inbox (msg_id, lead_id, subject, body, received, processed) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (msg["msg_id"], lead_id, msg["subject"], msg["body"], msg["received"]),
        )
        conn.commit()
        return jsonify({"stored": cur.rowcount == 1, "lead_id": lead_id})
    finally:
        conn.close()


@app.get("/health")
def health():
    conn = db()
    try:
        def one(sql):
            return conn.execute(sql).fetchone()[0]

        return jsonify({
            "inbox_total": one("SELECT COUNT(*) FROM inbox"),
            "inbox_unprocessed": one("SELECT COUNT(*) FROM inbox WHERE processed = 0"),
            "queue_pending": one("SELECT COUNT(*) FROM send_queue WHERE approved = 0"),
            "leads": one("SELECT COUNT(*) FROM leads"),
        })
    finally:
        conn.close()


if __name__ == "__main__":
    # Dev / systemd entry point. Put a reverse proxy in front for TLS.
    app.run(host="127.0.0.1", port=int(os.environ.get("WEBHOOK_PORT", "8025")))
