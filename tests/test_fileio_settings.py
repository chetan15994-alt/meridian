"""fileio + settings: UTF-8 safety, atomic writes, None-guards, cache invalidation."""
import os

import fileio
import settings


def test_utf8_roundtrip_with_indic_currency_and_dashes(tmp_path):
    """The exact content class that crashed v1.14.0 on Windows (cp1252 default)."""
    p = str(tmp_path / "r.yaml")
    data = {"summary": "Led GTM — ₹40Cr ARR, résumé polish", "skills": ["A/B", "GenAI"]}
    fileio.write_yaml_atomic(p, data)
    back = fileio.read_yaml(p)
    assert back == data
    # byte-level check: really UTF-8 on disk
    raw = open(p, "rb").read()
    assert "₹".encode("utf-8") in raw


def test_atomic_write_never_leaves_partial_file(tmp_path):
    p = str(tmp_path / "cfg.yaml")
    fileio.write_yaml_atomic(p, {"a": 1})
    # no stray temp files after a successful write
    assert [f for f in os.listdir(tmp_path) if f.startswith(".tmp_")] == []
    # overwrite keeps old content intact until replace: after write, new content present
    fileio.write_yaml_atomic(p, {"a": 2})
    assert fileio.read_yaml(p) == {"a": 2}


def test_read_yaml_missing_and_empty_return_default(tmp_path):
    missing = str(tmp_path / "nope.yaml")
    assert fileio.read_yaml(missing, default={}) == {}
    empty = str(tmp_path / "empty.yaml")
    open(empty, "w", encoding="utf-8").close()
    assert fileio.read_yaml(empty, default={}) == {}


def test_load_config_and_resume_never_return_none(tmp_path, monkeypatch):
    """v1.14.0: a truncated config.yaml loaded as None -> AttributeError crash loop."""
    monkeypatch.setattr(settings, "CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(settings, "RESUME", str(tmp_path / "resume.yaml"))
    assert settings.load_config() == {}          # missing file
    open(settings.CONFIG, "w", encoding="utf-8").close()
    assert settings.load_config() == {}          # empty file
    assert settings.load_resume() == {}
    # and .get() chains work (the old crash mode)
    assert settings.load_config().get("tailoring", {}).get("mode", "manual") == "manual"


def test_resume_cache_invalidation_hook():
    """v1.14.0 bug: score.resume_blob was lru_cached and NEVER cleared, so an
    in-session CV edit scored jobs against the old resume. The hook must clear it."""
    import score
    score.resume_blob.cache_clear()
    score.resume_blob()                                   # prime
    assert score.resume_blob.cache_info().currsize == 1
    settings.invalidate_resume_caches()
    assert score.resume_blob.cache_info().currsize == 0   # cleared
