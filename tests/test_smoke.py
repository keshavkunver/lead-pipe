"""Offline smoke tests — no network, no API calls.

Assumes lead_brain.db() resolves leads.db relative to the working
directory; every test runs chdir'd into a fresh tmp dir so the real DB
is never touched (lead_brain.DB is the relative path "leads.db").
"""
import json
from pathlib import Path

import pytest

EXPECTED_TABLES = {"leads", "outcomes", "suppression", "sends", "runs",
                   "send_queue", "inbox"}
SECRET = "smoke-test-secret"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("MAIL_FROM_DOMAIN", "example.test")
    monkeypatch.setenv("MAIL_PROVIDER_TOKEN", "not-a-real-token")
    import lead_brain as lb
    return lb


def payload(**over):
    base = {"MessageID": "m-1", "To": "reply+L4821@example.test",
            "Subject": "Re: quick question",
            "TextBody": "Sounds interesting, tell me more.",
            "Date": "2026-08-09T12:00:00Z"}
    base.update(over)
    return base


def test_db_initialises_with_all_tables(env):
    # webhook.db() layers orchestrator's runs/send_queue/inbox on top of
    # lead_brain's core schema — the full set every entry point relies on.
    import webhook
    conn = webhook.db()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = EXPECTED_TABLES - names
    assert not missing, f"schema missing tables: {missing}"


def test_fixture_labels_match_triage_taxonomy():
    # Offline guard: if TRIAGE_CATEGORIES drifts, this fails for free
    # instead of the paid triage test failing confusingly.
    import claude_tasks
    fixtures = json.loads(
        (Path(__file__).with_name("triage_fixtures.json")).read_text())
    bad = {fx["id"]: fx["expected"] for fx in fixtures
           if fx["expected"] not in claude_tasks.TRIAGE_CATEGORIES}
    assert not bad, f"fixture labels not in claude_tasks.TRIAGE_CATEGORIES: {bad}"
    assert len(fixtures) == 20


def test_webhook_rejects_unsigned(env):
    import webhook
    client = webhook.app.test_client()
    assert client.post("/inbound", json=payload()).status_code == 401
    assert client.post("/inbound", json=payload(),
                       headers={"X-Webhook-Secret": "wrong"}).status_code == 401


def test_webhook_stores_signed_payload_once(env):
    import webhook
    client = webhook.app.test_client()
    headers = {"X-Webhook-Secret": SECRET}
    assert client.post("/inbound", json=payload(), headers=headers).status_code == 200
    # Provider retry with the same MessageID must not create a second row.
    assert client.post("/inbound", json=payload(), headers=headers).status_code == 200
    conn = env.db()
    rows = conn.execute("SELECT lead_id, processed FROM inbox").fetchall()
    assert len(rows) == 1
    assert rows[0]["lead_id"] == "L4821"
    assert rows[0]["processed"] == 0


def test_webhook_keeps_unattributable_replies(env):
    import webhook
    client = webhook.app.test_client()
    r = client.post("/inbound", json=payload(To="plain@example.test", MessageID="m-2"),
                    headers={"X-Webhook-Secret": SECRET})
    assert r.status_code == 200
    conn = env.db()
    row = conn.execute("SELECT lead_id FROM inbox WHERE msg_id = 'm-2'").fetchone()
    assert row is not None and row["lead_id"] is None


@pytest.mark.parametrize("addr,want", [
    ("reply+L4821@example.test", "L4821"),
    ("Ops <reply+L99@example.test>", "L99"),
    ("plain@example.test", None),
    ("", None),
    (None, None),
    ("broken+@example.test", None),
])
def test_plus_address_parsing(addr, want):
    import webhook
    assert webhook.lead_id_from_address(addr) == want


def test_sender_dry_run_touches_nothing(env, monkeypatch):
    import sender
    conn = sender.db()
    conn.execute("INSERT INTO leads (lead_id, name, domain, email) "
                 "VALUES ('L1', 'Acme', 'acme.test', 'a@acme.test')")
    conn.execute("INSERT INTO send_queue (lead_id, opener, score, selected_via, queued_at, approved) "
                 "VALUES ('L1', 'Hi there', 0.9, 'model', '2026-08-09', 1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sender, "deliver",
                        lambda *a, **k: pytest.fail("dry-run must not call the provider"))
    assert sender.main(["--dry-run"]) == 0

    conn = env.db()
    assert conn.execute("SELECT COUNT(*) FROM send_queue").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_approve_list_runs_on_empty_db(env, capsys):
    import approve
    assert approve.main(["list"]) == 0
    assert "queue" in capsys.readouterr().out.lower()
