"""Orchestrator — run this twice a day (morning + afternoon).
Pipeline: discover -> dedupe/fresh -> score (local) -> persist -> export Tier-A Claude prompts.
Submission stays manual (open the review cockpit with: streamlit run app.py)."""
import os
import db, ingest, score, tailor, connectors

HERE = os.path.dirname(__file__)
PROMPTS = os.path.join(HERE, "outputs", "prompts")

def main(on_progress=None):
    import settings
    cfg = settings.load_config()
    db.init_db()

    print("1) Discovering jobs from public ATS boards ...")
    jobs, report = ingest.discover(cfg.get("sources", {}),
                           cfg.get("role_keywords", []),
                           cfg.get("freshness_days", 21),
                           cfg.get("locations", []),
                           cfg.get("remote_ok", True),
                           strict_title=cfg.get("strict_title_filter", False),
                           on_progress=on_progress)
    # ----- Web-wide layer: JSearch (Google for Jobs / open web), if enabled -----
    js_jobs, js_report = connectors.jsearch_discover(
        cfg, cfg.get("role_keywords", []), cfg.get("locations", []),
        cfg.get("freshness_days", 21), cfg.get("remote_ok", True),
        strict_title=cfg.get("strict_title_filter", False), on_progress=on_progress)
    if js_jobs:
        existing = {(j["company"].lower(), j["title"].strip().lower()) for j in jobs}
        added = 0
        for j in js_jobs:
            k = (j["company"].lower(), j["title"].strip().lower())
            if k not in existing:
                existing.add(k); jobs.append(j); added += 1
        print(f"   -> JSearch added {added} new web-wide roles ({len(js_jobs)-added} dupes of ATS)")
    report = (report or []) + js_report

    for j in jobs:
        db.upsert_job(j)
    print(f"   -> {len(jobs)} fresh, deduped, filtered postings ingested")

    todo = db.unscored_jobs()
    print(f"2) Scoring {len(todo)} new jobs with local embeddings (no tokens) ...")
    if todo:
        for j, sc in score.score_jobs(todo, cfg, on_progress=on_progress):
            db.save_score(j["content_hash"], sc)

    print("3) Exporting Claude Pro prompts for Tier-A roles ...")
    n_prompts = 0
    for row in db.ranked_jobs(tier="A"):
        if not row.get("prompt_path"):
            job = {"company": row["company"], "title": row["title"],
                   "location": row["location"], "url": row["url"],
                   "jd_text": row["jd_text"]}
            sc = {"missing_keywords": _loads(row["missing_keywords"]),
                  "missing_skills": _loads(row["missing_skills"])}
            p = tailor.save_prompt(job, sc, PROMPTS)
            db.set_app(row["content_hash"], prompt_path=p)
            n_prompts += 1
    print(f"   -> {n_prompts} new Tier-A prompts written to outputs/prompts/")

    s = db.stats()
    print("\nDONE.", "Total jobs:", s["total"], "| Tiers:", s["by_tier"],
          "| Applied:", s["applied"])
    print("Open the cockpit:  streamlit run app.py")
    return {"report": report, "ingested": len(jobs), "scored": len(todo), "stats": s}

def _loads(x):
    import json
    try: return json.loads(x) if x else []
    except Exception: return []

if __name__ == "__main__":
    main()
