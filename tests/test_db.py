"""db: column whitelists, prep-pack round trip, closed connections, migrations."""
import pytest


def test_set_app_rejects_unknown_columns(tmp_db):
    db = tmp_db
    db.upsert_job({"content_hash": "h1", "source": "manual", "company": "X",
                   "title": "PM", "location": "", "url": "", "jd_text": "x",
                   "posted_at": ""})
    db.set_app("h1", status="applied")                       # allowed
    with pytest.raises(ValueError):
        db.set_app("h1", **{"status=? WHERE 1=1;--": "pwn"})  # injection-shaped kwarg
    with pytest.raises(ValueError):
        db.update_outreach(1, **{"nope": "x"})


def test_prep_pack_roundtrip(tmp_db):
    db = tmp_db
    pack = {"schema_source": "greenhouse", "apply_url": "https://x",
            "questions": [{"key": "q1", "label": "Why?", "type": "textarea",
                           "required": True, "options": [], "kind": "freetext"}],
            "answers": {"q1": "Because — ₹ and unicode survive."}}
    db.save_prep_pack("h1", pack)
    got = db.get_prep_pack("h1")
    assert got["answers"]["q1"] == pack["answers"]["q1"]
    assert "_updated_at" in got
    assert db.get_prep_pack("missing") is None


def test_connections_are_closed(tmp_db):
    """v1.14.0 leaked one sqlite handle per call (sqlite3's context manager
    commits but never closes). The new conn() must close on exit."""
    db = tmp_db
    with db.conn() as c:
        c.execute("SELECT 1")
        held = c
    with pytest.raises(Exception):        # using a CLOSED connection must fail
        held.execute("SELECT 1")


def test_migrations_are_idempotent(tmp_db):
    db = tmp_db
    db.init_db(); db.init_db()            # run twice: additive, no error
    assert db.stats()["total"] == 0
