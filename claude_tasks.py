#!/usr/bin/env python3
"""
claude_tasks.py - the three places an LLM actually earns its cost.

Deliberately NOT here: scoring, scheduling, ranking, DB work. Your logistic
regression is cheaper, deterministic, and you can audit its coefficients.
Do not put an LLM in the scoring path.

  pip install anthropic
  export ANTHROPIC_API_KEY=...
"""

import json
import os
import re

MODEL = "claude-sonnet-4-6"


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _ask(system, prompt, max_tokens=1200, temperature=0.0):
    msg = _client().messages.create(
        model=MODEL, max_tokens=max_tokens, temperature=temperature,
        system=system, messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def _json(text):
    """Models sometimes wrap JSON in fences despite instructions. Strip and parse."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON found in: {text[:200]}")
    return json.loads(text[start:end + 1])


# ------------------------------------------------------------ 1. REPLY TRIAGE

TRIAGE_CATEGORIES = [
    "positive",        # wants to talk -> the label your model trains on
    "question",        # engaged but not committed -> human should answer
    "not_interested",  # soft no
    "remove_me",       # explicit opt-out -> MUST suppress, legal requirement
    "auto_reply",      # out-of-office, ticket ack -> NOT a real reply
    "wrong_person",    # forwarded / not the decision maker
    "hostile",         # complaint or spam accusation -> suppress + investigate
]

TRIAGE_SYSTEM = f"""You classify replies to cold outreach emails offering web design services.

Return ONLY a JSON object, no prose, no markdown fences:
{{"category": one of {TRIAGE_CATEGORIES},
  "confidence": 0.0-1.0,
  "suppress": true/false,
  "needs_human": true/false,
  "summary": "one short sentence"}}

Rules that matter:
- An out-of-office or automated ticket acknowledgement is "auto_reply", NEVER
  "positive", no matter how warm the wording sounds. This distinction is the
  entire point of this task.
- Any opt-out phrasing ("unsubscribe", "stop", "take me off") is "remove_me"
  with suppress=true, even if the rest of the message is friendly.
- "hostile" always sets suppress=true and needs_human=true.
- If confidence is below 0.7, set needs_human=true.
- Vague positives ("sure, send info") are "question", not "positive". Reserve
  "positive" for clear intent to talk, meet, or receive a quote."""


def classify_reply(subject, body):
    """Returns a dict. Feed 'positive' results into lead_brain.record(...)."""
    out = _ask(TRIAGE_SYSTEM,
               f"Subject: {subject}\n\nBody:\n{body[:3000]}",
               max_tokens=300)
    # Never let a malformed response crash the job or silently drop a reply.
    # Unparseable output routes to a human rather than guessing a label.
    try:
        d = _json(out)
    except (ValueError, json.JSONDecodeError):
        d = {}
    if d.get("category") not in TRIAGE_CATEGORIES:
        d = {"category": "question", "confidence": 0.0, "suppress": False,
             "needs_human": True, "summary": "unparseable - routed to human"}
    if d.get("confidence", 1.0) < 0.7:
        d["needs_human"] = True
    return d


# ----------------------------------------------------------- 2. OPENER DRAFT

OPENER_SYSTEM = """You write the opening line of a cold email from a web designer
to a local business owner.

Constraints:
- ONE or TWO sentences. Under 30 words.
- Reference the SPECIFIC technical finding you are given. Never generic praise.
- Plain language a contractor would use. No marketing voice, no "I hope this
  finds you well", no "I noticed you have an online presence".
- Do not claim to have done work you have not done.
- Never invent facts not present in the findings given to you.

Return only the line itself."""


def draft_opener(name, niche, findings):
    """findings: list of strings from your audit, e.g. ['not mobile-responsive']"""
    return _ask(
        OPENER_SYSTEM,
        f"Business: {name} ({niche})\nAudit findings: {'; '.join(findings)}",
        max_tokens=150, temperature=0.7,
    ).strip()


# ------------------------------------------------------------- 3. THE REPORT

REPORT_SYSTEM = """You write a short weekly operations brief for the owner of a
one-person web design business who runs an automated lead pipeline.

Structure:
1. One-line headline: what actually matters this week.
2. What changed vs the prior period, with numbers.
3. Anything that needs a DECISION from him, stated as a question. If nothing
   does, say so plainly.
4. Anomalies or warnings.

Rules:
- Lead with problems, not vanity metrics.
- If a metric moved on a small sample, say the sample is small rather than
  presenting it as a trend. Do not manufacture insight from noise.
- If the data shows nothing notable, say the week was uneventful. A boring
  week reported honestly is more useful than invented narrative.
- Under 250 words. Plain text, no headers.
- Never invent numbers not present in the data given."""


def write_report(stats):
    return _ask(REPORT_SYSTEM,
                f"This week's pipeline data:\n{json.dumps(stats, indent=2)}",
                max_tokens=800, temperature=0.3).strip()
