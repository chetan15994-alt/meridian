"""Load/save config.yaml and resume_master.yaml so EVERYTHING is editable
from the web app — no hand-editing YAML.

v1.15.0: all I/O is UTF-8 + atomic (see fileio.py); loads are None-guarded so a
missing/empty file can never crash the app with AttributeError; and every write
of the resume invalidates score.resume_blob's cache — previously, editing the CV
and re-running the pipeline in the same Streamlit session silently scored jobs
against the OLD resume (the cache was never cleared)."""
import os
import fileio

_HERE = os.path.dirname(__file__)
CONFIG = os.path.join(_HERE, "config.yaml")
RESUME = os.path.join(_HERE, "resume_master.yaml")


def invalidate_resume_caches():
    """Clear every in-process cache derived from resume_master.yaml.
    Call after ANY write to the resume file (save, snapshot-restore)."""
    try:
        import score
        score.resume_blob.cache_clear()
    except Exception:
        # score's heavy deps may be absent in stripped environments; a missing
        # cache is then impossible anyway, so failing to clear is safe.
        pass


def load_config():
    return fileio.read_yaml(CONFIG, default={}) or {}


def save_config(cfg):
    fileio.write_yaml_atomic(CONFIG, cfg)


def load_resume():
    return fileio.read_yaml(RESUME, default={}) or {}


def save_resume(r, label="", source="manual"):
    """Save resume_master.yaml — auto-snapshots the current version first,
    then invalidates all resume-derived caches."""
    import cv as _cv
    _cv.save_snapshot(label=label or "before save", source=source)
    fileio.write_yaml_atomic(RESUME, r)
    invalidate_resume_caches()
