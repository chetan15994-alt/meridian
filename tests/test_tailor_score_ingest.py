"""tailor (fact-integrity guard + prep prompts), score (offline pipeline),
ingest (freshness parsing + parallel discover determinism)."""
import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

import ingest
import score
import tailor


# ---------------- tailor: the fact-integrity guard ----------------
def test_guard_rejects_tampered_facts():
    fake_llm = {"summary": "s", "skills": ["A"],
                "experiences": [{"company": "BYJU'S", "role": "HACKED",
                                 "dates": "1999", "bullets": ["b"]}],
                "education": [{"degree": "FAKE PhD"}],
                "certifications": ["FAKE CERT"],
                "cover_letter": "c"}
    out = tailor.assemble_tailored(fake_llm, {"company": "X", "title": "Y"})
    master = tailor._resume()
    assert out["education"] == master.get("education", [])          # not FAKE
    assert out["certifications"] == master.get("certifications", [])
    for e in out["experiences"]:
        assert e["role"] != "HACKED" and e["dates"] != "1999"


def test_guard_survives_empty_llm_object():
    out = tailor.assemble_tailored({}, {"company": "X", "title": "Y"})
    assert out["summary"] == tailor._resume().get("summary", "")


def test_prep_prompt_has_both_formats_and_antihallucination():
    qs = [{"key": "k", "label": "Why?", "type": "textarea", "options": [],
           "required": True, "kind": "freetext"}]
    r = {"summary": "s", "skills": [], "experiences": []}
    job = {"company": "X", "title": "Y", "jd_text": "z"}
    pj = tailor.build_prep_answers_prompt(job, r, qs, {}, as_json=True)
    pt = tailor.build_prep_answers_prompt(job, r, qs, {}, as_json=False)
    assert '{"answers"' in pj and "ONLY valid JSON" in pj
    assert "### <question label>" in pt
    assert "never fabricate" in pj.lower() and "never fabricate" in pt.lower()


# ---------------- score: offline pipeline with a stub embedder ----------------
class _Stub:
    def encode(self, texts, **kw):
        v = HashingVectorizer(n_features=384, alternate_sign=False, norm="l2")
        return v.transform(texts).toarray().astype("float32")


def test_score_pipeline_runs_offline(monkeypatch):
    monkeypatch.setattr(score, "_model", _Stub())
    monkeypatch.setattr(score, "_emb", lambda t: _Stub().encode(t))
    score.resume_blob.cache_clear()
    cfg = {"weights": {"semantic": .45, "keywords": .3, "skills": .15, "seniority": .1},
           "tiers": {"A": 70, "B": 55}, "skills_lexicon": ["genai"],
           "target_seniority": ["senior"], "priority_companies": []}
    jobs = [{"hash": "h", "company": "A", "title": "Senior PM", "location": "BLR",
             "url": "u", "jd_text": "GenAI PM role", "source": "greenhouse",
             "posted_at": "", "content_hash": "h"}]
    out = list(score.score_jobs(jobs, cfg))
    assert len(out) == 1
    job, sc = out[0]
    assert 0 <= sc["fit"] <= 100 and sc["tier"] in "ABC"


# ---------------- ingest: freshness + deterministic parallel discover ----------
def test_fresh_handles_aware_z_and_stale():
    assert ingest._fresh({"posted_at": "2099-06-01T12:00:00-04:00"}, 30)
    assert ingest._fresh({"posted_at": "2099-06-01T12:00:00Z"}, 30)
    assert not ingest._fresh({"posted_at": "2000-01-01T00:00:00"}, 30)
    assert ingest._fresh({"posted_at": ""}, 30)   # unknown -> keep


def test_parallel_discover_is_deterministic_and_dedupes(monkeypatch):
    """Two tokens fetched CONCURRENTLY must yield the same jobs, in the same
    order, with cross-token dedup — identical to the old serial behaviour."""
    def mk(company, title, jd="senior product manager role in India"):
        return ingest._norm("greenhouse", company, title, "Bengaluru, India",
                            "http://u", jd, "")
    fake = {"a": [mk("a", "Senior Product Manager"), mk("a", "Junior Dev")],
            "b": [mk("b", "Senior Product Manager"),
                  mk("a", "Senior Product Manager")]}       # dup of a's job
    monkeypatch.setitem(ingest.FETCHERS, "greenhouse", lambda tok: fake[tok])
    runs = []
    for _ in range(3):
        jobs, report = ingest.discover({"greenhouse": ["a", "b"]},
                                       ["product manager"], 999, ["India"], True)
        runs.append([j["content_hash"] for j in jobs])
    assert runs[0] == runs[1] == runs[2]                     # deterministic
    assert len(runs[0]) == len(set(runs[0]))                 # deduped
    kept = {r["token"]: r["kept"] for r in report}
    assert kept["a"] == 1 and kept["b"] == 1                 # dup dropped in b
