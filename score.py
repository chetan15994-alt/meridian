"""Matching brain: 100% local, 100% free, zero LLM tokens.
Fit = weighted(semantic similarity, JD-keyphrase coverage, skills match, seniority match)."""
import re, os, functools
import numpy as np
from sklearn.feature_extraction.text import CountVectorizer

_HERE = os.path.dirname(__file__)
_model = None

SENIORITY = {"intern":0,"junior":1,"associate":1,"mid":2,"senior":3,
             "staff":4,"lead":4,"principal":5,"director":6,"vp":7,"head":6}

def _load_model(name):
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        print(f"  loading embedding model: {name} (first run downloads it once)")
        _model = SentenceTransformer(name)
    return _model

def _emb(texts):
    return _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

def _cos(a, b):  # a,b normalized
    return float(np.dot(a, b))

@functools.lru_cache(maxsize=1)
def resume_blob():
    import fileio
    d = fileio.read_yaml(os.path.join(_HERE, "resume_master.yaml"), default={}) or {}
    parts = [d.get("summary","")]
    parts += d.get("skills", [])
    for e in d.get("experiences", []):
        parts += e.get("bullets", [])
    return " ".join(parts).lower()

def extract_keyphrases(text, top_n=20):
    """Mini-KeyBERT: candidate n-grams ranked by embedding similarity to the JD."""
    try:
        vec = CountVectorizer(ngram_range=(1,3), stop_words="english",
                              max_features=400).fit([text])
    except ValueError:
        return []
    cands = list(vec.get_feature_names_out())
    if not cands: return []
    doc = _emb([text])[0]
    cse = _emb(cands)
    sims = cse @ doc
    order = np.argsort(-sims)[:top_n]
    return [cands[i] for i in order]

def detect_skills(text, lexicon):
    t = text.lower()
    return {s for s in lexicon if s.lower() in t}

def detect_seniority(text):
    t = text.lower()
    found = [lvl for kw, lvl in SENIORITY.items() if re.search(rf"\b{kw}\b", t)]
    return max(found) if found else 2  # default mid

def seniority_score(jd_text, targets):
    """targets: a level string or list of levels. Score = best match to any selected level."""
    if isinstance(targets, str): targets = [targets]
    if not targets: return 100.0
    got = detect_seniority(jd_text)
    tgt_ranks = [SENIORITY.get(t, 3) for t in targets]
    gap = min(abs(got - tr) for tr in tgt_ranks)
    return max(0.0, 1 - gap/3) * 100  # exact match to any selected level = 100

def score_jobs(jobs, cfg, on_progress=None):
    """Annotate each job dict with scores; returns list of (job, score_dict)."""
    _load_model(cfg.get("embedding_model","sentence-transformers/all-MiniLM-L6-v2"))
    lexicon = set(x.lower() for x in cfg.get("skills_lexicon", []))
    import fileio
    rd = fileio.read_yaml(os.path.join(_HERE, "resume_master.yaml"), default={}) or {}
    lexicon |= set(s.lower() for s in rd.get("skills", []))

    rblob = resume_blob()
    r_emb = _emb([rblob])[0]
    r_skills = detect_skills(rblob, lexicon)
    w = cfg["weights"]; tiers = cfg["tiers"]
    priority = set(c.lower() for c in cfg.get("priority_companies", []))

    results = []
    total = len(jobs) or 1
    for idx, j in enumerate(jobs):
        if on_progress: on_progress("score", idx, total, j.get("title",""))
        jd = j["jd_text"]
        j_emb = _emb([jd])[0]
        sem = max(0.0, _cos(r_emb, j_emb)) * 100

        kps = extract_keyphrases(jd, top_n=20)
        covered = [k for k in kps if k.lower() in rblob]
        kw = (len(covered)/len(kps)*100) if kps else 0.0
        missing_kw = [k for k in kps if k.lower() not in rblob][:10]

        jd_skills = detect_skills(jd, lexicon)
        have = jd_skills & r_skills
        sk = (len(have)/len(jd_skills)*100) if jd_skills else 50.0
        missing_sk = sorted(jd_skills - r_skills)[:10]

        sen = seniority_score(jd, cfg.get("target_seniority", ["senior"]))

        fit = (w["semantic"]*sem + w["keywords"]*kw +
               w["skills"]*sk + w["seniority"]*sen)
        fit = round(fit, 1)

        if j["company"].lower() in priority:
            tier = "A"
        elif fit >= tiers["A"]:
            tier = "A"
        elif fit >= tiers["B"]:
            tier = "B"
        else:
            tier = "C"

        bits = []
        if sem >= 50: bits.append("strong semantic fit")
        if have: bits.append("skills match: " + ", ".join(sorted(have)[:4]))
        if missing_sk: bits.append("gaps: " + ", ".join(missing_sk[:4]))
        rationale = "; ".join(bits) or "low overall match"

        results.append((j, {
            "fit": fit, "semantic": round(sem,1), "keywords": round(kw,1),
            "skills": round(sk,1), "seniority": round(sen,1), "tier": tier,
            "missing_keywords": missing_kw, "missing_skills": missing_sk,
            "rationale": rationale}))
    if on_progress: on_progress("score", total, total, "done")
    return results
