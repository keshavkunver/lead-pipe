"""Regression guard for the triage prompt behind claude_tasks.classify_reply.

Costs real API calls, so it is excluded from the normal pytest sweep.
Run it deliberately:

    RUN_TRIAGE_TESTS=1 pytest tests/test_triage_fixtures.py -s
    python tests/test_triage_fixtures.py        # same thing, no pytest

Fixture labels come from claude_tasks.TRIAGE_CATEGORIES (positive,
question, not_interested, remove_me, auto_reply, wrong_person, hostile).
Out-of-office fixtures (ooo-*) are labeled auto_reply — that taxonomy
has no separate out_of_office bucket. test_smoke.py validates the labels
offline, so a taxonomy drift is caught without spending API calls.
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pytest

FIXTURE_PATH = Path(__file__).with_name("triage_fixtures.json")

# The one mistake that silently poisons the training labels: an
# autoresponder (out-of-office or ticket ack, both labeled auto_reply)
# counted as a positive reply teaches the learned scorer that dead
# inboxes are great leads. That is a hard failure, always.
NEVER_POSITIVE = {"auto_reply"}

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_TRIAGE_TESTS"),
    reason="calls the Anthropic API; set RUN_TRIAGE_TESTS=1 to run",
)


def run_fixtures():
    from claude_tasks import classify_reply

    fixtures = json.loads(FIXTURE_PATH.read_text())
    return [(fx, classify_reply(fx["subject"], fx["body"])) for fx in fixtures]


def report(results) -> float:
    right = sum(1 for fx, got in results if got["category"] == fx["expected"])
    acc = right / len(results)
    print(f"\naccuracy: {right}/{len(results)} = {acc:.0%}\n")

    cats = sorted({fx["expected"] for fx, _ in results} | {g["category"] for _, g in results})
    confusion = Counter((fx["expected"], got["category"]) for fx, got in results)
    print("confusion (rows = expected, cols = got)")
    print(" " * 14 + " ".join(f"{c[:6]:>6}" for c in cats))
    for exp in cats:
        cells = " ".join(f"{confusion.get((exp, g), 0) or '.':>6}" for g in cats)
        print(f"{exp[:13]:<13} {cells}")

    for fx, got in results:
        if got["category"] != fx["expected"]:
            print(f"MISS {fx['id']}: expected {fx['expected']}, "
                  f"got {got['category']} ({fx['subject']!r})")
    return acc


def test_triage_fixtures():
    results = run_fixtures()
    acc = report(results)

    poisoned = [
        f"{fx['id']} (expected {fx['expected']}) classified positive: {fx['subject']!r}"
        for fx, got in results
        if fx["expected"] in NEVER_POSITIVE and got["category"] == "positive"
    ]
    assert not poisoned, (
        "LABEL POISON — autoresponders classified as positive replies:\n  "
        + "\n  ".join(poisoned)
    )

    # Opt-outs and complaints must carry suppress=true — remove_me is a
    # legal requirement, not a preference (see the triage prompt's rules).
    unsuppressed = [
        f"{fx['id']}: category {got['category']} but suppress={got.get('suppress')}"
        for fx, got in results
        if fx["expected"] in ("remove_me", "hostile")
        and got["category"] == fx["expected"] and not got.get("suppress")
    ]
    assert not unsuppressed, (
        "opt-outs/complaints missing suppress=true:\n  " + "\n  ".join(unsuppressed)
    )

    # Soft floor so broad drift also fails; raise it as the prompt matures.
    assert acc >= 0.7, f"triage accuracy regressed to {acc:.0%}"


if __name__ == "__main__":
    os.environ.setdefault("RUN_TRIAGE_TESTS", "1")
    sys.path.insert(0, str(FIXTURE_PATH.parent.parent))
    test_triage_fixtures()
    print("\nOK")
