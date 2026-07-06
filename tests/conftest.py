"""Make the jobcopilot modules importable when pytest runs from anywhere,
and give every test an isolated throwaway database."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point db.DB_PATH at a per-test temp file so tests NEVER touch the
    user's real jobcopilot.db."""
    import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return db
