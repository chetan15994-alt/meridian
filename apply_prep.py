"""apply_prep — Application Prep Pack (read-only).

For a given job this fetches the application's REAL question schema (where the ATS
exposes it publicly) and assembles a prep pack: the exact questions the form will
ask + AI-drafted answers, for the user to review, edit and submit MANUALLY.

Design principles (do not weaken):
- READ-ONLY. This module never submits an application and never logs in anywhere.
  Submission stays manual by design (ban-safety + quality control).
- Greenhouse exposes the full question schema publicly via
  GET /v1/boards/{token}/jobs/{id}?questions=true  -> we use the live schema.
- Lever's *public* postings API exposes only the standard application fields;
  custom per-posting questions live on the hosted form and are not reliably
  public, so Lever degrades to a clearly-labelled "standard fields" baseline.
- Any other source (ashby/workday/jsearch/manual/...) degrades to a GENERIC
  checklist of near-universal questions, clearly labelled as not fetched live.

Token resolution: for Greenhouse/Lever, ingest._norm stores the ATS token in the
job's "company" field and the apply URL in "url" — so no new data is needed.

All network access goes through an injectable `fetcher` so the whole module is
testable offline with a stub.
"""

import re

UA = {"User-Agent": "Mozilla/5.0 (Meridian personal job search)"}
TIMEOUT = 20

# Field-name / type classification ------------------------------------------------
_IDENTITY_NAMES = {"first_name", "last_name", "name", "full_name", "preferred_name",
                   "email", "phone"}
_DOCUMENT_NAMES = {"resume", "cv", "cover_letter"}
_EEO_HINTS = ("gender", "race", "veteran", "disability", "ethnic", "hispanic")
_SELECT_TYPES = ("select", "multi_value_single_select", "multi_value_multi_select",
                 "boolean", "yes_no")


def _classify(field_names, field_types):
    """Return one of: identity | document | eeo | choice | freetext.
    Most specific wins so a question is never under-protected."""
    names = [(n or "").lower() for n in field_names]
    types = [(t or "").lower() for t in field_types]
    if any(n in _DOCUMENT_NAMES for n in names) or any("file" in t for t in types):
        return "document"
    if any(hint in n for n in names for hint in _EEO_HINTS):
        return "eeo"
    if any(n in _IDENTITY_NAMES for n in names):
        return "identity"
    if any(t in _SELECT_TYPES for t in types):
        return "choice"
    return "freetext"


def _question(key, label, qtype, required=False, options=None, kind="freetext"):
    return {"key": str(key), "label": (label or "").strip() or str(key),
            "type": qtype, "required": bool(required),
            "options": list(options or []), "kind": kind}


# Network helper (default fetcher) ------------------------------------------------
def _http_json(url):
    """Default fetcher: GET url and return parsed JSON. Kept tiny + isolated so
    tests can inject a stub instead. Raises on any non-200/parse failure."""
    import requests
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# Greenhouse ----------------------------------------------------------------------
def greenhouse_job_id(url):
    """Extract the numeric Greenhouse job id from a stored apply URL."""
    if not url:
        return None
    for pat in (r"/jobs/(\d+)", r"[?&]gh_jid=(\d+)", r"[?&]token=(\d+)"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _collect_options(field):
    """Greenhouse select fields carry their options under 'values'/'options'.
    Return a list of human-readable option labels (best-effort, defensive)."""
    out = []
    for k in ("values", "options"):
        v = field.get(k)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    lbl = item.get("label") or item.get("name") or item.get("value")
                    if lbl is not None:
                        out.append(str(lbl))
                elif item is not None:
                    out.append(str(item))
    return out


def parse_greenhouse_questions(data):
    """Normalise a Greenhouse job payload (questions=true) into our schema."""
    out = []
    for q in (data.get("questions") or []):
        fields = q.get("fields") or []
        names = [f.get("name") for f in fields]
        types = [f.get("type") for f in fields]
        kind = _classify(names, types)
        opts = []
        for f in fields:
            opts.extend(_collect_options(f))
        key = next((n for n in names if n), q.get("label", "question"))
        qtype = (types[0] if types else "input_text") or "input_text"
        out.append(_question(key, q.get("label"), qtype,
                             required=q.get("required", False),
                             options=opts, kind=kind))
    # Demographic / EEO questions sometimes live under a separate key.
    for q in (data.get("demographic_questions") or data.get("compliance") or []):
        if isinstance(q, dict):
            out.append(_question(q.get("name", "demographic"), q.get("label", "Demographic question"),
                                 "demographic", required=False, kind="eeo"))
    return out


def _greenhouse_schema(token, job_id, fetcher):
    from urllib.parse import quote
    url = (f"https://boards-api.greenhouse.io/v1/boards/{quote(str(token), safe='')}"
           f"/jobs/{quote(str(job_id), safe='')}?questions=true")
    data = fetcher(url)
    return parse_greenhouse_questions(data)


# Lever ---------------------------------------------------------------------------
def _lever_apply_url(hosted_url):
    if not hosted_url:
        return ""
    base = hosted_url.split("?", 1)[0].rstrip("/")
    return base + "/apply"


def _lever_schema(hosted_url):
    """Lever's public API exposes only standard fields; custom questions are not
    reliably public. Return the documented standard baseline, clearly labelled."""
    qs = [
        _question("name", "Full name", "input_text", required=True, kind="identity"),
        _question("email", "Email", "input_text", required=True, kind="identity"),
        _question("phone", "Phone", "input_text", kind="identity"),
        _question("org", "Current company", "input_text", kind="freetext"),
        _question("urls_linkedin", "LinkedIn URL", "input_text", kind="identity"),
        _question("resume", "Resume / CV", "input_file", required=True, kind="document"),
        _question("comments", "Additional information", "textarea", kind="freetext"),
    ]
    return {"schema_source": "lever_standard", "questions": qs,
            "apply_url": _lever_apply_url(hosted_url),
            "note": "Lever's public API exposes only standard fields. This posting may add "
                    "custom questions on the hosted form that aren't publicly visible — open "
                    "the apply page to confirm."}


# Generic fallback ----------------------------------------------------------------
def _generic_questions():
    return [
        _question("why_role", "Why are you interested in this role and company?",
                  "textarea", required=True, kind="freetext"),
        _question("fit", "What makes you a strong fit for this role?",
                  "textarea", required=True, kind="freetext"),
        _question("yoe", "Years of relevant experience", "input_text", kind="freetext"),
        _question("notice", "Notice period / earliest start date", "input_text", kind="freetext"),
        _question("comp", "Expected compensation", "input_text", kind="freetext"),
        _question("work_auth", "Are you authorised to work in this location?",
                  "yes_no", options=["Yes", "No"], kind="choice"),
        _question("linkedin", "LinkedIn URL", "input_text", kind="identity"),
        _question("resume", "Resume / CV", "input_file", required=True, kind="document"),
    ]


def _generic(apply_url, note=""):
    return {"schema_source": "generic", "questions": _generic_questions(),
            "apply_url": apply_url or "",
            "note": note or "These are common application questions, not fetched from the live "
                            "form — open the apply page to see the exact fields."}


# Public API ----------------------------------------------------------------------
def fetch_schema(job, fetcher=None):
    """Return {schema_source, questions, apply_url, note}. Never raises — any
    failure degrades gracefully to a generic checklist with an explanatory note."""
    fetch = fetcher or _http_json
    source = (job.get("source") or "").lower()
    url = job.get("url") or ""
    token = (job.get("company") or "").strip()   # ingest stores ATS token here for GH/Lever

    if source == "greenhouse":
        job_id = greenhouse_job_id(url)
        if not (token and job_id):
            return _generic(url, "Couldn't resolve the Greenhouse job id from the URL; "
                                 "showing a generic checklist.")
        try:
            qs = _greenhouse_schema(token, job_id, fetch)
        except Exception as e:
            return _generic(url, f"Couldn't fetch the live Greenhouse form ({e}); "
                                 "showing a generic checklist.")
        if not qs:
            return _generic(url, "The Greenhouse form returned no questions; showing a "
                                 "generic checklist.")
        return {"schema_source": "greenhouse", "questions": qs, "apply_url": url, "note": ""}

    if source == "lever":
        return _lever_schema(url)

    return _generic(url)


def draftable_questions(questions):
    """The questions worth AI-drafting: free-text + multiple-choice. Identity,
    document upload and EEO/demographic questions are intentionally excluded."""
    return [q for q in (questions or []) if q.get("kind") in ("freetext", "choice")]


def draft_answers(job, questions, cfg, llm_fn=None):
    """API mode: batch-draft answers for the given (already draftable) questions.
    Returns (answers_dict, usage). Raises llm.LLMError on model failure.
    `llm_fn` is injectable for offline tests (defaults to llm.chat_json)."""
    import llm
    import tailor
    import settings
    draftable = [q for q in questions if q.get("kind") in ("freetext", "choice")]
    if not draftable:
        return {}, {"in": 0, "out": 0}
    resume = settings.load_resume()
    saved = (cfg.get("application", {}) or {}).get("answers", {}) or {}
    prompt = tailor.build_prep_answers_prompt(job, resume, draftable, saved, as_json=True)
    tc = cfg.get("tailoring", {})
    fn = llm_fn or llm.chat_json
    obj, usage = fn(prompt, tc.get("base_url"), tc.get("model"),
                    api=tc.get("api", "openai"), api_key=llm.get_api_key())
    answers = obj.get("answers", {}) if isinstance(obj, dict) else {}
    by_key = {q["key"]: q for q in draftable}
    clean = {}
    for k, v in (answers or {}).items():
        if k not in by_key or not isinstance(v, (str, int, float, bool)):
            continue
        if isinstance(v, bool):
            v = _bool_to_option(v, by_key[k].get("options") or [])
        clean[k] = str(v).strip()
    return clean, (usage or {"in": 0, "out": 0})


_YES_WORDS = ("yes", "true", "y")
_NO_WORDS = ("no", "false", "n")

def _bool_to_option(val, options):
    """Map True/False onto a question's real options ('Yes'/'No' etc.), so a
    boolean model reply never surfaces as the string 'True' in a form answer."""
    wanted = _YES_WORDS if val else _NO_WORDS
    for opt in options:
        if str(opt).strip().lower() in wanted:
            return opt
    return "Yes" if val else "No"


def manual_prompt(job, questions):
    """Manual mode: a plain-text prompt the user pastes into Claude Pro."""
    import tailor
    import settings
    resume = settings.load_resume()
    draftable = [q for q in questions if q.get("kind") in ("freetext", "choice")]
    return tailor.build_prep_answers_prompt(job, resume, draftable, {}, as_json=False)
