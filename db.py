"""SQLite storage. Zero setup — the file is created on first run.

Time convention (v1.15.0, unchanged storage formats):
- machine/audit timestamps (fetched_at, scored_at) -> naive UTC ISO
- user-facing calendar fields (applied_at, follow_up_at) -> local dates
- display timestamps (documents/prep_packs created/updated) -> local datetime
"""
import sqlite3, json, datetime, os, contextlib

DB_PATH = os.path.join(os.path.dirname(__file__), "jobcopilot.db")

def _utcnow():
    """Naive-UTC now (same stored format as before) without the deprecated
    datetime.utcnow(), which is scheduled for removal."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

@contextlib.contextmanager
def conn():
    """Yield a connection wrapped in a transaction AND close it afterwards.
    sqlite3's own context manager only commits/rolls back — it never closes,
    which leaked one file handle per DB call (and on Windows, open handles
    hold file locks that can interfere with update.ps1's backup copy)."""
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()

def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS jobs(
            content_hash TEXT PRIMARY KEY,
            source TEXT, company TEXT, title TEXT, location TEXT,
            url TEXT, jd_text TEXT, posted_at TEXT, fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS scores(
            content_hash TEXT PRIMARY KEY,
            fit REAL, semantic REAL, keywords REAL, skills REAL, seniority REAL,
            tier TEXT, missing_keywords TEXT, missing_skills TEXT,
            rationale TEXT, scored_at TEXT,
            FOREIGN KEY(content_hash) REFERENCES jobs(content_hash)
        );
        CREATE TABLE IF NOT EXISTS applications(
            content_hash TEXT PRIMARY KEY,
            status TEXT DEFAULT 'not_applied',
            resume_path TEXT, prompt_path TEXT,
            applied_at TEXT, follow_up_at TEXT, notes TEXT,
            FOREIGN KEY(content_hash) REFERENCES jobs(content_hash)
        );
        CREATE TABLE IF NOT EXISTS documents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type TEXT, content_hash TEXT,
            company TEXT, title TEXT,
            label TEXT, path TEXT, mode TEXT, created_at TEXT
        );
        """)
    migrate()
    migrate_v14()
    migrate_v15()

def add_document(doc_type, path, company="", title="", content_hash=None, label="", mode=""):
    """Register a generated/uploaded document for cataloguing, versioning and download."""
    import datetime as _dt
    with conn() as c:
        cur = c.execute("""INSERT INTO documents
            (doc_type,content_hash,company,title,label,path,mode,created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (doc_type, content_hash, company, title, label, path, mode,
             _dt.datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid

def list_documents(doc_type=None):
    with conn() as c:
        if doc_type:
            rows = c.execute("SELECT * FROM documents WHERE doc_type=? ORDER BY created_at DESC",(doc_type,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]

def documents_for(content_hash):
    with conn() as c:
        rows = c.execute("SELECT * FROM documents WHERE content_hash=? ORDER BY created_at DESC",(content_hash,)).fetchall()
    return [dict(r) for r in rows]

def delete_document(doc_id):
    import os as _os
    with conn() as c:
        row = c.execute("SELECT path FROM documents WHERE id=?", (doc_id,)).fetchone()
        c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    if row and row["path"] and _os.path.exists(row["path"]):
        try: _os.remove(row["path"])
        except Exception: pass

def upsert_job(j):
    with conn() as c:
        c.execute("""INSERT OR IGNORE INTO jobs
            (content_hash,source,company,title,location,url,jd_text,posted_at,fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (j["content_hash"], j["source"], j["company"], j["title"], j["location"],
             j["url"], j["jd_text"], j.get("posted_at",""),
             _utcnow().isoformat()))

def add_manual_job(company, title, location, url, jd_text):
    """Create a first-class job from user-entered details (e.g. found on LinkedIn).
    Returns the content_hash. It then flows through scoring, tailoring and tracking
    identically to discovered jobs. import ingest lazily to reuse the same hashing."""
    import ingest, datetime as _dt
    j = ingest._norm("manual", company.strip(), title.strip(), location.strip(),
                     url.strip(), jd_text.strip(),
                     _utcnow().isoformat())
    upsert_job(j)
    return j["content_hash"]

def unscored_jobs():
    with conn() as c:
        rows = c.execute("""SELECT j.* FROM jobs j
            LEFT JOIN scores s ON j.content_hash=s.content_hash
            WHERE s.content_hash IS NULL""").fetchall()
    return [dict(r) for r in rows]

def save_score(h, sc):
    with conn() as c:
        c.execute("""INSERT OR REPLACE INTO scores
            (content_hash,fit,semantic,keywords,skills,seniority,tier,
             missing_keywords,missing_skills,rationale,scored_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (h, sc["fit"], sc["semantic"], sc["keywords"], sc["skills"], sc["seniority"],
             sc["tier"], json.dumps(sc["missing_keywords"]), json.dumps(sc["missing_skills"]),
             sc["rationale"], _utcnow().isoformat()))
        c.execute("INSERT OR IGNORE INTO applications(content_hash) VALUES (?)", (h,))

def ranked_jobs(tier=None):
    q = """SELECT j.*, s.fit,s.semantic,s.keywords,s.skills,s.seniority,s.tier,
                  s.missing_keywords,s.missing_skills,s.rationale,
                  a.status,a.resume_path,a.prompt_path,a.applied_at,a.follow_up_at,a.notes,a.cover_path,a.variant
           FROM jobs j JOIN scores s ON j.content_hash=s.content_hash
           LEFT JOIN applications a ON j.content_hash=a.content_hash"""
    if tier:
        q += " WHERE s.tier=?"
    q += " ORDER BY s.fit DESC"
    with conn() as c:
        rows = c.execute(q, (tier,) if tier else ()).fetchall()
    return [dict(r) for r in rows]

_APP_COLS = {"status","resume_path","prompt_path","applied_at","follow_up_at",
             "notes","cover_path","variant"}

def set_app(h, **fields):
    if not fields: return
    bad = set(fields) - _APP_COLS
    if bad:
        raise ValueError(f"set_app: unknown column(s) {sorted(bad)} — allowed: {sorted(_APP_COLS)}")
    cols = ",".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO applications(content_hash) VALUES (?)", (h,))
        c.execute(f"UPDATE applications SET {cols} WHERE content_hash=?",
                  (*fields.values(), h))

def stats():
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_tier = dict(c.execute(
            "SELECT tier,COUNT(*) FROM scores GROUP BY tier").fetchall())
        applied = c.execute(
            "SELECT COUNT(*) FROM applications WHERE status!='not_applied'").fetchone()[0]
    return {"total": total, "by_tier": by_tier, "applied": applied}

def _safe_add_column(c, table, col, decl):
    try: c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    except Exception: pass

def migrate():
    with conn() as c:
        _safe_add_column(c, "applications", "cover_path", "TEXT")
        _safe_add_column(c, "applications", "variant", "TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS usage(
            day TEXT PRIMARY KEY, calls INTEGER, tin INTEGER, tout INTEGER, cost REAL)""")

def add_usage(tin, tout, cost):
    day = datetime.date.today().isoformat()
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS usage(
            day TEXT PRIMARY KEY, calls INTEGER, tin INTEGER, tout INTEGER, cost REAL)""")
        row = c.execute("SELECT calls,tin,tout,cost FROM usage WHERE day=?", (day,)).fetchone()
        if row:
            c.execute("UPDATE usage SET calls=?,tin=?,tout=?,cost=? WHERE day=?",
                      (row[0]+1, row[1]+tin, row[2]+tout, row[3]+cost, day))
        else:
            c.execute("INSERT INTO usage VALUES (?,?,?,?,?)", (day,1,tin,tout,cost))

def usage_today():
    day = datetime.date.today().isoformat()
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS usage(
            day TEXT PRIMARY KEY, calls INTEGER, tin INTEGER, tout INTEGER, cost REAL)""")
        row = c.execute("SELECT calls,tin,tout,cost FROM usage WHERE day=?", (day,)).fetchone()
    return {"calls":row[0],"tin":row[1],"tout":row[2],"cost":row[3]} if row else \
           {"calls":0,"tin":0,"tout":0,"cost":0.0}

def migrate_v14():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS outreach(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT, contact TEXT, channel TEXT, status TEXT,
            notes TEXT, created_at TEXT, follow_up_at TEXT)""")

def add_outreach(company, contact, channel, status, notes, follow_up_at):
    migrate_v14()
    with conn() as c:
        c.execute("""INSERT INTO outreach(company,contact,channel,status,notes,created_at,follow_up_at)
                     VALUES (?,?,?,?,?,?,?)""",
                  (company, contact, channel, status, notes,
                   datetime.date.today().isoformat(), follow_up_at))

def list_outreach():
    migrate_v14()
    with conn() as c:
        rows = c.execute("SELECT * FROM outreach ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]

_OUTREACH_COLS = {"company","contact","channel","status","notes","follow_up_at"}

def update_outreach(oid, **fields):
    if not fields: return
    bad = set(fields) - _OUTREACH_COLS
    if bad:
        raise ValueError(f"update_outreach: unknown column(s) {sorted(bad)} — allowed: {sorted(_OUTREACH_COLS)}")
    cols = ",".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE outreach SET {cols} WHERE id=?", (*fields.values(), oid))

def migrate_v15():
    """Application Prep Pack storage (read-only artifact: the questions a form asks
    + the user's reviewed draft answers). One row per job."""
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS prep_packs(
            content_hash TEXT PRIMARY KEY,
            pack_json TEXT, updated_at TEXT)""")

def save_prep_pack(content_hash, pack):
    migrate_v15()
    with conn() as c:
        c.execute("""INSERT OR REPLACE INTO prep_packs(content_hash,pack_json,updated_at)
                     VALUES (?,?,?)""",
                  (content_hash, json.dumps(pack),
                   datetime.datetime.now().isoformat(timespec="seconds")))

def get_prep_pack(content_hash):
    migrate_v15()
    with conn() as c:
        row = c.execute("SELECT pack_json,updated_at FROM prep_packs WHERE content_hash=?",
                        (content_hash,)).fetchone()
    if not row:
        return None
    try:
        pack = json.loads(row["pack_json"])
        pack["_updated_at"] = row["updated_at"]
        return pack
    except Exception:
        return None

def due_followups():
    """Applications + outreach whose follow-up date is today or earlier and still active."""
    today = datetime.date.today().isoformat()
    apps, reach = [], []
    with conn() as c:
        apps = [dict(r) for r in c.execute("""
            SELECT j.company,j.title,a.follow_up_at,a.status,j.content_hash
            FROM applications a JOIN jobs j ON a.content_hash=j.content_hash
            WHERE a.follow_up_at IS NOT NULL AND a.follow_up_at<=?
              AND a.status NOT IN ('offer','rejected','not_applied')
            ORDER BY a.follow_up_at""", (today,)).fetchall()]
        try:
            reach = [dict(r) for r in c.execute("""
                SELECT id,company,contact,follow_up_at,status FROM outreach
                WHERE follow_up_at IS NOT NULL AND follow_up_at<=?
                  AND status NOT IN ('replied','closed')
                ORDER BY follow_up_at""", (today,)).fetchall()]
        except Exception:
            reach = []
    return {"applications": apps, "outreach": reach}
