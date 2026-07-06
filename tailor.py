"""Writing brain.
 Tier A  -> build a high-quality prompt you paste into Claude Pro (best quality, 0 tokens).
 Tier B  -> no-LLM template tailoring: pick your most relevant REAL bullets + flag keywords.
Nothing here invents content."""
import os, re, datetime
import numpy as np

_HERE = os.path.dirname(__file__)

def _resume():
    import fileio
    return fileio.read_yaml(os.path.join(_HERE, "resume_master.yaml"), default={}) or {}

# ---------------- Tier A: Claude Pro prompt ----------------
# ─────────────────────────────────────────────────────────────────────────────
# SHARED TAILORING SPEC — one source of truth for BOTH the API path and the
# Claude Pro (manual) path, so the two never drift in quality or behaviour.
# ─────────────────────────────────────────────────────────────────────────────
_ROLE = (
"You are an elite executive resume writer who has placed senior product managers at top "
"technology companies. Your craft is tailoring a candidate's master resume to a specific role "
"so it earns a shortlist — sharp, senior, and credible — without ever misrepresenting them."
)

_PRINCIPLES = """CORE PRINCIPLES (follow all):
1. TRUTH IS ABSOLUTE. Use ONLY facts present in the master resume. Never invent or inflate
   employers, titles, dates, metrics, tools, or skills. If the JD wants something the candidate
   does not have, do NOT fake it — surface their closest genuine strength instead.
2. PRESERVE IDENTITY & STRUCTURE. Keep company names, role titles, employment dates, education
   and certifications EXACTLY as in the master. Keep the same sections and overall structure.
   You may only reshape: the SUMMARY, the ORDER of skills, and the SELECTION/WORDING of bullets.
3. TAILOR WITH INTENT:
   • Summary — 2-3 crisp lines repositioning the candidate for THIS role, leading with their single
     most relevant strength and mirroring the JD's framing and seniority. Confident, not generic.
   • Skills — the same real skills, reordered so the ones this JD cares about appear first. Add none
     that aren't already true; drop none that are.
   • Experience bullets — for each role, choose the 3-5 strongest bullets for THIS JD, lead with the
     most relevant, and rephrase each to: (a) open with a strong action verb, (b) mirror the JD's
     vocabulary where truthful, (c) weave in the under-used JD keywords ONLY where the underlying
     work genuinely supports them, and (d) KEEP every real metric the master already states.
4. QUALITY BAR. Every bullet = strong verb + what was done + a concrete or measurable outcome.
   No filler ("responsible for", "worked on", "helped with"). Specific over generic. One line each.
5. ATS-SAFE. Plain text, standard section names, no tables, columns, or graphics.
6. VOICE. Confident, precise, senior — professional, never boastful, and naturally human."""

def _exp_block(r):
    out = []
    for e in r["experiences"]:
        out.append(f"\n{e['company']} — {e['role']} ({e['dates']})")
        for b in e["bullets"]:
            out.append(f"  - {b}")
    return "".join(out)

def _shared_context(job, score, r):
    miss = ", ".join(score.get("missing_keywords", []) + score.get("missing_skills", []))
    return f"""# TARGET ROLE
Company: {job['company']}
Title:   {job['title']}
Location: {job.get('location','')}
{('Link: ' + job['url']) if job.get('url') else ''}

# JOB DESCRIPTION
{job['jd_text'][:4000]}

# MY MASTER RESUME (the ONLY source of truth — never exceed these facts)
Summary: {r['summary']}
Skills:  {", ".join(r['skills'])}
Experience:{_exp_block(r)}

# JD KEYWORDS MY RESUME UNDER-USES (weave in ONLY where genuinely true)
{miss or "(none detected)"}"""


def build_claude_prompt(job, score):
    """Manual mode — a rich, copy-paste-ready prompt for Claude Pro. Produces the SAME tailored
    sections as the API path so quality is identical across modes."""
    r = _resume()
    return f"""{_ROLE}

{_PRINCIPLES}

{_shared_context(job, score, r)}

# WHAT TO PRODUCE (clean, copy-paste-ready, in this exact order)
**Tailored Summary** — 2-3 lines aimed at this role.
**Core Competencies** — one line, my real skills reordered for this JD.
**Professional Experience** — for EACH company (keep names/titles/dates unchanged), 3-5 tailored
  bullets selected and rephrased from my real bullets per the principles above.
**Fit Notes** — 2-3 sentences: my strongest match for this role and the one gap to address.
**Cover Letter** — 120-160 words, warm and specific, grounded only in my real background.

Keep it ATS-friendly (plain text, standard headings). Do not invent anything."""


def build_structured_prompt(job, score):
    """API mode — same spec, but returns strict JSON for deterministic assembly into the .docx."""
    r = _resume()
    schema = ('{"summary": str, "skills": [str], '
              '"experiences": [{"company": str, "bullets": [str]}], '
              '"cover_letter": str, "fit_notes": str}')
    return f"""{_ROLE}

{_PRINCIPLES}

Return ONLY valid JSON (no markdown fences, no commentary) with EXACTLY this schema:
{schema}

JSON rules:
- "experiences": one object per company in the master, in the SAME order, each with 3-5 tailored
  bullets. Use the master's exact company names as the "company" value (roles/dates are re-attached
  automatically, so omit them).
- "skills": my real skills, reordered for this JD.
- "cover_letter": 120-160 words, specific and grounded only in my real background.
- "fit_notes": 2-3 sentences — strongest match + one gap to address.

{_shared_context(job, score, r)}"""


def tailor_structured(job, score, cfg):
    """Returns (llm_obj, usage). Raises llm.LLMError on failure."""
    import llm
    tc = cfg.get("tailoring", {})
    return llm.chat_json(build_structured_prompt(job, score),
                         tc.get("base_url"), tc.get("model"), api=tc.get("api","openai"),
                         api_key=llm.get_api_key())

def assemble_tailored(llm_obj, job):
    """Merge LLM output with master facts. Roles, dates, contact, education and
    certifications ALWAYS come from the master resume (integrity); only summary,
    skills, bullets, cover letter come from the model."""
    r = _resume()
    llm_by = {x.get("company","").lower(): x for x in llm_obj.get("experiences", [])}
    exps = []
    for e in r["experiences"]:
        lo = llm_by.get(e["company"].lower())
        bullets = (lo.get("bullets") if lo and lo.get("bullets") else e["bullets"])
        exps.append({"company":e["company"], "role":e["role"],
                     "dates":e["dates"], "bullets":bullets})
    return {"name":r["name"], "title":r["title"], "contact":r["contact"],
            "summary": llm_obj.get("summary") or r["summary"],
            "skills":  llm_obj.get("skills") or r["skills"],
            "experiences": exps, "education": r["education"],
            "certifications": r.get("certifications", []),
            "cover_letter": llm_obj.get("cover_letter",""),
            "fit_notes": llm_obj.get("fit_notes",""),
            "tailored_for": f"{job['company']} - {job['title']}"}

# ---------------- Application screening answers ----------------
def build_screening_prompt(job):
    r = _resume()
    return f"""Using ONLY my real background, draft concise application answers for this role.
Return plain text with two short labelled paragraphs:
"Why this company/role:" (3-4 sentences, specific to the JD)
"First 90 days focus:" (3-4 sentences)
Be specific and truthful — no invented facts.

ROLE: {job['company']} - {job['title']}
JD: {job['jd_text'][:3000]}
MY SUMMARY: {r['summary']}
MY SKILLS: {', '.join(r['skills'])}
"""

# ---------------- Interview prep notes ----------------
def build_prep_prompt(job):
    r = _resume()
    return f"""Act as an interview coach. From this JD and my real background, produce:
1) Six likely interview questions specific to THIS role.
2) For each, a 2-sentence talking point drawing on my real experience.
3) Three sharp questions I should ask them.
Use only my real background; be specific to the JD; no invented facts.

ROLE: {job['company']} - {job['title']}
JD: {job['jd_text'][:3000]}
MY SUMMARY: {r['summary']}
MY SKILLS: {', '.join(r['skills'])}
"""

# ---------------- Outreach message draft ----------------
def build_outreach_prompt(job):
    r = _resume()
    return f"""Draft a concise LinkedIn outreach message (90-120 words) to a recruiter or
hiring manager for this role. Reference ONE concrete reason I'm a fit, grounded in my real
background. Warm, professional, specific, with a clear ask for a short chat. No invented facts.

ROLE: {job['company']} - {job['title']}
JD excerpt: {job['jd_text'][:1500]}
MY SUMMARY: {r['summary']}
MY SKILLS: {', '.join(r['skills'])}
"""


# ---------------- Completeness fix generation (point 4) ----------------
def build_fix_prompt(issue, resume):
    """Draft a concrete improvement for a single completeness issue. Returns text to review."""
    field = issue.get("field",""); msg = issue.get("message","")
    return f"""You are improving one specific weakness in a product manager's resume.
Use ONLY the candidate's real background below — never invent facts, metrics, employers or skills.

WEAKNESS TO FIX
field: {field}
problem: {msg}

CANDIDATE RESUME (source of truth)
summary: {resume.get('summary','')}
skills: {", ".join(resume.get('skills',[]))}
experiences: {[{'company':e['company'],'role':e['role'],'bullets':e['bullets']} for e in resume.get('experiences',[])]}

YOUR TASK
Provide a concrete, ready-to-use replacement for ONLY the weak item (a rewritten bullet, summary,
or skill phrasing). If the fix needs a metric the candidate hasn't provided, write the line so a
number can be slotted in and mark it as [add metric]. Keep it truthful, senior, and ATS-clean.
Return just the improved text — no preamble."""


def build_answer_prompt(question, job, resume, saved_answers):
    """Generate an application-question answer grounded in CV + JD + the user's prior answers."""
    prior = "\n".join(f"- Q: {q}\n  A: {a}" for q,a in (saved_answers or {}).items()) or "(none yet)"
    return f"""You are helping a senior product manager answer a job application question in their
own authentic voice. Use ONLY their real background — never fabricate.

THE QUESTION
{question}

TARGET ROLE
{job.get('company','')} — {job.get('title','')}
{job.get('jd_text','')[:1500]}

CANDIDATE BACKGROUND
summary: {resume.get('summary','')}
skills: {", ".join(resume.get('skills',[]))}
experiences: {[{'company':e['company'],'role':e['role'],'bullets':e['bullets']} for e in resume.get('experiences',[])]}

THEIR PAST ANSWERS (mirror this voice/length where useful)
{prior}

Write a concise, specific, genuine answer (90-150 words) tailored to THIS role and question.
Confident and human, not generic. Return just the answer."""


def build_prep_answers_prompt(job, resume, questions, saved_answers, as_json=True):
    """Batch-draft answers to a job's REAL application questions.

    One shared spec, two output formats (so API and Manual never drift):
      as_json=True  -> strict JSON {"answers": {"<key>": "<answer>"}}  (API mode)
      as_json=False -> labelled plain text the user pastes back        (Manual mode)

    Anti-hallucination: questions needing information not in the CV (e.g. exact
    salary or notice period) must come back EMPTY for the user to fill in.
    """
    prior = "\n".join(f"- {q}: {a}" for q, a in (saved_answers or {}).items()) or "(none)"
    qlines = []
    for q in questions:
        opts = f"  [options: {', '.join(q['options'])}]" if q.get("options") else ""
        qlines.append(f'- key "{q["key"]}" ({q["type"]}): {q["label"]}{opts}')
    qblock = "\n".join(qlines) or "(none)"

    if as_json:
        output_rules = (
            'Return ONLY valid JSON (no markdown fences, no commentary) with exactly:\n'
            '{"answers": {"<key>": "<answer string>", ...}}\n'
            "- Use the exact key strings shown for each question.\n"
            "- For a choice question, return one of its exact options."
        )
    else:
        output_rules = (
            "Return each answer under a markdown heading using the question's label, e.g.\n"
            "### <question label>\n<your answer>\n\n"
            "For a choice question, state one of its exact options."
        )

    return f"""You are helping a senior product manager fill out a REAL job application in their
own authentic voice. Use ONLY their real background below — never fabricate facts, metrics,
employers, skills, dates, salary, or notice period.

{output_rules}

Answering rules:
- Answer every listed question.
- Keep free-text answers concise (60-150 words), specific to THIS role, confident and human.
- If a question asks for information not present in their background (e.g. expected salary,
  exact notice period, a metric they never gave), leave it EMPTY ("" in JSON, or an empty body
  under the heading). Do not guess such values.

ROLE
{job.get('company','')} — {job.get('title','')}
{job.get('jd_text','')[:1500]}

QUESTIONS
{qblock}

THEIR BACKGROUND
summary: {resume.get('summary','')}
skills: {", ".join(resume.get('skills',[]))}
experiences: {[{'company':e['company'],'role':e['role'],'bullets':e['bullets']} for e in resume.get('experiences',[])]}

THEIR SAVED STANDARD ANSWERS (reuse where they fit; mirror this voice)
{prior}
"""


# ---------------- Manual-mode prompt file + no-LLM template ----------------
def save_prompt(job, score, outdir):
    """Write the manual Claude-Pro prompt to a timestamped file; return its path."""
    import os, datetime as _dt
    os.makedirs(outdir, exist_ok=True)
    safe = "".join(ch for ch in f"{job['company']}_{job['title']}"
                   if ch.isalnum() or ch in " _-")[:50].strip().replace(" ","_")
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(outdir, f"prompt_{safe}_{stamp}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_claude_prompt(job, score))
    return path

def tailor_template(job, model, top_per_role=4):
    """No-LLM tailoring: deterministically select the candidate's strongest real bullets per role,
    lightly reordered toward the JD's vocabulary. Never invents — pure selection from the master."""
    r = _resume()
    jd = (job.get("jd_text") or "").lower()
    def _relevance(bullet):
        words = set(w for w in re.findall(r"[a-z]{4,}", bullet.lower()))
        return sum(1 for w in words if w in jd)
    exps = []
    for e in r["experiences"]:
        ranked = sorted(e["bullets"], key=_relevance, reverse=True)
        exps.append({"company": e["company"], "role": e["role"], "dates": e["dates"],
                     "bullets": ranked[:top_per_role] or e["bullets"]})
    return {"name": r["name"], "title": r["title"], "contact": r["contact"],
            "summary": r["summary"], "skills": r["skills"], "experiences": exps,
            "education": r["education"], "certifications": r.get("certifications", []),
            "cover_letter": "", "fit_notes": "",
            "tailored_for": f"{job['company']} - {job['title']}"}
