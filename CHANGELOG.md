# Changelog — Meridian

All notable changes. Newest first.

## v1.19.0  — Performance release: measured, structural fixes for app + Interview Coach speed

Diagnosed with measurements, not vibes. Raw DB cost of the heavy queries was profiled at ~29ms
per rerun (270 realistic jobs, 4 full fetches) — so the database was NOT the bottleneck. The
real costs were structural, and each fix below names its mechanism.

**FIXED — the structural one: st.tabs executed ALL six tabs on every click.** Streamlit reruns
the entire script on every widget interaction, and `st.tabs` renders every tab's body each time —
so a single radio click on the Interview Coach setup screen also re-fetched ~270 full jobs FOUR
times (queue, tracker, analytics, review-distribution ≈ 1.1MB of JD text per fetch), rebuilt the
270-row data editor, re-ran CV completeness scoring and the analytics aggregations, and
re-serialized all of it to the browser. Replaced `st.tabs` with `st.segmented_control` navigation
+ `if nav == ...:` guards: only the ACTIVE section's code now runs per interaction. This is the
fix that makes every click in the Interview tab (and everywhere else) stop paying for five other
tabs. Section choice persists across reruns via the widget key.

**FIXED — round transitions ran 2-3 sequential LLM calls (10-30s of dead air).** The old flow at
every round boundary: evaluate the finished round, then synthesize-or-generate the next opening
question, back to back. New deferred-evaluation flow: at wrap-up the session advances immediately
(at most ONE opening-question call); all `awaiting_eval` rounds are batch-scored at the report
stage behind a real progress bar — where waiting is expected. Total LLM calls are IDENTICAL; they
just moved to where they don't interrupt the conversation. Engine change:
`evaluate_round(..., round_idx=None)` now accepts an explicit index (tested both paths).

**FIXED — local voice transcription was needlessly slow on CPU.** Three concrete levers in
`voice_io`: (1) `beam_size=1` greedy decoding — 2-5x faster than faster-whisper's default beam of
5, a minor accuracy cost that's the right trade here since the transcript is reviewed/edited
before submitting anyway; (2) `vad_filter=True` skips decoding silence; (3) default model is now
`base` (~2-3x faster than the previous `small`), selectable in Settings (tiny/base/small).
Additionally the model is now PREWARMED in a background thread the moment a voice session becomes
active, so the first answer no longer pays the 5-15s model-load on top of transcription
(thread-safe per-size cache with a lock; best-effort, silently no-op if the package is missing).

**NEW — optional fast model for interview turns only.** Interview turns are latency-critical in a
way tailoring isn't. `Settings -> Interview Coach -> "Faster model for interviews"` overrides the
model for interview LLM calls ONLY (same provider/base_url/key): point it at a Haiku/gpt-4o-mini
class model for a visibly snappier live loop while tailoring keeps the higher-quality model.
Empty = unchanged behaviour. Also new: whisper model-size selector. Save now `update()`s the
interview_coach block instead of replacing it, so future keys survive a settings save.

**Also:** the interview content YAML loaders (rubrics/loops/question-bank) are now `lru_cache`d —
they were re-read and re-parsed from disk on every rerun; now a dict lookup, with
`clear_content_cache()` for tests/future editing.

**Honest constraints, unchanged and worth restating:**
- The per-turn LLM API round-trip (typically 2-8s depending on model/provider) is inherent to a
  live AI interviewer; the model-override above is the lever, streaming is a possible future.
- Live interviewing structurally CANNOT run on free Claude Pro manual copy-paste (as stated at
  v1.18.0). What IS free: local voice (STT/TTS, ₹0 forever) and the whole engine outside LLM
  calls. The paid part is only the interviewer/evaluator tokens; with a Haiku-class override a
  full round is roughly a few cents.
- Streamlit's first-launch import time (torch/sentence-transformers) is a pre-existing cost of
  the stack, unrelated to and unchanged by this release; embeddings were already lazy-loaded.

Tests: 75/75 passing (+5 new: deferred-eval-by-index path incl. synthesis, eval-by-index status
guard, loader cache identity + clear, prewarm safe-fail, whisper model-size passthrough).

## v1.18.1  — Fixes from a real run: Personio double-fetch, Interview Coach slowness, voice UX

Diagnosed directly from a pasted terminal log of a real `python run.py` + `streamlit run app.py`
session, not guessed at.

- **FIXED: every Personio company was being fetched twice.** Counted the log line-by-line —
  each of the ~18 Personio companies appeared in the failure output exactly twice, not once.
  Root cause not fully pinned down without the user's actual config.yaml (candidates: a hand-edited
  source list, or a bulk-add path that doesn't check for an existing entry), so the fix closes the
  bug class at the last point before any network call happens instead of chasing one upstream cause:
  `ingest._parallel_fetch` now defensively dedupes by (source, token) before submitting to the
  thread pool, on top of `_normalize_tokens`'s existing dedup. Verified with a test that feeds the
  exact duplicate-token shape from the log and confirms the fetcher is called once per company.
- **FIXED: Interview Coach's job picker was measurably wasteful.** `db.ranked_jobs()` — a
  `SELECT j.*` pulling full JD text, rationale, and every applications column for every row — was
  being re-queried in full on every single widget interaction ANYWHERE in the app (Streamlit
  reruns the whole script top-to-bottom on any click, in any tab; this isn't new, but Interview
  Coach's setup form made it newly visible). New `db.job_picker_options()` (5 lightweight columns,
  no JD text) + `db.get_job()` (fetch the ONE full row only once a job is actually picked), wrapped
  in `st.cache_data(ttl=120)` and explicitly invalidated right after "Run pipeline now" so new
  results show up immediately rather than waiting out the cache.
- **FIXED: the pyttsx3 error surfaced too late.** The user hit "(voice playback unavailable:
  pyttsx3 isn't installed...)" mid-interview, having only run `pip install -r requirements.txt`
  (voice packages are deliberately optional/commented there) and missed the separate voice-install
  instruction. New `voice_io.local_engine_status()` (a cheap import-only check, never loads a
  model) now warns upfront on the Interview Coach setup screen, before a session starts, with the
  exact install command AND the alternative of switching to the API engine in Settings — instead of
  discovering it live, turn by turn.
- Cleaned up 18 instances of Streamlit's deprecated `use_container_width` (flagged in the log,
  scheduled for removal after 2025-12-31) → `width='stretch'`/`width='content'`, matching the
  Streamlit 1.58 the user has installed. Cosmetic — was only producing log noise, not slowness.
- **Explained, not changed:** the overall `python run.py` wall-clock time (querying 9 ATS portals
  x ~20 target companies = ~180 combinations) is mostly inherent to how broad that search currently
  is, not a regression from this release — the Personio fix removes real duplicate work but is a
  small fraction of ~180 total calls. The concrete, already-available lever: run the "Batch company
  verifier" in Settings once per company to resolve its ONE real ATS, then trim `sources` down to
  just that portal — cutting the fetch count roughly 9x for that company on every future run.
  Streamlit/sentence-transformers/torch import time on first launch is a separate, pre-existing
  cost of the stack, unrelated to this release.

Tests: 70/70 passing (+4 new: fetch dedup under duplicate input, lightweight picker excludes heavy
columns, single-job lookup, local-engine-status shape).

## v1.18.0  — Interview Coach: voice-first AI mock interviewer for PM rounds

**Feature, not a demo.** A structured, multi-round mock interviewer covering the four core PM
round types (Product Sense, Execution/Analytical, RCA/diagnosis, Behavioral), voice-first via
Streamlit's native `st.audio_input`/`st.audio` (no custom JS component, no WebRTC), grounded in
your real CV and your actual target jobs from the Review Queue. Never praises or reassures in
Realistic mode — that's structural, not a prompt suggestion (see rationale below).

**Researched before building, not assumed:**
- Verified `st.audio_input` is a native, current Streamlit widget (record button, returns WAV
  bytes) — no custom component or streaming/VAD complexity needed. Since we get a COMPLETE clip
  per turn, STT is a simple batch call, not real-time streaming.
- Verified a concrete, current stack rather than picking categories: local `faster-whisper` (STT)
  + local `pyttsx3`/Windows SAPI5 (TTS) as the free/offline default; OpenAI's transcription/speech
  endpoints as the optional API tier — deliberately reusing the SAME credential already saved for
  tailoring, so voice is a Settings toggle, not a new vendor signup.
- Found and worked around a documented Streamlit bug: multiple `st.audio(autoplay=True)` calls in
  one script run can collide on auto-generated element IDs (no `key` param exists to fix it) — the
  UI speaks only the single newest interviewer line, once, never loops autoplay over history.

**New modules:**
- `interview_content/` (rubrics.yaml, loops.yaml, question_bank.yaml) — anchored 1-4 scoring
  rubrics per round type, loop templates (Generic, Meta-style, India-Startup, Amazon-style) as
  DATA (a new company loop is a data change, never a code change), seed questions + curveballs +
  probe-ladder guidance. `interview.validate_content()` sanity-checks all three stay consistent.
- `interview_prompts.py` — every LLM prompt, kept to 3 deliberately separate calls (interviewer
  turn, round evaluator, session synthesis). Anti-sycophancy is structural: the interviewer prompt
  forbids praise outright, and the evaluator prompt REQUIRES a verbatim transcript quote for every
  score — unfalsifiable feedback is refused, not just discouraged.
- `interview.py` — the pure engine (rounds -> turns -> evaluation -> synthesis), zero I/O, fully
  testable with an injected fake LLM. The engine — not the model — enforces the follow-up budget
  and the wall-clock wrap-up, so a misbehaving reply can never turn a 45-minute round unbounded;
  verified with a fake LLM that never stops probing (engine forces `move_on` anyway) and a round
  clock forced 999 minutes over budget (forces `wrap_up` WITHOUT even calling the LLM). Evaluator
  validation rejects any score missing an evidence quote and never defaults an invalid hire-bar
  verdict to something flattering — both proven with a deliberately bad evaluator reply.
- `voice_io.py` — STT/TTS adapter registry (local-first, API optional) + local, zero-cost delivery
  metrics (WPM, filler-word rate, hedging phrases, pause distribution) that degrade honestly to
  `None`/empty rather than fabricating numbers when timing data isn't available.
- `db.py` — additive `migrate_v16`: `interview_sessions` (whole engine state as JSON — the engine
  and its persistence can never drift apart) + `interview_turns` (denormalized for future delivery
  trend analytics). Fixed a real bug found in review: `save_interview_session` used
  `INSERT OR REPLACE`, and callers that omit `content_hash` on a later save (abort, post-eval) would
  have silently NULLed out the job association from an earlier save — fixed by deriving it from the
  session's own stored job data when not explicitly passed, closing the whole bug class at the
  source rather than patching every call site.
- `app.py` — new **Interview Coach** tab: setup (job/loop/level/mode/input picker) -> active
  session (chat-style transcript, pause/resume/abort, text or voice input, TTS playback of the
  interviewer) -> report (per-round rubric scores with evidence quotes, hire-bar verdict, overall
  strengths/fixes, delivery report). New Settings section for STT/TTS engine choice. This is the
  first Meridian feature that always requires an API key — cost-tracked and capped through the
  exact same daily-cap infrastructure as tailoring; the tab refuses to start in manual mode or once
  the cap is already hit, with a clear message either way.
- Two structural bugs found and fixed during review (not just by the automated tests): the autoplay
  collision above, and a session-state key rename that fell outside the "start a new session"
  cleanup filter — would have silently skipped voice playback of a fresh session's opening question
  whenever a stale round-index key collided. Both are now covered by a permanent regression test.

**What's tested vs. what needs your live check:** the entire engine (rubrics, loops, decisions,
timing, evaluation, synthesis) and all of voice_io's logic (delivery metrics, adapter interfaces)
are fully tested offline with injected fakes — 66/66 tests passing, +21 new. What's NOT and CANNOT
be tested here: real audio capture/playback, real faster-whisper/pyttsx3 output quality, and the
Streamlit UI rendering itself (no audio hardware or streamlit in this sandbox, as always). Try it
on a Tier-A job from your queue and confirm voice capture, transcription, and playback all work
before relying on it for real interview prep.

**Explicitly rejected, on purpose (see discovery dossier, section I):** live-interview answer
feeding, real-interview recording, video/body-language analysis, voice-cloning real interviewers,
full-duplex/barge-in voice. This coaches; it never assists during a real interview.

## v1.17.0  — GitHub sync (docs + packaging only; zero application-code changes)

- **New `HOW_TO_RUN.md`** — was referenced by PROJECT_STATE.md as an existing doc but had never
  actually been created (audited: only README.md/UPDATING.md existed). Now covers local run, the
  new GitHub push/pull workflow, a tracked-vs-ignored table with rationale, and troubleshooting.
- **Verified the target repo first, didn't assume:** `git ls-remote` against
  `https://github.com/chetan15994-alt/meridian` confirmed it's public and empty (0 refs) before
  any packaging decision was made.
- **Hardened `.gitignore` for git specifically** (separate from, and in addition to, what
  `update.ps1` already preserves locally): `config.yaml` and `resume_master.yaml` are now
  git-ignored. Both contain real personal data — actual target companies/keywords, and your actual
  CV (name, contact, employment history) — that should never sit in plaintext in source control,
  private repo or not. `*.zip` and `_backup_*/` are also ignored (releases belong in GitHub
  Releases, not the tracked tree).
- **New tracked templates:** `config.example.yaml` and `resume_master.example.yaml` — clean,
  documented, so a fresh `git clone` is immediately usable (`cp *.example.yaml *.yaml`).
  `resume_master.example.yaml` matches the canonical schema in `cv.py` exactly (contact/summary/
  skills/experiences/education/certifications) rather than being a fresh guess at the shape.
- **Explicit credential boundary, stated plainly in HOW_TO_RUN.md:** the assistant preparing a
  build can never hold or enter GitHub credentials (PAT/SSH key) — this is a hard boundary, not a
  convenience limit — so `git push` is always run by the user, from their own authenticated
  machine, using a folder Claude prepares. This is documented once so it never needs re-explaining.
- **No changes to the release-zip layout or `update.ps1`.** The zip keeps its top-level
  `jobcopilot/` folder (required by `update.ps1`'s extraction path); the git repo is a separately
  packaged, flattened copy of the same source (`app.py` etc. at repo root — the normal GitHub
  layout). The two channels are independent: the zip+`update.ps1` channel remains what protects
  your live app's personal data across code updates; git is version history/backup for the code.
- Tests: unchanged (30/30 passing) — this release touches only docs, `.gitignore`, and two new
  example files; no application logic changed.

## v1.16.0  — Paste-to-import for LinkedIn / any posting (the safe answer to "LinkedIn sync")

**The request was a LinkedIn -> Meridian live sync. Investigated, and NOT built — here's why.**
Verified against current (2026) facts, not memory: there is no sanctioned or stable path.
(1) No official LinkedIn MCP and no Anthropic-LinkedIn partnership exist — every "connect LinkedIn
to Cowork" route is a THIRD-PARTY bridge that drives your logged-in session or a vendor session
pool, not LinkedIn itself. (2) LinkedIn's official API is closed to individuals (~5% partner
acceptance, ~6-month waits; Jobs endpoints are enterprise-gated). (3) LinkedIn User Agreement 8.2
categorically prohibits automated access to login-walled data — the hiQ ruling only covers PUBLIC
data, and job results sit behind your login. (4) 2026 enforcement is fast and behavioral: LinkedIn
flagged 23.5M automated sessions in one quarter and now flags suspicious sessions within ~48h; a
scheduled, repetitive "keep in sync" query is precisely the pattern it detects — applied to the one
account whose loss would be catastrophic mid-search. This reaffirms PROJECT_STATE 3 + 6a rather than
overturning them. A LinkedIn sync was therefore rejected on ToS, account-safety, north-star, and
maintainability grounds.

**What we built instead — the safe, high-value slice.** You're already allowed to VIEW jobs while
logged in; the real friction is retyping them into Meridian. New **paste-to-import**: copy the whole
posting (Ctrl+A/Ctrl+C on the job page), paste once, and Meridian extracts company / title /
location / url / JD for you to confirm before scoring. Nothing here touches LinkedIn — it only parses
text you pasted. Re-pasting the same role upserts by content-hash (no duplicates), which gives the
PRACTICAL benefit of "sync" (paste again anytime; it updates in place) with zero scraping.

- **New module `jobimport.py`** — deterministic, offline, pure-Python parser (`parse_job_text`) that
  reads LinkedIn's stable label structure ("Company · Location · …", "About the job" headers) plus
  generic heuristics; strips UI chrome (Easy Apply, Promoted, applicant counts, "Skip to…" nav,
  "Save <title> at <company>" buttons, "Set alert"); returns per-field confidence + a `needs_review`
  flag. Never raises. Optional LLM fallback (`build_extract_prompt` + `merge_llm`) is extraction-only
  (never invents fields) and is invoked ONLY when the deterministic parse is weak.
- **LLM assist policy: deterministic-first, LLM only on weak parse.** Free and instant for the common
  case (LinkedIn pastes are highly regular); spends tokens only when a required field is missing, is
  cost-capped, and always degrades to the deterministic result if the model fails or is unavailable.
- **UI:** the "Add a job" expander is now two tabs — **Paste full posting** (parse → confirm fields →
  add & score) and **Enter fields manually** (the original flow, unchanged). Shared add/score logic
  extracted to one helper (`add_and_score`) so both paths stay DRY and identical downstream.
- **Tests:** +10 regression tests (30 total, all passing) covering LinkedIn + generic + messy pastes,
  chrome/nav stripping, URL detection, the needs_review logic, LLM-merge overlay rules, and
  empty/garbage inputs that must never crash. A self-caught title-vs-nav-chrome bug found during
  end-to-end testing is now a permanent regression test.

**Not changed / still true:** submission stays manual; imported jobs are first-class (scored,
tailored, prepped, tracked exactly like ATS jobs); no network calls added anywhere in this feature.

## v1.15.0  — Hardening release: two proven bugs fixed + full audit remediation (P0/P1/P2): two proven bugs fixed + full audit remediation (P0/P1/P2)

**P0 — proven, user-facing bugs**
- **FIXED: Windows encoding crash (the #1 finding).** Every text-file read/write previously used
  Python's platform default encoding — cp1252 on the user's Windows laptop. Saving a resume
  containing `₹` (or `—`, `é`) crashed with UnicodeEncodeError, and UTF-8 files shipped in
  releases read back as mojibake. New `fileio.py` module: ALL text I/O is now explicitly UTF-8,
  and all writes are ATOMIC (temp file + `os.replace`, atomic on Windows too) so a crash mid-write
  can never truncate config/resume/secrets to zero bytes. Every module now routes through it;
  zero bare text-mode `open()` calls remain (verified by grep + test).
- **FIXED: stale resume cache silently corrupting scores.** `score.resume_blob` is lru-cached and
  was NEVER invalidated, while the pipeline runs in-process from the app — so editing your CV in
  the CV tab and clicking "Run pipeline" scored every job against the OLD resume until Streamlit
  restarted. `settings.save_resume` and snapshot-restore now call
  `settings.invalidate_resume_caches()`. Proven fixed end-to-end (edit → re-score sees new CV).
- **FIXED: update.ps1 didn't do what the docs promised.** It never backed up `cv_versions/`
  (your CV snapshot history survived updates only because release zips ship the folder empty),
  and it backed up `outputs/` but never RESTORED it. Both folders are now backed up AND restored
  (PowerShell 5.1-compatible). Also added: a warning + confirm prompt if a Python/Streamlit
  process is running, since copying jobcopilot.db mid-write can capture an inconsistent database.

**P1 — robustness**
- Atomic, None-guarded config/resume loading: a missing/empty YAML now yields `{}` instead of
  `None` → the old AttributeError crash-loop on startup after a truncated write is impossible.
- `db.conn()` is now a real context manager: transaction semantics AND a guaranteed close.
  (sqlite3's own context manager commits but never closes — v1.14.0 leaked one file handle per
  DB call; on Windows those handles hold locks that interfere with update backups.) All existing
  `with conn() as c:` call sites work unchanged; verified closed by test.
- Column whitelists in `db.set_app` / `db.update_outreach`: kwarg NAMES were interpolated into
  SQL. All call sites were literal (audited — not exploitable today), but one future
  `**user_dict` call would have been an injection. Unknown columns now raise ValueError.
- `ui.field()` HTML-escapes label/help before its `unsafe_allow_html` render (same landmine class).

**P2 — performance, correctness edges, tests**
- **Parallel discovery:** the fetch phase of `ingest.discover` now runs on a thread pool
  (12 workers), ~10x for multi-company sweeps and one slow ATS no longer stalls the run —
  measured 8×0.5s fetches in 0.50s vs ~4.0s serial. Filtering/dedup/report stay sequential in the
  ORIGINAL token order, so output is deterministic and identical to the old serial behaviour
  (proven by a 3-run determinism test). Progress callbacks fire only from the caller's thread
  (Streamlit-safe; verified by test).
- Removed deprecated `datetime.utcnow()` / `utcfromtimestamp()` (5 sites) with format-identical
  replacements; documented the DB time convention (machine timestamps = naive UTC, user-facing
  calendar fields = local) instead of silently mixing clocks.
- LLM client: non-retryable 4xx errors now fail fast with the server's own error body (previously
  a bad model name burned all retries and reported a generic "failed after 3 attempts"); the
  response_format 400-fallback can no longer mask a genuine 400.
- `apply_prep`: Greenhouse token/job-id are URL-quoted (no path-injection shape even from a
  malformed config); boolean model answers map to the question's real options ("Yes"), never the
  string "True".
- Requirements now carry upper bounds on majors (numpy<3, streamlit<2, …) so a future breaking
  major can't silently break a fresh install; pytest added as the dev dependency.
- **NEW `tests/` package — 20 regression tests, all passing** (`python -m pytest tests/ -q`):
  UTF-8/atomic I/O, load guards, cache invalidation, DB whitelists + connection closing +
  prep-pack round-trip, apply_prep classification/degradation/drafting, the fact-integrity guard
  under tampered input, offline scoring, freshness parsing, and parallel-discover determinism.
  Tests isolate their own temp DB and never touch user data. Previously the test suite existed
  only in chat history — the user's machine had zero regression protection.

**Explicitly NOT changed:** SQLite journal mode stays default (WAL would add -wal/-shm sidecar
files that update.ps1's backup wouldn't capture — worse for the hot-backup problem, not better).
Stored timestamp FORMATS unchanged (old rows sort/compare correctly against new ones).

## v1.14.0  — Application Prep Pack (read-only; submission stays manual)
- **New feature: per-job "Prep application".** For any job in the Review Queue, Meridian now
  fetches the application's REAL question set and drafts editable answers, so you walk into the
  apply page knowing exactly what it asks and with strong first-draft answers in hand. You still
  review, edit, and submit on the employer's page yourself.
- **Why this and not Playwright auto-apply (the original request):** researched it; rejected it.
  (1) The "clean" path doesn't exist for a candidate — Greenhouse's submit endpoint and Lever's
  apply endpoint are both real but gated behind the *employer's* API key (Lever's must be enabled
  by Lever staff), so a job-seeker can't use them to apply to companies they don't work for.
  Auto-submit therefore means driving human-facing forms with a bot. (2) LinkedIn/Naukri-class
  automation is a categorical User-Agreement violation with intensifying enforcement and a
  catastrophic, irreversible downside (permanent loss of a senior candidate's network). (3) It
  attacks the north star (optimizes applications-sent, not interviews/week) and concentrates risk
  at the single most irreversible point in the funnel — an un-reviewed send. (4) It's the least
  testable feature possible in this no-network sandbox. So we built the high-value, zero-ToS-risk
  slice instead: prep, not submit.
- **What's read-only and sanctioned:** Greenhouse exposes the full application schema publicly
  (`GET /v1/boards/{token}/jobs/{id}?questions=true`) — every field, required flags, custom
  questions and demographic questions. We use it live. Lever's public API exposes only standard
  fields, so Lever degrades to a clearly-labelled "standard fields" baseline. Every other source
  (Ashby/Workday/JSearch/manual) degrades to a labelled generic checklist. The user always sees
  which source they're looking at.
- **New module `apply_prep.py`** (READ-ONLY by design — never submits, never logs in):
  `fetch_schema()` resolves the ATS token from the stored job (ingest already stores the token in
  `company` and the job id in `url`, so no new data is needed), normalises every question to
  `{key,label,type,required,options,kind}`, and **never raises** — any network failure degrades to
  the generic checklist with an explanatory note. `draftable_questions()` returns only free-text +
  choice questions; identity, résumé-upload and EEO/demographic questions are deliberately excluded
  from AI drafting. `draft_answers()` batch-drafts via the LLM (injectable for tests).
- **Shared prompt spec** (`tailor.build_prep_answers_prompt`, `as_json` flag): one spec, two
  outputs — strict JSON for API mode, labelled plain text for Manual mode — so the two paths never
  drift. Strict anti-hallucination: questions needing data the CV doesn't contain (expected salary,
  exact notice period) come back EMPTY for the user to fill, never guessed.
- **Persistence:** new `prep_packs` table (`db.save_prep_pack`/`get_prep_pack`, additive
  `migrate_v15`) stores the reviewed pack per job so it can be revisited.
- **Tested:** Greenhouse schema parse + field classification, graceful degradation on network
  failure, Lever/generic fallbacks, JSON/plain-text prompt outputs, draft-answer key filtering, and
  DB round-trip all validated offline with injected stubs (sandbox has no network — consistent with
  build/test/ship discipline). UI glue (`render_prep`) ast-validated; live render needs your check.

## v1.13.1  — complete tooltip sweep (point 7 fully closed)
- All 21 field-level `help=` tooltips across the app (Settings, Review Queue, JSearch, Tailoring,
  CV tab) converted to the `ui.field(label, hint)` inline pattern — hint text appears immediately
  next to the field label rather than far-right. Only correct uses remain: `st.metric` tooltips,
  `column_config` data-editor hints, and disabled-button tooltips that explain why an action is
  unavailable.
- v1.13.1 is a complete, additive release — all 7 features from v1.13.0 are included intact.

## v1.13.0  — CV document hub, smarter queue, AI-assisted CV & answers, clean UI
- **CV documents hub (new)**: every generated/uploaded CV and cover letter is now registered in a
  `documents` table with type (Original/Tailored/Cover), date, label and mode. View, download,
  delete, and compare versions from a new table in the CV tab. Files are timestamped so versions
  never overwrite each other (true versioning).
- **Review Queue upgraded**: search by company/role, filter by tier/source/status/tailored, and a
  selected-row **action bar** — Open Job, Tailor CV, Cover letter, Mark applied — driven by row
  selection. Tier still defaults to All.
- **Completeness fixes (point 4)**: each issue in the report now offers a one-click **Generate fix**
  (API/Anthropic) that drafts a concrete improvement to review before applying to your CV.
- **Application Answers generator (point 5)**: draft answers to free-text application questions from
  your CV + a role's JD + your saved answers; edit before use and optionally save to your answer bank.
- **Professional UI (point 6)**: removed all ~35 decorative emojis; tabs, headers and actions now use
  Streamlit Material icons via a single `ui.py` design system for a consistent, production look.
- **Inline field hints (point 7)**: a `ui.field()` helper places hints next to labels instead of the
  far-right tooltip.
- **Bug fix**: restored `save_prompt` and `tailor_template` (a no-LLM/manual path that had been
  accidentally dropped in v1.12.0 and would have errored in Manual mode).

## v1.12.0  — CV tailoring overhaul + tracker + table queue
- **Mode toggle, key not re-asked**: a clear Manual ⟷ API toggle that saves instantly. In API mode
  the saved key is used automatically; key entry is collapsed behind "change it" — no re-entry.
- **CV template redesigned (ATS-safe, faithful)**: cleaner professional layout with proper margins,
  section rules, and — critically — **fixed a bug where Education/Certifications rendered as raw
  Python dicts**. Education now reads e.g. "MBA, Marketing — IIM Kashipur (2020)".
- **Dual-mode tailoring from one shared spec**: API and Claude-Pro prompts now derive from a single
  richer prompt spec (stronger role framing, structure/voice preservation, smarter keyword weaving,
  strict anti-hallucination, higher bullet-quality bar) so both modes produce consistent, impactful
  CVs. Fact-integrity guard retained (roles/dates/education/companies always from the master).
- **Add a job manually**: paste a role found on LinkedIn/Naukri/elsewhere — it's scored, tailored and
  tracked exactly like discovered jobs (source='manual').
- **Review Queue table**: the whole queue now shows in a sortable table (Tier/Fit/Company/Role/
  Location/Source/Status/Tailored) above the per-role expanders. Tier filter defaults to **All**.
- **Hardened CV module**: schema validation on parse and restore; corrupt snapshots/parses are
  rejected with a clear message instead of silently corrupting the master.

## v1.11.0  — web-wide search + company registry (Layer 0 + breadth)
- **Web-wide search via JSearch (new)**: beyond the ATS portals, Meridian can now query the open
  web (Google for Jobs — LinkedIn, Indeed, Naukri, company pages) through a sanctioned API. Legal,
  no scraping, full job descriptions for tailoring. Free tier (no credit card) at OpenWeb Ninja;
  RapidAPI also supported. Configure under Settings → Web-wide search; runs automatically each
  pipeline run when enabled. Optimised for India (country=in).
- **Unified pipeline**: JSearch results are normalised into the same schema, run through the same
  semantic title/location/freshness filters and scorer, and cross-source de-duplicated against ATS
  results (a Greenhouse role also seen on LinkedIn is kept once).
- **Per-query funnel**: the run report now shows JSearch alongside ATS sources with the same
  fetched → title → location → stale → kept breakdown.
- **Target-company registry (Layer 0)**: your company list now persists across sessions (Save
  company list), formalising the company-centric model that feeds ATS resolution.
- **Merge-safe secrets**: the JSearch key and the Anthropic key now coexist; saving one no longer
  clobbers the other.

## v1.10.0
- **CV upload & parse (new tab)**: upload a PDF or DOCX; Claude extracts it into structured YAML.
  Review the parsed output and completeness score before confirming. Your current CV is always
  auto-snapshotted before overwriting.
- **Auto-versioning**: every call to save_resume() now creates a timestamped snapshot in
  cv_versions/ first. Microsecond-precision filenames prevent collisions.
- **Version history panel**: browse all saved CV snapshots, restore any of them (auto-snapshots
  the current first), or delete old ones. Restore is non-destructive.
- **Completeness checker (fixes live bug)**: scans resume_master.yaml for placeholders
  ([fill in: ...] patterns), empty required fields, weak bullet openers (responsible for / helped),
  and bullets with no quantified metrics. Shows errors/warnings/tips with a 0–100 score.
  Errors block clean tailoring output — fix them first.
- **New 📄 CV tab** in the main navigation.

## v1.9.0  — semantic matching + full funnel visibility
- **Geography-aware locations**: typing a country now matches its cities/states. "India" matches
  Bengaluru, Bangalore, Mumbai, Hyderabad, Pune, Chennai, Delhi/NCR, Kolkata, etc. — you no longer
  need to list every city. (US/UK/Germany/Singapore/Canada/Australia/UAE/Ireland also supported.)
- **Semantic title matching**: "product manager" now also matches "Group/Staff/Principal PM",
  "Product Lead", "Manager, Product", etc. (word-order independent + role aliases). Engineering and
  unrelated roles are still excluded. A "Strict title match" toggle restores exact-substring behavior.
- **Per-company funnel diagnostics**: every run now shows, per company, Fetched → ✗Title → ✗Location
  → ✗Stale → Kept, plus the actual locations its roles are in. A "0 kept" is never a mystery again.
- **Token hygiene**: tokens are de-duplicated case-insensitively ('Postman'=='postman'), space-broken
  tokens (e.g. 'Urban Company') are flagged instead of firing malformed requests, and SmartRecruiters/
  Workday case is preserved.
- Architecture note: the pre-filter is now lenient by design — borderline roles reach the embedding
  scorer, which ranks them into tiers (low scorers land in Tier C, never silently dropped).

## v1.8.1
- **FIX (the 401): "Test connection" now saves the key first.** The previous Test button read the
  key from disk, but the key was only written on "Save backend" — so testing before saving sent an
  empty key and Anthropic returned 401. Testing now persists the key first and also uses the
  just-entered key directly. This was the root cause of the Anthropic 401.
- **Empty-key guard**: instead of a raw 401, you now get "No API key configured" when no key is set,
  and a clear "Anthropic rejected the API key" message on a genuine 401.
- **API keys are trimmed** of stray whitespace/newlines on save and use (a common paste error).
- **Provider field is now the service name** (Anthropic, OpenAI, Google Gemini, Groq, Ollama, Custom).
  Selecting one auto-fills the endpoint and API style — no manual entry.
- **Model is now a dropdown** of the versions under the selected provider (e.g. Anthropic →
  Haiku / Sonnet / Opus), each with an editable price estimate.
- A "🔑 key saved" indicator shows whether a key is currently stored.

## v1.8.0
- **3 new portals**: Workable (public JSON), Personio (public XML), and Freshteam (best-effort
  no-auth HTML). Brings supported portals to 9. They appear automatically in Active Job Portals.
- **Bulk company → ATS finder**: paste a list of companies (pre-seeded with 19 Indian tech firms)
  and verify which portal each actually uses — probed live from your machine, so tokens are
  confirmed, not guessed. One click adds all confirmed tokens to your config.
- **Token finder now probes 9 portals** (adds Workable/Personio/Freshteam to the single search).
- Honest scope note: Darwinbox, iCIMS, Keka and Wellfound were evaluated and are NOT integrable
  as public portals (auth-only / partner-gated / anti-bot walled). Use them manually and add the
  underlying company's public ATS instead.

## v1.7.0
- **Anthropic Claude API support (native)**: added "Anthropic Claude Haiku" and "Sonnet"
  providers using the native /v1/messages endpoint with x-api-key headers — fixes the 401
  that happened when pointing the OpenAI path at api.anthropic.com.
- **API style selector**: choose openai-compatible vs anthropic-native (auto-set by presets);
  Custom can now target either correctly.
- **Token finder now covers SmartRecruiters + Workday**: probes CamelCase ids and a bounded
  set of Workday datacenters/sites; returns the full Workday URL when found.
- **Tier A/B/Freshness tooltips**: explain the 0–100 fit score and what each threshold means.
- **Per-run summary**: after a run, a banner shows roles matched THIS run vs cumulative totals
  (header metrics relabeled "Total jobs" to remove the confusion).
- **Location diagnostics**: when a location filter hides everything, the app shows the actual
  locations of your found roles and explains why, so zero-results is never a mystery.

## v1.6.0
- **Dynamic portal selection**: the Active Job Portals multiselect now adds/removes token
  boxes live (moved outside the form). Removing a portal removes its box.
- **Multi-select target seniority**: pick several levels (e.g. senior + staff + principal);
  scoring matches ANY selected level.
- **Stricter location filter (fix)**: setting "India" no longer leaks US roles. Remote roles
  pinned to another country (e.g. "Remote - US") are excluded; purely-remote roles still pass
  when "Include Remote" is on. Filter also applies to already-stored jobs immediately.
- **Live pipeline progress bar**: discovery + scoring show a real progress bar with stage labels.
- **Per-company discovery report (fix)**: after each run, see fetched/kept counts per company,
  so you know exactly why a company returned no jobs (wrong token, no title match, filtered out).

## v1.5.1
- **Platform renamed to Meridian** — all user-facing surfaces updated (app title, page config,
  README, updater). Internal module/folder names unchanged to preserve the update flow.

## v1.5.0
- **Analytics tab**: application funnel (applied → screen → interview → offer), conversion by
  Tier (validates scoring), by Variant (A/B), and by Portal. Directional, with sample-size caveats.
- **A/B testing**: tag each application as variant A or B in the Review Queue; compare reply rates.
- **Learning loop**: compares fit sub-scores of roles that replied vs. didn't, and suggests nudged
  scoring weights you can apply in one click. Conservative (needs >=3 per group); never auto-applies.
- **Interview prep notes**: per-role button generates likely questions + talking points from the JD.
- **Outreach drafts**: per-role button drafts a LinkedIn message and logs it to the outreach tracker.
- **Cross-board dedup**: the same role posted on multiple ATS boards is now collapsed to one.

## v1.4.0
- **Desired location filter**: set target locations + "Include Remote" toggle in Settings; jobs filtered on discovery.
- **Workday adapter** (6th portal): pulls from enterprise Workday boards (e.g. Salesforce) via a careers-URL token. Slower; per-job JD fetch.
- **Company token finder**: type a company name → probes Greenhouse/Lever/Ashby/Recruitee and offers a one-click "Add".
- **Threshold calibration**: score-distribution histogram + data-driven suggested Tier A/B cutoffs you can apply.
- **Application answers**: a standard-answer library (work auth, notice, salary...) shown per-role, plus AI/manual "why this company / first 90 days" drafts (copy-paste).
- **Active follow-ups**: header badge + a "Follow-ups due now" panel in the Tracker (applications + outreach), with snooze.
- **Outreach tracking**: log LinkedIn/email outreach per company with status + follow-up date, surfaced alongside applications.

## v1.3.0
- **Configurable portals**: Targets now has a portal multiselect (Greenhouse, Lever,
  Ashby, Recruitee, SmartRecruiters) with per-field ⓘ tooltips and an honest note on why
  aggregators (LinkedIn/Indeed) can't be added.
- **Real-time token & cost meter**: live session tokens in/out + cost, today-vs-cap,
  per-call breakdown, live updates during batch tailoring. (Discovery + scoring use 0 tokens.)
- **Manual-mode instructions**: a step-by-step popup (st.dialog) explaining the Claude Pro flow.
- Requires Streamlit >= 1.37 (for st.dialog).

## v1.2.0
- Comprehensive API tailoring MVP: structured JSON tailoring -> finished ATS .docx + cover
  letter, fact-integrity guard, provider presets, retries/backoff, daily cost cap, batch tailoring.

## v1.1.0
- In-app web UI for all configuration (targets, CV, backend) + "Run pipeline now" button.
- Switchable tailoring backend (manual Claude Pro / API).

## v1.0.0
- Core pipeline: discovery (Greenhouse/Lever/Ashby), local-embedding fit scoring,
  template tailoring, ATS-safe .docx render, SQLite tracker, Streamlit cockpit.
