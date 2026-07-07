"""db: interview session/turn persistence round-trip."""
import interview as iv


def test_interview_session_roundtrip(tmp_db):
    db = tmp_db
    sess = iv.new_session(loop_key="generic", resume_summary="Senior PM")
    sess, q = iv.start_round(sess, llm_fn=None)
    db.save_interview_session("s1", sess)
    got = db.get_interview_session("s1")
    assert got and got["rounds"][0]["question"] == q
    assert "_updated_at" in got


def test_interview_session_missing_returns_none(tmp_db):
    db = tmp_db
    assert db.get_interview_session("does-not-exist") is None


def test_interview_turn_log_and_delivery_trend(tmp_db):
    db = tmp_db
    sess = iv.new_session(loop_key="generic")
    sess, q = iv.start_round(sess, llm_fn=None)
    db.save_interview_session("s2", sess)
    db.log_interview_turn("s2", 0, 0, "interviewer", q)
    db.log_interview_turn("s2", 0, 1, "candidate", "my answer",
                          delivery_metrics={"wpm": 140, "filler_count": 2})
    trend = db.delivery_metrics_trend()
    assert len(trend) == 1 and trend[0]["wpm"] == 140


def test_list_interview_sessions_ordering_and_filter(tmp_db):
    db = tmp_db
    for i in range(3):
        sess = iv.new_session(loop_key="generic")
        db.save_interview_session(f"s{i}", sess, content_hash="job1" if i < 2 else "job2")
    all_sessions = db.list_interview_sessions()
    assert len(all_sessions) == 3
    job1_only = db.list_interview_sessions(content_hash="job1")
    assert len(job1_only) == 2


def test_interview_migration_is_idempotent(tmp_db):
    db = tmp_db
    db.migrate_v16()
    db.migrate_v16()  # must not raise


def test_job_picker_options_excludes_heavy_columns(tmp_db):
    """The dropdown-only query must never carry jd_text — that's the whole
    point of this function existing separately from ranked_jobs."""
    db = tmp_db
    db.upsert_job({"content_hash": "h1", "source": "greenhouse", "company": "Acme",
                  "title": "Senior PM", "location": "Bengaluru", "url": "u",
                  "jd_text": "x" * 5000, "posted_at": ""})
    db.save_score("h1", {"fit": 80, "semantic": 1, "keywords": 1, "skills": 1, "seniority": 1,
                         "tier": "A", "missing_keywords": [], "missing_skills": [], "rationale": ""})
    opts = db.job_picker_options()
    assert "jd_text" not in opts[0]
    assert opts[0]["company"] == "Acme" and opts[0]["fit"] == 80


def test_get_job_returns_full_row_and_none_for_missing(tmp_db):
    db = tmp_db
    db.upsert_job({"content_hash": "h1", "source": "greenhouse", "company": "Acme",
                  "title": "Senior PM", "location": "Bengaluru", "url": "u",
                  "jd_text": "full jd text here", "posted_at": ""})
    full = db.get_job("h1")
    assert full["jd_text"] == "full jd text here"
    assert db.get_job("does-not-exist") is None


def test_content_hash_survives_a_save_that_omits_it(tmp_db):
    """save_interview_session uses INSERT OR REPLACE. A caller that forgets to
    pass content_hash on a later save (e.g. abort, post-evaluation) must NOT
    silently wipe the job association set on an earlier save."""
    db = tmp_db
    job = {"content_hash": "job123", "company": "Acme", "title": "PM"}
    sess = iv.new_session(loop_key="generic", job=job)
    sess, _ = iv.start_round(sess, llm_fn=None)
    db.save_interview_session("sX", sess, content_hash="job123")
    db.save_interview_session("sX", sess)  # no content_hash passed this time
    by_job = db.list_interview_sessions(content_hash="job123")
    assert len(by_job) == 1
