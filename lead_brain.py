#!/usr/bin/env python3
"""
lead_brain.py - the learning layer that sits on top of lead_finder.py

The loop:
    discover -> gate -> score -> select (exploit + explore) -> contact
        ^                                                        |
        +----------------- record outcome <----------------------+

Four parts, in order of importance:

  1. Ledger        Store every lead WITH its feature snapshot, and every outcome.
                   Without this nothing else works. This is the whole ballgame.
  2. QualityGate   Hard filters. Not scores - a lead that fails is never sent,
                   regardless of how good it looks. Protects deliverability.
  3. LearnedScorer Logistic regression fit on features -> replied. Replaces your
                   hand-written weights once enough outcomes exist.
  4. MarketBandit  Learns WHICH niche/city to search next, not just which lead
                   to pick from what you already found.

  pip install requests beautifulsoup4 scikit-learn numpy dnspython
"""

import json
import random
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

DB = "leads.db"
NOW = lambda: datetime.now(timezone.utc).isoformat()

# Outcome stages, ordered. Anything >= POSITIVE_AT counts as a success label.
STAGES = ["contacted", "bounced", "opened", "replied", "positive_reply", "meeting", "won"]
POSITIVE_AT = "positive_reply"

# Feature order is FROZEN. Append only - never reorder, or old rows misalign
# with new models and your coefficients become nonsense.
FEATURES = [
    "no_website", "social_only", "unreachable", "no_https", "not_mobile",
    "legacy_html", "years_stale", "weak_platform", "pagespeed",
    "reviews_log", "rating", "has_email", "is_franchise", "agency_built",
]


# ------------------------------------------------------------------ 1. LEDGER

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  lead_id TEXT PRIMARY KEY, name TEXT, domain TEXT, email TEXT, phone TEXT,
  niche TEXT, geo TEXT, features TEXT, heuristic REAL, model_score REAL,
  selected_via TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
  lead_id TEXT, stage TEXT, value REAL DEFAULT 0, ts TEXT,
  PRIMARY KEY (lead_id, stage)
);
CREATE TABLE IF NOT EXISTS suppression (key TEXT PRIMARY KEY, reason TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS sends (ts TEXT, lead_id TEXT, channel TEXT);
"""


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def save_lead(conn, lead_id, name, domain, email, phone, niche, geo,
              features, heuristic, model_score=None, selected_via=""):
    conn.execute(
        "INSERT OR REPLACE INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead_id, name, domain, email, phone, niche, geo,
         json.dumps(features), heuristic, model_score, selected_via, NOW()),
    )
    conn.commit()


def record(conn, lead_id, stage, value=0.0):
    """Log an outcome. This is the function you must actually call - the whole
    system is worthless if outreach results never make it back in here."""
    assert stage in STAGES, f"unknown stage {stage}"
    conn.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?)",
                 (lead_id, stage, value, NOW()))
    if stage == "bounced":
        row = conn.execute("SELECT domain FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
        if row and row["domain"]:
            suppress(conn, row["domain"], "hard bounce")
    conn.commit()


def suppress(conn, key, reason):
    conn.execute("INSERT OR REPLACE INTO suppression VALUES (?,?,?)", (key, reason, NOW()))
    conn.commit()


def label(conn, lead_id):
    """1 if this lead reached positive_reply or beyond, else 0. None if never contacted."""
    rows = {r["stage"] for r in
            conn.execute("SELECT stage FROM outcomes WHERE lead_id=?", (lead_id,))}
    if not rows or "contacted" not in rows:
        return None
    cutoff = STAGES.index(POSITIVE_AT)
    return int(any(STAGES.index(s) >= cutoff for s in rows if s in STAGES))


# ------------------------------------------------------------- 2. QUALITY GATE

class QualityGate:
    """Hard pass/fail. Runs BEFORE scoring. A lead that fails is never contacted.

    This is the answer to 'raw volume is not the solution' - you shrink the
    pool deliberately, and you protect your sending domain while doing it."""

    def __init__(self, conn, min_reviews=8, recontact_days=90):
        self.conn = conn
        self.min_reviews = min_reviews
        self.recontact_days = recontact_days

    def check(self, lead):
        """Return (ok: bool, reason: str)."""
        dom, email = lead.get("domain", ""), lead.get("email", "")

        for key in (dom, email, lead.get("phone", "")):
            if key and self.conn.execute(
                    "SELECT 1 FROM suppression WHERE key=?", (key,)).fetchone():
                return False, "suppressed"

        # Too few reviews = business may be dormant or brand new. Low reviews
        # is the single biggest source of wasted sends.
        if lead.get("reviews", 0) < self.min_reviews:
            return False, f"only {lead.get('reviews',0)} reviews"

        # Franchise: corporate owns the website, the local owner cannot buy from you.
        if lead.get("is_franchise"):
            return False, "franchise - no local site control"

        # Already has an agency. Lookalike-good, converts terribly.
        if lead.get("agency_built"):
            return False, "agency-built site"

        if email and not mx_exists(dom):
            return False, "domain has no MX record"

        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.recontact_days)).isoformat()
        if self.conn.execute(
                "SELECT 1 FROM sends WHERE lead_id=? AND ts>?",
                (lead["lead_id"], cutoff)).fetchone():
            return False, "contacted recently"

        return True, "ok"


def mx_exists(domain):
    """Cheapest possible bounce prevention. Needs: pip install dnspython"""
    if not domain:
        return False
    try:
        import dns.resolver
        return bool(dns.resolver.resolve(domain, "MX"))
    except ImportError:
        return True   # can't check, don't block
    except Exception:
        return False


# ----------------------------------------------------------- 3. LEARNED SCORER

class LearnedScorer:
    """Fits features -> positive_reply. Falls back to your hand weights until
    there's enough signal, then blends in gradually.

    Logistic regression on purpose: with a few hundred rows anything fancier
    overfits, and you want coefficients you can read and sanity-check."""

    MIN_ROWS, MIN_POS, FULL_TRUST_AT = 40, 6, 250

    def __init__(self):
        self.model = None
        self.scaler = None
        self.n = 0

    def vec(self, f):
        return [float(f.get(k, 0) or 0) for k in FEATURES]

    def fit(self, conn):
        X, y = [], []
        for r in conn.execute("SELECT lead_id, features FROM leads"):
            lab = label(conn, r["lead_id"])
            if lab is None:
                continue
            X.append(self.vec(json.loads(r["features"])))
            y.append(lab)

        self.n = len(y)
        if self.n < self.MIN_ROWS or sum(y) < self.MIN_POS:
            self.model = None
            return f"holding: {self.n} labeled / {sum(y) if y else 0} positive " \
                   f"(need {self.MIN_ROWS}/{self.MIN_POS})"

        X, y = np.array(X), np.array(y)
        self.scaler = StandardScaler().fit(X)
        # Strong regularization (low C) because the dataset is small.
        self.model = LogisticRegression(
            class_weight="balanced", C=0.3, max_iter=2000
        ).fit(self.scaler.transform(X), y)
        return f"fit on {self.n} labeled, {int(y.sum())} positive"

    def blend_weight(self):
        """0 -> trust hand weights entirely. 1 -> trust the model entirely."""
        return 0.0 if self.model is None else min(1.0, self.n / self.FULL_TRUST_AT)

    def score(self, features, heuristic):
        if self.model is None:
            return heuristic, 0.0
        p = self.model.predict_proba(self.scaler.transform([self.vec(features)]))[0][1]
        w = self.blend_weight()
        return round((1 - w) * heuristic + w * (p * 100), 1), round(p, 4)

    def explain(self):
        """Read the coefficients. If a sign looks wrong, your labels are wrong."""
        if self.model is None:
            return []
        coefs = self.model.coef_[0]
        return sorted(zip(FEATURES, coefs), key=lambda t: -abs(t[1]))


# ------------------------------------------------------- 4. EXPLORE / EXPLOIT

def select(leads, n, epsilon=0.15):
    """Pick n leads to contact.

    (1-epsilon) go to the top scorers. epsilon go to RANDOM leads below the cut.

    That random slice is not waste - it is the only reason the model keeps
    learning. If you only ever contact leads the model already likes, you never
    observe outcomes for the ones it dislikes, the training set curdles, and the
    model confidently narrows onto a shrinking slice of the market. This one
    parameter is the difference between a system that improves and one that
    slowly strangles itself."""
    ranked = sorted(leads, key=lambda l: -l["score"])
    n_exploit = int(n * (1 - epsilon))
    chosen = ranked[:n_exploit]
    for l in chosen:
        l["selected_via"] = "exploit"
    rest = ranked[n_exploit:]
    if rest:
        for l in random.sample(rest, min(n - n_exploit, len(rest))):
            l["selected_via"] = "explore"
            chosen.append(l)
    return chosen


class MarketBandit:
    """Learns which (niche, geo) markets are worth searching at all.

    Thompson sampling over a Beta posterior per market. Naturally spends more
    on proven markets while still probing thin ones, and needs no tuning."""

    def __init__(self, conn):
        self.conn = conn

    def stats(self):
        out = {}
        for r in self.conn.execute("SELECT lead_id, niche, geo FROM leads"):
            lab = label(self.conn, r["lead_id"])
            if lab is None:
                continue
            k = (r["niche"], r["geo"])
            w, l = out.get(k, (0, 0))
            out[k] = (w + lab, l + (1 - lab))
        return out

    def next_market(self, candidates):
        """candidates: list of (niche, geo). Returns the one to search next."""
        s = self.stats()
        best, best_draw = None, -1
        for arm in candidates:
            wins, losses = s.get(arm, (0, 0))
            draw = np.random.beta(1 + wins, 1 + losses)  # unseen arms start optimistic
            if draw > best_draw:
                best, best_draw = arm, draw
        return best

    def report(self):
        rows = []
        for (niche, geo), (w, l) in sorted(
                self.stats().items(), key=lambda kv: -(kv[1][0] / max(kv[1][0] + kv[1][1], 1))):
            n = w + l
            rows.append((niche, geo, n, w, w / n if n else 0))
        return rows


# ------------------------------------------------------------ 5. SEND GOVERNOR

class SendGovernor:
    """Volume caps and a bounce circuit breaker. Deliverability is the real
    constraint on this business, not lead supply."""

    def __init__(self, conn, daily_cap=40, bounce_limit=0.03, window=200):
        self.conn, self.cap = conn, daily_cap
        self.bounce_limit, self.window = bounce_limit, window

    def sent_today(self):
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat()
        return self.conn.execute("SELECT COUNT(*) c FROM sends WHERE ts>?", (start,)).fetchone()["c"]

    def bounce_rate(self):
        recent = [r["lead_id"] for r in self.conn.execute(
            "SELECT lead_id FROM sends ORDER BY ts DESC LIMIT ?", (self.window,))]
        if len(recent) < 30:
            return 0.0
        q = ",".join("?" * len(recent))
        b = self.conn.execute(
            f"SELECT COUNT(*) c FROM outcomes WHERE stage='bounced' AND lead_id IN ({q})",
            recent).fetchone()["c"]
        return b / len(recent)

    def budget(self):
        """How many sends are allowed right now, and why."""
        br = self.bounce_rate()
        if br > self.bounce_limit:
            return 0, f"HALTED - bounce rate {br:.1%} over {self.bounce_limit:.0%} limit"
        remaining = max(0, self.cap - self.sent_today())
        return remaining, f"{remaining} left today (bounce {br:.1%})"

    def log_send(self, lead_id, channel="email"):
        self.conn.execute("INSERT INTO sends VALUES (?,?,?)", (NOW(), lead_id, channel))
        self.conn.commit()


# ------------------------------------------------------------------- 6. HEALTH

def health(conn, scorer):
    """Run weekly. Catches the failure modes before they compound."""
    out = []
    total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    labeled = sum(1 for r in conn.execute("SELECT lead_id FROM leads")
                  if label(conn, r["lead_id"]) is not None)
    out.append(f"leads={total} labeled={labeled} ({labeled/max(total,1):.0%})")

    ex = conn.execute("SELECT selected_via, COUNT(*) c FROM leads GROUP BY selected_via").fetchall()
    mix = {r["selected_via"]: r["c"] for r in ex}
    explore_pct = mix.get("explore", 0) / max(sum(mix.values()), 1)
    out.append(f"explore share={explore_pct:.0%}" +
               ("  <-- TOO LOW, model will ossify" if explore_pct < 0.08 else ""))

    if scorer.model is not None:
        out.append(f"model trust={scorer.blend_weight():.0%}")
        for feat, c in scorer.explain()[:5]:
            out.append(f"   {feat:>14s} {c:+.2f}")
    return out
