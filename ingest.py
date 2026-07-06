"""Job discovery from PUBLIC ATS job-board endpoints. No auth, no API tokens.
Wrong/closed company tokens just 404 and are skipped."""
import requests, hashlib, re, html, datetime
from urllib.parse import urlparse

UA = {"User-Agent": "Mozilla/5.0 (JobCopilot personal job search)"}
TIMEOUT = 20

def _clean(text):
    if not text: return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)         # strip HTML tags
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _hash(company, title, jd):
    return hashlib.sha1(f"{company}|{title}|{jd[:400]}".encode()).hexdigest()

def _norm(source, company, title, location, url, jd, posted_at):
    jd = _clean(jd)
    return {"content_hash": _hash(company, title, jd), "source": source,
            "company": company, "title": title or "", "location": location or "",
            "url": url or "", "jd_text": jd, "posted_at": posted_at or ""}

# ---------- Greenhouse ----------
def greenhouse(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    out = []
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200: return out
        for j in r.json().get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            out.append(_norm("greenhouse", token, j.get("title"), loc,
                             j.get("absolute_url"), j.get("content", ""),
                             j.get("updated_at", "")))
    except Exception as e:
        print(f"  [greenhouse:{token}] {e}")
    return out

# ---------- Lever ----------
def lever(token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    out = []
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200: return out
        for j in r.json():
            loc = (j.get("categories") or {}).get("location", "")
            ts = j.get("createdAt")
            posted = (datetime.datetime.fromtimestamp(ts/1000, tz=datetime.timezone.utc)
                      .replace(tzinfo=None).isoformat()) if ts else ""
            jd = j.get("descriptionPlain") or j.get("description") or ""
            out.append(_norm("lever", token, j.get("text"), loc,
                             j.get("hostedUrl"), jd, posted))
    except Exception as e:
        print(f"  [lever:{token}] {e}")
    return out

# ---------- Ashby ----------
def ashby(token):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    out = []
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200: return out
        for j in r.json().get("jobs", []):
            jd = j.get("descriptionPlain") or j.get("descriptionHtml") or ""
            out.append(_norm("ashby", token, j.get("title"), j.get("location"),
                             j.get("jobUrl") or j.get("applyUrl"), jd,
                             j.get("publishedDate", "")))
    except Exception as e:
        print(f"  [ashby:{token}] {e}")
    return out


# ---------- Recruitee ----------
def recruitee(token):
    url = f"https://{token}.recruitee.com/api/offers/"
    out = []
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200: return out
        for j in r.json().get("offers", []):
            jd = j.get("description") or ""
            loc = j.get("location") or j.get("city") or ""
            out.append(_norm("recruitee", token, j.get("title"), loc,
                             j.get("careers_url") or j.get("url"), jd,
                             j.get("published_at", "")))
    except Exception as e:
        print(f"  [recruitee:{token}] {e}")
    return out

# ---------- SmartRecruiters ----------
def smartrecruiters(token):
    base = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    out = []
    try:
        r = requests.get(base + "?limit=100", headers=UA, timeout=TIMEOUT)
        if r.status_code != 200: return out
        for p in r.json().get("content", [])[:40]:   # cap to limit per-posting calls
            pid, title = p.get("id"), p.get("name")
            loc = (p.get("location") or {}).get("city", "")
            url = f"https://jobs.smartrecruiters.com/{token}/{pid}"
            jd = ""
            try:
                d = requests.get(f"{base}/{pid}", headers=UA, timeout=TIMEOUT)
                if d.status_code == 200:
                    secs = ((d.json().get("jobAd") or {}).get("sections")) or {}
                    jd = " ".join((secs.get(k) or {}).get("text", "") for k in
                                  ["jobDescription", "qualifications", "additionalInformation"])
            except Exception:
                pass
            out.append(_norm("smartrecruiters", token, title, loc, url, jd,
                             p.get("releasedDate", "")))
    except Exception as e:
        print(f"  [smartrecruiters:{token}] {e}")
    return out


# ---------- Workday (URL-based: token = a company's Workday careers URL) ----------
def workday(token):
    """token example: https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"""
    out = []
    try:
        u = urlparse(token if "://" in token else "https://" + token)
        host = u.netloc
        segs = [x for x in u.path.split("/") if x]
        if not host or not segs: return out
        tenant, site = host.split(".")[0], segs[0]
        base = f"https://{host}/wday/cxs/{tenant}/{site}"
        hdr = {**UA, "Content-Type": "application/json"}
        offset = 0
        for _ in range(3):
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}
            r = requests.post(base + "/jobs", json=body, headers=hdr, timeout=TIMEOUT)
            if r.status_code != 200: break
            posts = r.json().get("jobPostings", [])
            if not posts: break
            for p in posts:
                path = p.get("externalPath", "")
                jd = ""
                try:
                    d = requests.get(base + path, headers=UA, timeout=TIMEOUT)
                    if d.status_code == 200:
                        jd = ((d.json().get("jobPostingInfo") or {}).get("jobDescription")) or ""
                except Exception:
                    pass
                out.append(_norm("workday", tenant, p.get("title"),
                                 p.get("locationsText", ""), f"https://{host}{path}",
                                 jd, p.get("postedOn", "")))
            offset += 20
    except Exception as e:
        print(f"  [workday] {e}")
    return out

# ---------- Workable (public widget JSON, no auth) ----------
def workable(token):
    """GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"""
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    out = []
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200: return out
        for j in r.json().get("jobs", []):
            loc_parts = [j.get("city"), j.get("region"), j.get("country")]
            loc = ", ".join([p for p in loc_parts if p]) or j.get("location", "")
            jd = j.get("description", "") or ""
            if j.get("requirements"): jd += " " + j["requirements"]
            out.append(_norm("workable", token, j.get("title"), loc,
                             j.get("url") or j.get("application_url", ""),
                             jd, j.get("published_on") or j.get("created_at", "")))
    except Exception as e:
        print(f"  [workable:{token}] {e}")
    return out

# ---------- Personio (public XML feed, no auth) ----------
def personio(token):
    """GET https://{token}.jobs.personio.de/xml (falls back to .com)."""
    import xml.etree.ElementTree as ET
    out = []
    for host in (f"https://{token}.jobs.personio.de/xml?language=en",
                 f"https://{token}.jobs.personio.com/xml?language=en"):
        try:
            r = requests.get(host, headers=UA, timeout=TIMEOUT)
            if r.status_code != 200 or "<" not in r.text: continue
            root = ET.fromstring(r.text.encode("utf-8"))
            for pos in root.iter("position"):
                def _g(tag):
                    el = pos.find(tag); return (el.text or "").strip() if el is not None else ""
                title = _g("name"); office = _g("office"); jid = _g("id")
                descs = []
                for jd in pos.iter("jobDescription"):
                    v = jd.find("value")
                    if v is not None and v.text: descs.append(v.text)
                jd_text = " ".join(descs) or _g("recruitingCategory")
                url = f"https://{token}.jobs.personio.de/job/{jid}" if jid else host
                out.append(_norm("personio", token, title, office, url, jd_text, _g("createdAt")))
            if out: break
        except Exception as e:
            print(f"  [personio:{token}] {e}")
    return out

# ---------- Freshteam (public careers HTML, no auth — best-effort parse) ----------
def freshteam(token, max_detail=40):
    """Parse https://{token}.freshteam.com/jobs (no-auth HTML). Job links carry
    data-portal-* attributes; JD text is pulled from each role's JSON-LD (capped)."""
    import lxml.html, json as _json
    out = []
    try:
        r = requests.get(f"https://{token}.freshteam.com/jobs", headers=UA, timeout=TIMEOUT)
        if r.status_code != 200: return out
        doc = lxml.html.fromstring(r.text)
        seen = set()
        for a in doc.xpath('//a[@data-portal-title]'):
            title = a.get("data-portal-title", "").strip()
            href = a.get("href", "")
            if not title or href in seen: continue
            seen.add(href)
            loc = a.get("data-portal-location", "") or a.get("data-location", "")
            url = href if href.startswith("http") else f"https://{token}.freshteam.com{href}"
            out.append(_norm("freshteam", token, title, loc, url, title, ""))
        for job in out[:max_detail]:
            try:
                d = requests.get(job["url"], headers=UA, timeout=TIMEOUT)
                if d.status_code != 200: continue
                dd = lxml.html.fromstring(d.text)
                for s in dd.xpath('//script[@type="application/ld+json"]/text()'):
                    obj = _json.loads(s)
                    if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                        desc = re.sub("<[^>]+>", " ", obj.get("description", "") or "")
                        if desc:
                            job["jd_text"] = _clean(desc)
                            job["content_hash"] = _hash(job["company"], job["title"], job["jd_text"])
                        loc = obj.get("jobLocation", {})
                        if isinstance(loc, dict):
                            addr = loc.get("address", {})
                            if isinstance(addr, dict):
                                city = addr.get("addressLocality") or addr.get("addressRegion")
                                if city and not job["location"]: job["location"] = city
                        break
            except Exception:
                pass
    except Exception as e:
        print(f"  [freshteam:{token}] {e}")
    return out

FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
            "recruitee": recruitee, "smartrecruiters": smartrecruiters, "workday": workday,
            "workable": workable, "personio": personio, "freshteam": freshteam}
# Only ATS providers with PUBLIC posting endpoints can be supported here.
PORTALS = {
  "greenhouse": {
     "name": "Greenhouse",
     "desc": "Widely-used ATS for startups & tech companies. JD included, very reliable.",
     "help": "Token = the slug in the careers URL  boards.greenhouse.io/<TOKEN>",
     "example": "e.g. stripe, anthropic"},
  "lever": {
     "name": "Lever",
     "desc": "Common ATS for mid-size tech firms. Public JSON postings.",
     "help": "Token = the slug in  jobs.lever.co/<TOKEN>",
     "example": "e.g. netflix, plaid"},
  "ashby": {
     "name": "Ashby",
     "desc": "Modern ATS popular with AI/early-stage startups.",
     "help": "Token = the slug in  jobs.ashbyhq.com/<TOKEN>",
     "example": "e.g. ramp, openai"},
  "recruitee": {
     "name": "Recruitee",
     "desc": "ATS used by many EU & global companies. JD included in one call.",
     "help": "Token = the subdomain in  <TOKEN>.recruitee.com",
     "example": "e.g. companyname"},
  "smartrecruiters": {
     "name": "SmartRecruiters",
     "desc": "Enterprise ATS. Fetches each posting's detail for the full JD (a bit slower).",
     "help": "Token = the company id in  jobs.smartrecruiters.com/<TOKEN>",
     "example": "e.g. CompanyInc"},
  "workday": {
     "name": "Workday",
     "desc": "Used by many large enterprises (Salesforce, SAP, etc.). Slower; URL-based.",
     "help": "Token = the FULL Workday careers URL, e.g. https://company.wd1.myworkdayjobs.com/SiteName",
     "example": "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"},
  "workable": {
     "name": "Workable",
     "desc": "Public no-auth JSON. Common among SMBs, agencies & operational teams.",
     "help": "Token = the account slug in  apply.workable.com/<TOKEN>",
     "example": "e.g. companyname"},
  "personio": {
     "name": "Personio",
     "desc": "Public XML feed. Mostly European (DACH) employers — lower India coverage.",
     "help": "Token = the subdomain in  <TOKEN>.jobs.personio.de",
     "example": "e.g. companyname"},
  "freshteam": {
     "name": "Freshteam (best-effort)",
     "desc": "No-auth careers page parsed from HTML (Freshworks ATS). Slower; may break if their page changes.",
     "help": "Token = the subdomain in  <TOKEN>.freshteam.com",
     "example": "e.g. companyname"},
}

def _fresh(job, days):
    if not job["posted_at"]: return True   # keep if unknown
    try:
        d = job["posted_at"].replace("Z", "")
        dt = datetime.datetime.fromisoformat(d[:19])
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        return (now - dt).days <= days
    except Exception:
        return True

import functools

# ---- Geography knowledge base: a country expands to its cities/states/aliases ----
# Lets "India" semantically include Bengaluru, Mumbai, etc. Extensible.
GEO_EXPAND = {
  "india": ["india","bengaluru","bangalore","mumbai","delhi","new delhi","gurgaon","gurugram",
            "noida","hyderabad","pune","chennai","kolkata","ahmedabad","jaipur","kochi","cochin",
            "indore","chandigarh","coimbatore","trivandrum","thiruvananthapuram","mysuru","mysore",
            "nagpur","vadodara","surat","visakhapatnam","bhubaneswar","gandhinagar","ncr",
            "karnataka","maharashtra","telangana","tamil nadu","haryana","uttar pradesh","gujarat","kerala"],
  "united states": ["united states","usa","u.s.","u.s.a","new york","san francisco","seattle","austin",
            "boston","chicago","los angeles","mountain view","palo alto","sunnyvale","denver","atlanta"],
  "united kingdom": ["united kingdom","uk","london","manchester","edinburgh","cambridge"],
  "singapore": ["singapore"], "germany": ["germany","berlin","munich","hamburg"],
  "canada": ["canada","toronto","vancouver","montreal"], "australia": ["australia","sydney","melbourne"],
  "uae": ["uae","dubai","abu dhabi"], "ireland": ["ireland","dublin"],
}
_COUNTRY_ALIASES = {"usa":"united states","us":"united states","u.s.":"united states",
                    "uk":"united kingdom","u.k.":"united kingdom","bharat":"india"}
REMOTE_KW = ["remote","anywhere","distributed","work from home","wfh","virtual"]

@functools.lru_cache(maxsize=64)
def expand_locations(locations):
    """locations: a tuple of user-entered strings. Returns a frozenset of lowercased match tokens,
    expanding any country to its cities/states (India -> Bengaluru, Mumbai, ...)."""
    out = set()
    for raw in locations:
        l = (raw or "").strip().lower()
        if not l: continue
        canon = _COUNTRY_ALIASES.get(l, l)
        if canon in GEO_EXPAND:
            out.update(GEO_EXPAND[canon])
        else:
            out.add(l)                      # a city/region the user typed directly
    return frozenset(out)

# ---- Role aliases: makes title matching semantic instead of exact-substring ----
ROLE_ALIASES = {
  "product manager": ["product manager","product management","product owner","product lead",
      "group product manager","associate product manager","senior product manager",
      "principal product manager","staff product manager","lead product manager",
      "director of product","head of product","vp of product","vp product","product director",
      "gpm","apm","pm ii","pm i","technical product manager","ai product manager","platform product manager"],
  "program manager": ["program manager","programme manager","technical program manager","tpm"],
  "project manager": ["project manager","delivery manager"],
}

def _wb(text, token):
    """Word-boundary containment — avoids 'us' matching 'Houston' or 'pm' matching 'shipment'."""
    return re.search(r"\b" + re.escape(token) + r"\b", text) is not None

def _title_match(job, keywords, strict=False):
    """strict=True: title must contain a keyword as an exact substring (old behavior).
    strict=False (default): also matches if all words of a keyword appear (any order), or if
    the keyword maps to a known role family and any of its aliases appear. This lets
    'Group Product Manager', 'Product Lead', 'GPM' pass a 'product manager' keyword."""
    if not keywords: return True
    t = (job["title"] or "").lower()
    for k in keywords:
        kl = (k or "").lower().strip()
        if not kl: continue
        if kl in t: return True
        if strict: continue
        words = kl.split()
        if words and all(_wb(t, w) for w in words): return True   # all words present, any order
        for canon, aliases in ROLE_ALIASES.items():
            if kl == canon or kl in aliases:
                if any(_wb(t, a) for a in aliases): return True
    return False

def _loc_match(job, locations, remote_ok):
    """Geography-aware. 'India' matches Bengaluru/Mumbai/etc. via GEO_EXPAND. No locations set =>
    no location filter. 'Include Remote' adds purely-remote roles; a remote role pinned to a
    different geography (e.g. 'Remote - US' when you want India) is still rejected."""
    if not locations:
        return True                          # no location filter -> everything passes
    expanded = expand_locations(tuple(locations))
    loc = (job.get("location") or "").lower().strip()
    if not loc:
        return False                         # filter active -> drop unknown-location roles
    if any(_wb(loc, tok) for tok in expanded):
        return True
    if remote_ok and any(k in loc for k in REMOTE_KW):
        stripped = loc
        for tok in REMOTE_KW + ["-", ",", "/", "(", ")", "|", "global", "worldwide", "remote-first"]:
            stripped = stripped.replace(tok, " ")
        if not stripped.strip():
            return True                      # purely remote, no conflicting geography
        return any(_wb(loc, tok) for tok in expanded)   # geo-qualified remote
    return False

def _slugs(name):
    b = name.strip().lower()
    cand = [b.replace(" ",""), b.replace(" ","-"), b.replace(" ","_"), name.strip().replace(" ","")]
    return list(dict.fromkeys([c for c in cand if c]))

def _camel_ids(name):
    """Candidate SmartRecruiters company ids (often CamelCase, no spaces)."""
    words = [w for w in re.split(r"[\s_-]+", name.strip()) if w]
    cap = "".join(w[:1].upper()+w[1:] for w in words)
    return list(dict.fromkeys([cap, "".join(words), name.strip().replace(" ","")]))

def probe_company(name, quick=False):
    """Guess which ATS a company uses by probing public endpoints. Returns [(portal, token)].
    Covers Greenhouse/Lever/Ashby/Recruitee/Workable/Personio/Freshteam (slug), SmartRecruiters
    (CamelCase id), and a bounded Workday probe. quick=True skips the heavy Workday brute-force
    (used for batch verification where speed matters)."""
    hits = []
    for slug in _slugs(name):
        checks = [
            ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "jobs", "get"),
            ("lever",      f"https://api.lever.co/v0/postings/{slug}?mode=json", None, "get"),
            ("ashby",      f"https://api.ashbyhq.com/posting-api/job-board/{slug}", "jobs", "get"),
            ("recruitee",  f"https://{slug}.recruitee.com/api/offers/", "offers", "get"),
            ("workable",   f"https://apply.workable.com/api/v1/widget/accounts/{slug}", "jobs", "get"),
            ("personio",   f"https://{slug}.jobs.personio.de/xml?language=en", "_xml", "get"),
        ]
        for portal, url, key, _m in checks:
            try:
                r = requests.get(url, headers=UA, timeout=10)
                if r.status_code != 200: continue
                if key == "_xml":
                    ok = ("<position>" in r.text or "<position " in r.text)
                else:
                    data = r.json()
                    ok = bool(data) if key is None else bool(data.get(key))
                if ok and (portal, slug) not in hits: hits.append((portal, slug))
            except Exception:
                pass
        # Freshteam — no-auth HTML, check for job markers
        try:
            r = requests.get(f"https://{slug}.freshteam.com/jobs", headers=UA, timeout=10)
            if r.status_code == 200 and "data-portal-title" in r.text:
                if ("freshteam", slug) not in hits: hits.append(("freshteam", slug))
        except Exception:
            pass

    # SmartRecruiters — try CamelCase company-id candidates
    for cid in _camel_ids(name):
        try:
            r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=1",
                             headers=UA, timeout=10)
            if r.status_code == 200 and (r.json().get("content") is not None):
                if ("smartrecruiters", cid) not in hits: hits.append(("smartrecruiters", cid))
                break
        except Exception:
            pass

    if quick:
        return hits

    # Workday — bounded probe across common datacenters x site names (tenant = slug)
    dcs = ["wd1","wd3","wd5","wd103","wd12","wd2","wd101"]
    sites = ["External","Careers","External_Career_Site","careers","en-US/External"]
    found_wd = False
    for slug in _slugs(name)[:2]:
        if found_wd: break
        for dc in dcs:
            if found_wd: break
            for site in sites:
                host = f"https://{slug}.{dc}.myworkdayjobs.com"
                cxs = f"{host}/wday/cxs/{slug}/{site}/jobs"
                try:
                    r = requests.post(cxs, json={"limit":1,"offset":0,"searchText":"","appliedFacets":{}},
                                      headers={**UA,"Content-Type":"application/json"}, timeout=8)
                    if r.status_code == 200 and r.json().get("jobPostings") is not None:
                        full = f"{host}/{site}"
                        hits.append(("workday", full)); found_wd = True; break
                except Exception:
                    pass
    return hits

def probe_many(names, quick=True, on_progress=None):
    """Batch-resolve a list of company names to ATS portals+tokens (live, on the user's
    machine). Returns {name: [(portal, token), ...]}. quick=True keeps it fast for many names."""
    result = {}
    total = len(names) or 1
    for i, nm in enumerate(names):
        nm = nm.strip()
        if not nm: continue
        if on_progress: on_progress("probe", i, total, nm)
        try:
            result[nm] = probe_company(nm, quick=quick)
        except Exception:
            result[nm] = []
    if on_progress: on_progress("probe", total, total, "done")
    return result

_LOWER_SLUG = {"greenhouse","lever","ashby","recruitee","workable","personio","freshteam"}

def _normalize_tokens(sources):
    """Clean + dedupe tokens. Lowercases slug-based portals (so 'Postman'=='postman'), preserves
    case for SmartRecruiters (CamelCase ids) and Workday (full URL), and flags space-broken tokens
    as invalid instead of firing malformed requests. Returns [(src, token, valid, note)]."""
    out, seen = [], set()
    for src, toks in (sources or {}).items():
        for raw in (toks or []):
            t = str(raw or "").strip()
            if not t: continue
            if src in _LOWER_SLUG:
                norm = t.lower()
                if " " in norm:
                    key = (src, norm)
                    if key not in seen: seen.add(key); out.append((src, t, False, "invalid token (has spaces) — use the bulk finder to get the real slug"))
                    continue
                key = (src, norm)
                if key in seen: continue
                seen.add(key); out.append((src, norm, True, ""))
            else:  # smartrecruiters / workday — preserve case
                key = (src, t.lower())
                if key in seen: continue
                seen.add(key); out.append((src, t, True, ""))
    return out

MAX_FETCH_WORKERS = 12   # network-bound public GETs; polite but ~10x a serial sweep

def _parallel_fetch(tokens, on_progress, total):
    """Fetch all valid (src, tok) pairs concurrently. Returns {idx: raw_jobs}.
    Progress is reported from THIS (caller's) thread as futures complete, so a
    Streamlit progress callback is never touched from a worker thread. Fetchers
    already swallow their own errors and return []; the belt-and-braces except
    here means one crashing portal can never take down the whole run."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    fetchable = [(i, s, t) for i, (s, t, valid, _) in enumerate(tokens)
                 if valid and FETCHERS.get(s)]
    results = {}
    if not fetchable:
        return results
    workers = min(MAX_FETCH_WORKERS, len(fetchable))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(FETCHERS[s], t): (i, s, t) for i, s, t in fetchable}
        done = 0
        for fut in as_completed(futs):
            i, s, t = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                print(f"  [{s}:{t}] fetch failed: {e}")
                results[i] = []
            done += 1
            if on_progress: on_progress("discover", done, total, f"{s}:{t}")
    return results

def discover(sources, role_keywords, freshness_days, locations=None, remote_ok=True,
             strict_title=False, on_progress=None):
    """Returns (jobs, report). Each report row carries a per-stage funnel (how many dropped at
    title / location / freshness) plus sample locations, so a '0 kept' is never a mystery.
    Fetching is parallel (v1.15.0); filtering/dedup runs in the original token
    order so output is deterministic and byte-identical to the old serial path."""
    seen, seen_role, jobs, report = set(), set(), [], []
    tokens = _normalize_tokens(sources)
    total = len(tokens) or 1
    fetched = _parallel_fetch(tokens, on_progress, total)
    for idx, (src, tok, valid, tnote) in enumerate(tokens):
        if not valid:
            report.append({"source":src,"token":tok,"fetched":0,"kept":0,"drop_title":0,
                           "drop_loc":0,"drop_stale":0,"sample_locs":"","note":tnote})
            continue
        if not FETCHERS.get(src):
            report.append({"source":src,"token":tok,"fetched":0,"kept":0,"drop_title":0,
                           "drop_loc":0,"drop_stale":0,"sample_locs":"","note":"unknown portal"})
            continue
        raw = fetched.get(idx, [])
        d = {"dup":0,"no_jd":0,"title":0,"loc":0,"stale":0,"kept":0}
        locs_seen = {}
        for j in raw:
            ld = j.get("location") or "—"
            locs_seen[ld] = locs_seen.get(ld, 0) + 1
            role_key = (j["company"].lower(), j["title"].strip().lower())
            if j["content_hash"] in seen or role_key in seen_role: d["dup"]+=1; continue
            if not j["jd_text"]: d["no_jd"]+=1; continue
            if not _title_match(j, role_keywords, strict_title): d["title"]+=1; continue
            if not _loc_match(j, locations or [], remote_ok): d["loc"]+=1; continue
            if not _fresh(j, freshness_days): d["stale"]+=1; continue
            seen.add(j["content_hash"]); seen_role.add(role_key); jobs.append(j); d["kept"]+=1
        top = sorted(locs_seen.items(), key=lambda x:-x[1])[:4]
        note = tnote or ("" if raw else "0 fetched — wrong token or no public postings")
        report.append({"source":src,"token":tok,"fetched":len(raw),"kept":d["kept"],
                       "drop_title":d["title"],"drop_loc":d["loc"],"drop_stale":d["stale"],
                       "sample_locs":"; ".join(f"{l} ({n})" for l,n in top),"note":note})
        print(f"  {src}:{tok} -> fetched {len(raw)}, kept {d['kept']} "
              f"(title-drop {d['title']}, loc-drop {d['loc']}, stale {d['stale']})")
    if on_progress: on_progress("discover", total, total, "done")
    return jobs, report
