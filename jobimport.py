"""jobimport — turn a PASTED job posting (LinkedIn or generic) into the structured
fields Meridian needs: company, title, location, url, jd_text.

Why this exists (v1.16.0):
The user asked for a LinkedIn -> Meridian sync. That was investigated and rejected
on the facts: there is no sanctioned, stable path (no official LinkedIn MCP, no
Anthropic-LinkedIn partnership; LinkedIn User Agreement 8.2 prohibits automated
access to login-walled data; 2026 enforcement flags scheduled repetitive queries
fast). A scheduled scraper against the one account the search depends on is exactly
the wrong risk. See PROJECT_STATE 6a.

The safe, high-value alternative: the user is already allowed to VIEW jobs while
logged in. This module removes the friction of retyping — copy the whole posting,
paste once, and Meridian parses it into fields to confirm before scoring. Nothing
here touches LinkedIn; it only parses text the user pasted.

Design:
- `parse_job_text(raw)` is DETERMINISTIC, offline, and pure — no network, no LLM.
  It reads LinkedIn's stable on-page label structure ("Company · Location",
  "About the job", apply/URL lines) plus generic heuristics.
- It returns a ParseResult with per-field confidence and a `needs_review` flag so
  the UI can decide whether to offer the (optional) LLM cleanup. The LLM path
  lives in the app layer (cost-capped, dual-mode) and always falls back to this
  deterministic result on any failure.
- The required fields for a usable Meridian job are: title, company, jd_text.
"""

import re
from urllib.parse import urlparse

# LinkedIn (and common board) section headers that introduce the JD body.
_JD_HEADERS = (
    "about the job", "about the role", "about this role", "job description",
    "responsibilities", "what you'll do", "what you will do", "the role",
    "role overview", "who we are looking for", "your role", "overview",
)
# Lines that are LinkedIn UI chrome, not job content — dropped from the JD body.
_CHROME = (
    "easy apply", "save", "apply now", "apply", "show more", "show less",
    "see more", "see less", "promoted", "actively recruiting", "be an early applicant",
    "people you can reach out to", "meet the hiring team", "sign in", "join now",
    "notify me", "how you match", "skills associated with the job",
    "set alert for similar jobs", "set alert",
)
# Chrome that appears as a PREFIX of a longer nav line — matched with startswith.
_CHROME_PREFIX = (
    "skip to", "skip main", "skip search", "back to search results",
    "save lead", "save senior", "save product",  # "Save <title> at <co>" button label
)


def _is_chrome(line):
    l = line.lower().strip()
    if l in _CHROME:
        return True
    if any(l.startswith(p) for p in _CHROME_PREFIX):
        return True
    # generic "save <anything> at <company>" LinkedIn save-button label
    if l.startswith("save ") and " at " in l:
        return True
    return False
_EMPLOYMENT = ("full-time", "part-time", "contract", "temporary", "internship",
               "volunteer", "on-site", "on\u2011site", "remote", "hybrid")


def _lines(raw):
    return [ln.strip() for ln in (raw or "").replace("\r", "\n").split("\n")]


def _nonblank(raw):
    return [ln for ln in _lines(raw) if ln.strip()]


def _find_url(raw):
    m = re.search(r"https?://[^\s)>\]]+", raw or "")
    if not m:
        return "", 0.0
    url = m.group(0).rstrip(".,;")
    try:
        if urlparse(url).netloc:
            return url, 0.9
    except ValueError:
        pass
    return "", 0.0


def _looks_like_location(s):
    sl = s.lower()
    if any(w in sl for w in ("remote", "hybrid", "on-site", "on\u2011site")):
        return True
    # "City, Region" / "City, Country": short, comma-separated, few words, and
    # NOT sentence-like. Titles such as "Senior Product Manager, AI Platform"
    # also contain a comma, so require the comma-separated parts to look like
    # place tokens (Title-case or known region words), not role phrases.
    if "," in s and len(s) <= 60 and not s.endswith(":"):
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if 2 <= len(parts) <= 4 and all(len(p.split()) <= 3 for p in parts):
            # reject if any part contains role-ish words
            roleish = ("manager", "engineer", "director", "lead", "product",
                       "designer", "analyst", "developer", "officer", "head",
                       "specialist", "consultant", "architect", "scientist")
            if not any(w in sl for w in roleish):
                return True
    return False


def _clean_company(s):
    # LinkedIn often renders "Company · Location" or "Company logo\nCompany".
    s = s.split("\u00b7")[0].strip()
    s = re.sub(r"\s+logo$", "", s, flags=re.I).strip()
    return s


def _split_meta_line(line):
    """A LinkedIn subheader like 'Acme Corp · Bengaluru, India · 2 days ago · 30 applicants'.
    Returns (company, location) best-effort."""
    parts = [p.strip() for p in line.split("\u00b7") if p.strip()]
    company = loc = ""
    if parts:
        company = _clean_company(parts[0])
    for p in parts[1:]:
        if _looks_like_location(p):
            loc = p
            break
    return company, loc


class ParseResult(dict):
    """dict with attribute access + a tidy view for the UI."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    @property
    def fields(self):
        return {k: self.get(k, "") for k in ("company", "title", "location", "url", "jd_text")}

    @property
    def missing_required(self):
        return [k for k in ("title", "company", "jd_text") if not self.get(k, "").strip()]

    @property
    def needs_review(self):
        # Offer LLM cleanup when a required field is missing OR overall confidence is low.
        return bool(self.missing_required) or self.get("confidence", 0.0) < 0.55


def parse_job_text(raw):
    """Deterministic, offline parse of a pasted posting. Never raises."""
    try:
        return _parse(raw)
    except Exception as e:  # defensive: a parser bug must never break the import UI
        r = ParseResult(company="", title="", location="", url="", jd_text=(raw or "").strip(),
                        confidence=0.0, source_guess="unknown", note=f"parser error: {e}")
        return r


def _parse(raw):
    raw = (raw or "").strip()
    if not raw:
        return ParseResult(company="", title="", location="", url="", jd_text="",
                           confidence=0.0, source_guess="empty", note="nothing pasted")

    url, url_conf = _find_url(raw)
    source_guess = "linkedin" if "linkedin.com" in raw.lower() else "generic"

    lines = _nonblank(raw)
    low = [ln.lower() for ln in lines]

    # --- locate the JD body: first known header line onward ---
    jd_start = None
    for i, ln in enumerate(low):
        if any(ln == h or ln.startswith(h) for h in _JD_HEADERS):
            jd_start = i
            break

    title = company = location = ""
    title_conf = company_conf = loc_conf = 0.0

    # --- header region = everything before the JD body (or first ~8 lines) ---
    header_end = jd_start if jd_start is not None else min(len(lines), 8)
    header = lines[:header_end]

    # Title: first substantial non-chrome header line.
    for ln in header:
        l = ln.lower()
        if _is_chrome(ln):
            continue
        if _looks_like_location(ln) or "\u00b7" in ln:
            continue
        if 2 <= len(ln) <= 120 and not ln.endswith(":"):
            title = ln
            title_conf = 0.75
            break

    # Company + location: from the first "A · B · C" meta line, else scan.
    for ln in header:
        if "\u00b7" in ln:
            c, lo = _split_meta_line(ln)
            if c and not company:
                company, company_conf = c, 0.7
            if lo and not location:
                location, loc_conf = lo, 0.7
            if company:
                break
    if not location:
        for ln in header:
            if ln != title and _looks_like_location(ln):
                location, loc_conf = ln, 0.6
                break
    if not company:
        # Fallback: an "at Company" pattern in the title, or the line after title.
        m = re.search(r"\bat\s+([A-Z][\w&.,'\- ]{1,60})", title)
        if m:
            company, company_conf = m.group(1).strip(" .,"), 0.5
        else:
            idx = header.index(title) if title in header else -1
            if 0 <= idx + 1 < len(header):
                cand = header[idx + 1]
                if not _looks_like_location(cand) and not _is_chrome(cand):
                    company, company_conf = _clean_company(cand), 0.4

    # --- JD body ---
    if jd_start is not None:
        body_lines = lines[jd_start + 1:]
    else:
        # No header found: treat everything after the header region as the JD.
        body_lines = lines[header_end:]
    body_lines = [ln for ln in body_lines if not _is_chrome(ln)]
    jd_text = "\n".join(body_lines).strip()
    if len(jd_text) < 40:  # too little — fall back to the whole paste minus obvious chrome
        jd_text = "\n".join(ln for ln in lines if not _is_chrome(ln)).strip()

    # Employment-type sniff appended into location if location empty but type present.
    if not location:
        for ln in header:
            if ln.lower() in _EMPLOYMENT:
                location, loc_conf = ln, 0.4
                break

    fields_present = sum(bool(x) for x in (title, company, jd_text))
    confidence = round(
        0.4 * title_conf + 0.3 * company_conf + 0.15 * loc_conf +
        0.15 * (1.0 if len(jd_text) >= 120 else 0.3), 3)

    return ParseResult(
        company=company.strip(), title=title.strip(), location=location.strip(),
        url=url.strip(), jd_text=jd_text.strip(),
        confidence=confidence, source_guess=source_guess,
        note="" if fields_present == 3 else "some fields need your review")


# ---- optional LLM assist (called by the app layer only when needs_review) ----
def build_extract_prompt(raw):
    """Prompt for the LLM fallback. Strict JSON, extraction-only (no invention)."""
    return (
        "Extract job-posting fields from the text below into EXACT JSON. "
        "Extract ONLY what is present — never invent a company, title, or location. "
        "Use an empty string for anything not stated.\n"
        'Return ONLY: {"company":"","title":"","location":"","url":"","jd_text":""}\n'
        "jd_text = the full role description/responsibilities/requirements, cleaned of "
        "UI chrome (buttons like 'Easy Apply', applicant counts, 'Promoted').\n\n"
        "TEXT:\n" + (raw or "")[:6000]
    )


def merge_llm(deterministic, llm_obj):
    """Overlay LLM-extracted fields onto the deterministic result. LLM fills only
    fields it actually returned; deterministic values win when the LLM is blank.
    Always returns a ParseResult (never raises)."""
    out = ParseResult(deterministic)
    if isinstance(llm_obj, dict):
        for k in ("company", "title", "location", "url", "jd_text"):
            v = llm_obj.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
    out["confidence"] = max(out.get("confidence", 0.0), 0.85) if not out.missing_required else out.get("confidence", 0.0)
    out["note"] = "" if not out.missing_required else "still missing: " + ", ".join(out.missing_required)
    return out
