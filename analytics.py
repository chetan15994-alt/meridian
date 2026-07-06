"""Pure analytics + learning-loop logic over application data.
No Streamlit, no DB — operates on plain row dicts so it's unit-testable."""
import statistics as stt

STAGES = ["applied", "screen", "interview", "offer"]
_RANK = {"applied":0, "screen":1, "interview":2, "offer":3, "rejected":0, "not_applied":-1}

def _responded(status): return _RANK.get(status, 0) >= 1

def applied_only(rows):
    return [r for r in rows if r.get("status") and r["status"] != "not_applied"]

def funnel(rows):
    """Counts of applications that reached >= each stage, plus total."""
    apps = applied_only(rows)
    counts = {s: 0 for s in STAGES}
    for a in apps:
        rk = _RANK.get(a["status"], 0)
        for i, s in enumerate(STAGES):
            if rk >= i: counts[s] += 1
    return counts, len(apps)

def conversion_by(rows, key):
    """Group applied rows by a key (tier/variant/source); response + interview rates."""
    apps = applied_only(rows)
    g = {}
    for a in apps:
        k = a.get(key) or "—"
        d = g.setdefault(k, {"n":0, "responded":0, "interview":0})
        d["n"] += 1
        rk = _RANK.get(a["status"], 0)
        if rk >= 1: d["responded"] += 1
        if rk >= 2: d["interview"] += 1
    for d in g.values():
        d["resp_rate"] = round(100*d["responded"]/d["n"], 1) if d["n"] else 0.0
        d["int_rate"]  = round(100*d["interview"]/d["n"], 1) if d["n"] else 0.0
    return g

def suggest_weights(rows, current, min_per_group=3):
    """Learning loop: compare mean sub-scores of responders vs non-responders and nudge
    the scoring weights toward features that correlate with replies. Returns None if the
    sample is too small to be meaningful (kept deliberately conservative)."""
    apps = applied_only(rows)
    resp = [a for a in apps if _responded(a["status"])]
    non  = [a for a in apps if not _responded(a["status"])]
    if len(resp) < min_per_group or len(non) < min_per_group:
        return None
    feats = ["semantic", "keywords", "skills", "seniority"]
    gaps = {f: stt.mean([float(a[f]) for a in resp]) - stt.mean([float(a[f]) for a in non])
            for f in feats}
    new = {f: max(0.0, float(current.get(f, 0.25)) + (gaps[f]/100.0)*0.5) for f in feats}
    tot = sum(new.values()) or 1.0
    new = {f: round(new[f]/tot, 3) for f in feats}
    return {"weights": new, "gaps": {f: round(gaps[f],1) for f in feats},
            "n_resp": len(resp), "n_non": len(non)}
