"""External job-data connectors — the 'web-wide' layer beyond direct ATS fetch.

Currently implements JSearch (OpenWeb Ninja / RapidAPI), which surfaces live listings from
Google for Jobs and the open web (LinkedIn, Indeed, Naukri, company pages) — legally, via a
sanctioned API, with full job descriptions good enough to tailor against.

Design: query-based (role + location), not company-token-based. Results are normalised into
the SAME job schema as ATS sources and run through the SAME title/location/freshness filters,
so they merge cleanly into the existing pipeline and scorer."""
import os, time, urllib.parse, requests
import ingest

_HERE   = os.path.dirname(__file__)
SECRETS = os.path.join(_HERE, "secrets.yaml")
UA      = {"User-Agent": "Mozilla/5.0 (Meridian job copilot)"}

# ─────────────────────────────────────────────────────────────────────────────
# Merge-safe secrets (so the JSearch key and the Anthropic key coexist)
# ─────────────────────────────────────────────────────────────────────────────
def _read_secrets():
    if os.path.exists(SECRETS):
        try:
            import fileio
            return fileio.read_yaml(SECRETS, default={}) or {}
        except Exception: return {}
    return {}

def _write_secret(field, value):
    s = _read_secrets(); s[field] = str(value or "").strip()
    import fileio
    fileio.write_yaml_atomic(SECRETS, s)

def get_jsearch_key():  return (_read_secrets().get("jsearch_api_key") or "").strip()
def save_jsearch_key(k): _write_secret("jsearch_api_key", k)

# ─────────────────────────────────────────────────────────────────────────────
# JSearch HTTP
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_BASE = "https://api.openwebninja.com/jsearch/search"

def _headers(provider, api_key):
    if provider == "rapidapi":
        # host derived from the base URL the user configured
        return {"x-rapidapi-key": api_key, "x-rapidapi-host": "jsearch.p.rapidapi.com"}
    return {"x-api-key": api_key, **UA}            # openwebninja (direct)

def jsearch_query(api_key, query, country="in", date_posted="month", num_pages=1,
                  provider="openwebninja", base_url=DEFAULT_BASE, timeout=30):
    """Single JSearch call. Returns the raw decoded JSON dict (raises on HTTP error)."""
    params = {"query": query, "country": country, "date_posted": date_posted,
              "page": 1, "num_pages": max(1, int(num_pages))}
    r = requests.get(base_url, headers=_headers(provider, api_key), params=params, timeout=timeout)
    if r.status_code in (401, 403):
        raise PermissionError(f"{r.status_code} — JSearch rejected the key (check it / your plan).")
    if r.status_code == 429:
        raise RuntimeError("429 — JSearch rate limit / quota reached for your plan.")
    r.raise_for_status()
    return r.json()

# ─────────────────────────────────────────────────────────────────────────────
# Normalisation → Meridian job schema
# ─────────────────────────────────────────────────────────────────────────────
def _normalize(item):
    title   = (item.get("job_title") or "").strip()
    company = (item.get("employer_name") or "Unknown").strip()
    if item.get("job_is_remote"):
        loc = "Remote"
    else:
        parts = [item.get("job_city"), item.get("job_state"), item.get("job_country")]
        loc = ", ".join([p for p in parts if p])
    jd     = item.get("job_description") or ""
    url    = item.get("job_apply_link") or item.get("job_google_link") or ""
    posted = item.get("job_posted_at_datetime_utc") or ""
    j = ingest._norm("jsearch", company, title, loc, url, jd, posted)
    j["publisher"] = item.get("job_publisher", "") or ""
    return j

def build_queries(role_keywords, locations, cap=8):
    """Build 'title in location' queries from the user's keywords + locations (JSearch format)."""
    kws  = role_keywords or ["product manager"]
    locs = locations or ["India"]
    qs = []
    for kw in kws:
        for loc in locs:
            qs.append(f"{kw} in {loc}")
    # de-dupe preserving order, cap to control cost
    seen, out = set(), []
    for q in qs:
        if q.lower() not in seen:
            seen.add(q.lower()); out.append(q)
    return out[:cap]

# ─────────────────────────────────────────────────────────────────────────────
# Discovery (mirrors ingest.discover's filtering, returns (jobs, report))
# ─────────────────────────────────────────────────────────────────────────────
def jsearch_discover(cfg, role_keywords, locations, freshness_days, remote_ok,
                     strict_title=False, on_progress=None):
    """Run all JSearch queries, filter + dedupe, return (jobs, report_rows)."""
    js = cfg.get("jsearch", {}) or {}
    api_key = get_jsearch_key()
    jobs, report = [], []
    if not js.get("enabled") or not api_key:
        return jobs, report

    queries  = js.get("queries") or build_queries(role_keywords, locations)
    country  = js.get("country", "in")
    posted   = js.get("date_posted", "month")
    npages   = js.get("num_pages", 1)
    provider = js.get("provider", "openwebninja")
    base_url = js.get("base_url", DEFAULT_BASE)
    seen     = set()

    for idx, q in enumerate(queries):
        if on_progress: on_progress("jsearch", idx, len(queries), q)
        try:
            data  = jsearch_query(api_key, q, country, posted, npages, provider, base_url)
            items = data.get("data") or []
        except Exception as e:
            report.append({"source":"jsearch","token":q,"fetched":0,"kept":0,"drop_title":0,
                           "drop_loc":0,"drop_stale":0,"sample_locs":"","note":str(e)[:70]})
            continue
        d = {"title":0,"loc":0,"stale":0}; kept = 0; locs_seen = {}
        for it in items:
            j = _normalize(it)
            if not j["title"] or not j["jd_text"]: continue
            ld = j.get("location") or "—"; locs_seen[ld] = locs_seen.get(ld,0)+1
            if j["content_hash"] in seen: continue
            if not ingest._title_match(j, role_keywords, strict_title): d["title"]+=1; continue
            if not ingest._loc_match(j, locations or [], remote_ok):      d["loc"]+=1;   continue
            if not ingest._fresh(j, freshness_days):                      d["stale"]+=1; continue
            seen.add(j["content_hash"]); jobs.append(j); kept += 1
        top = sorted(locs_seen.items(), key=lambda x:-x[1])[:4]
        report.append({"source":"jsearch","token":q,"fetched":len(items),"kept":kept,
                       "drop_title":d["title"],"drop_loc":d["loc"],"drop_stale":d["stale"],
                       "sample_locs":"; ".join(f"{l} ({n})" for l,n in top),"note":""})
        time.sleep(0.4)   # be polite to the API
    if on_progress: on_progress("jsearch", len(queries), len(queries), "done")
    return jobs, report

def health(api_key, provider="openwebninja", base_url=DEFAULT_BASE, country="in"):
    """Quick connectivity + key check."""
    try:
        data = jsearch_query(api_key, "product manager in India", country, "month", 1, provider, base_url)
        n = len(data.get("data") or [])
        return True, f"OK — sample query returned {n} results"
    except Exception as e:
        return False, str(e)[:160]
