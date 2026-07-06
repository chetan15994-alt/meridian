# Meridian

A local, zero-API-token job-application copilot. It discovers fresh roles from public
ATS job boards, scores each one against your resume using a **local embedding model**
(no LLM tokens, runs offline), tailors your resume, ATS-proofs it, and tracks everything.

The only "premium" model used is **your Claude Pro** — manually, via copy-paste, and
only for the handful of top-fit (Tier A) roles. Submission stays manual by design
(quality + account safety).

Built to stand up in 1–2 days. Cross-platform (macOS + Windows).

---

## What it does (and what it deliberately doesn't)

| Stage | How | Tokens? |
|---|---|---|
| Discover jobs | Public Greenhouse / Lever / Ashby board endpoints (no auth) | none |
| Score fit | Local `sentence-transformers` embeddings + keyword + skills + seniority | none |
| ATS keyword gaps | Mini-KeyBERT built on the local model | none |
| Tailor (Tier A) | Generates a prompt you paste into **Claude Pro** | your Pro plan |
| Tailor (Tier B) | Template: picks your most relevant REAL bullets, no LLM | none |
| ATS-proof resume | Single-column `.docx`, parse-back validated | none |
| Track + follow-up | SQLite + Streamlit cockpit | none |
| **Submit** | **You click submit** (not automated — on purpose) | n/a |

It never invents experience: the tailoring engine only reorders/rephrases facts in
`resume_master.yaml`.

---

## Setup (5 minutes)

```bash
# 1. create + activate a virtual environment
python -m venv venv
# macOS / Linux:
source venv/bin/activate
# Windows (PowerShell):
venv\Scripts\Activate.ps1

# 2. install dependencies
pip install -r requirements.txt
```

Then edit two files:

1. **`config.yaml`** — replace the example company tokens with real targets.
   Find a token from the careers URL:
   `boards.greenhouse.io/<TOKEN>`, `jobs.lever.co/<TOKEN>`, `jobs.ashbyhq.com/<TOKEN>`.
   (Wrong tokens just 404 and are skipped.)
2. **`resume_master.yaml`** — fill in the `[fill in: ...]` markers with your real numbers.

One-time Claude Pro setup (for Tier-A tailoring): in claude.ai create a **Project**
called "Resume Tailoring", and add `resume_master.yaml` as project knowledge. Then each
Tier-A prompt the tool gives you can be pasted straight in.

---

## Daily use

```bash
# run the pipeline (first run downloads the embedding model ~80MB, once)
python run.py

# open the review cockpit
streamlit run app.py
```

In the cockpit:
- **Tier A** (top fit): click *Claude prompt* → paste into your Claude Pro Project →
  paste the tailored result back → save as your resume for that role.
- **Tier B** (good fit): click *Build tailored resume* → download the ATS `.docx`.
- Apply on the company site yourself, then click *Mark applied* (sets a day-7 follow-up).
- Track status in the **Application Tracker** tab.

> **First-run calibration:** look at the spread of fit scores on your real targets and
> adjust `tiers:` and `weights:` in `config.yaml`. Scores are relative — tune once and
> they'll be stable. `priority_companies` always force Tier A regardless of score.

---

## Run it automatically, twice a day

**macOS / Linux (cron):**
```bash
crontab -e
# add (runs 9am & 2pm):
0 9,14 * * * cd /full/path/to/jobcopilot && ./venv/bin/python run.py >> outputs/run.log 2>&1
```

**Windows (Task Scheduler):**
Create `run.bat` in the project folder:
```bat
@echo off
cd /d %~dp0
venv\Scripts\python.exe run.py >> outputs\run.log 2>&1
```
Then Task Scheduler → Create Task → Triggers: daily at 9:00 and 14:00 → Action: start
`run.bat`. (Use `pythonw.exe` if you want it to run with no console window.)

---

## File map

```
config.yaml         target companies, role keywords, weights, tier thresholds
resume_master.yaml  your resume as structured data (single source of truth)
ingest.py           public ATS board fetchers (Greenhouse / Lever / Ashby)
score.py            local-embedding fit scoring + keyword/skill/seniority sub-scores
tailor.py           Tier-A Claude prompt builder + Tier-B template tailoring
render.py           ATS-safe .docx renderer + parse-back check
db.py               SQLite storage
run.py              orchestrator — run 2x/day
app.py              Streamlit review cockpit + application tracker
```

## Notes & honest limits
- **ATS coverage:** great for startups/mid-size on Greenhouse/Lever/Ashby; big-enterprise
  Workday/custom portals you'll add manually.
- **Scores are heuristic**, not ground truth — they rank and triage, you decide.
- **No automated submission / no LinkedIn automation** — deliberate, to protect your
  accounts and keep application quality high.
- **Upgrade idea:** swap `embedding_model` to `BAAI/bge-small-en-v1.5` for a quality bump
  (slightly larger; remember bge benefits from a query prefix if you customize).

---

---

## v2 — Meridian web app (API tailoring MVP)

Run everything from the browser — no YAML editing, no terminal:

```bash
streamlit run app.py
```

Browser opens at `http://localhost:8501`.

**Tabs**
- **📋 Review Queue** — ranked roles with sub-scores; one-click tailoring; batch "Tailor all Tier A"; mark-applied with auto day-7 follow-up.
- **⚙ Settings** — target companies, roles, location, tiers, full CV editor (incl. per-experience bullets), and the tailoring backend.
- **📊 Tracker** — application pipeline (applied → screen → interview → offer).

**One-click tailoring (API mode)**
Pick a provider in Settings → 🧠 (preset auto-fills URL + model), paste an API key, Test connection, Save. Then in the Review Queue, **✍ Tailor (1-click)** produces a finished, ATS-safe **.docx + cover letter**, fact-checked against your master resume.

| Provider | Set in Settings | Cost (≈/role) | Notes |
|---|---|---|---|
| OpenAI `gpt-4o-mini` | preset | ~$0.001 | reliable default |
| Gemini 2.0 Flash | preset | ~$0.0008 | cheap; has a free tier |
| Groq Llama-3.1-8B | preset | ~$0.0003 | very fast |
| Local Ollama | preset (no key) | $0 | offline; needs Ollama running |

**Safety & robustness built in**
- **Fact integrity:** roles, dates, employers, education and contact info are ALWAYS taken from your master resume — the model can only reorder/rephrase summary, skills, and bullets. It cannot invent or alter facts.
- **Hard daily cost cap** (Settings): API tailoring stops once the day's spend hits your cap. Real usage is read from each API response, not guessed.
- **Retries + backoff** on transient/429 errors; **graceful fallback** to the no-LLM template `.docx` if the API fails.
- **API key** stored locally in `secrets.yaml` (gitignored), never in config.

**Recommended first session**
1. Settings → add 5–10 target company tokens, save.
2. Run pipeline now → review the Tier A/B ranking; tune thresholds if needed.
3. Settings → 🧠 → pick a provider + key (set a $1 cap) → Test connection.
4. Review Queue → ✍ Tailor a couple of Tier A roles → sanity-check the output → apply → Mark applied.
