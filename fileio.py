"""UTF-8-safe, atomic file I/O — the single place Meridian touches text files.

Why this module exists (v1.15.0):
1. ENCODING. Python's default text encoding on Windows is cp1252 (until 3.15).
   The user's machine IS Windows, and their CV realistically contains characters
   like the rupee sign or en-dashes — with the old bare `open()` calls, saving a
   resume containing "₹40Cr" CRASHED with UnicodeEncodeError, and reading the
   UTF-8 files shipped in each release produced mojibake. Every read/write here
   is explicitly UTF-8.
2. ATOMICITY. `open(path, "w")` truncates the file BEFORE writing; a crash
   mid-write leaves a 0-byte config/resume and the app won't start. All writes
   here go to a temp file in the same directory, then `os.replace()` — which is
   atomic on POSIX and Windows alike (same-volume guaranteed by same-dir temp).

Rules for the rest of the codebase:
- Never call bare `open()` for text — use these helpers (binary I/O is fine).
- Missing file  -> the caller-supplied `default` (no exception).
- Empty file    -> `default` too (yaml/json of "" is None; callers want dicts).
- MALFORMED file -> raises. That's deliberate: silently returning {} for a
  corrupted config would quietly wipe user settings on the next save.
"""

import os
import json
import tempfile

import yaml

ENC = "utf-8"


# ------------------------------ reads ------------------------------
def read_text(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding=ENC) as f:
        return f.read()


def read_yaml(path, default=None):
    txt = read_text(path)
    if txt is None or not txt.strip():
        return default
    parsed = yaml.safe_load(txt)
    return parsed if parsed is not None else default


def read_json(path, default=None):
    txt = read_text(path)
    if txt is None or not txt.strip():
        return default
    return json.loads(txt)


# ------------------------------ writes -----------------------------
def write_text_atomic(path, text):
    """Write text to `path` atomically: temp file in the same dir + os.replace."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d)
    try:
        with os.fdopen(fd, "w", encoding=ENC) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX and Windows
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_yaml_atomic(path, obj, **dump_kw):
    dump_kw.setdefault("sort_keys", False)
    dump_kw.setdefault("allow_unicode", True)
    write_text_atomic(path, yaml.safe_dump(obj, **dump_kw))


def write_json_atomic(path, obj, **dump_kw):
    dump_kw.setdefault("indent", 2)
    dump_kw.setdefault("ensure_ascii", False)
    write_text_atomic(path, json.dumps(obj, **dump_kw))
