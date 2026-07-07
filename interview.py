"""interview — the Interview Coach engine.

Pure state machine: rounds -> turns -> evaluation -> synthesis. No voice, no
Streamlit, no direct network calls — every LLM call is injected as `llm_fn`,
exactly like apply_prep.draft_answers, so the whole engine is testable with a
fake. Voice is a thin adapter that sits OUTSIDE this module entirely
(voice_io.py) and only ever hands it plain text + optional delivery metrics.

Session state is a plain JSON-serializable dict so it round-trips through
SQLite (db.py) without any custom (de)serialization.

Anti-sycophancy and realism guardrails live partly here, structurally, not
just in the prompt: the engine — not the LLM — enforces the follow-up budget
and the wall-clock wrap-up, so a misbehaving model reply can never turn a
45-minute round into an unbounded one.
"""

import datetime
import json
import os
import random

import fileio
import interview_prompts as ip

_HERE = os.path.dirname(__file__)
_CONTENT_DIR = os.path.join(_HERE, "interview_content")

_VALID_DECISIONS = {"probe", "challenge", "move_on", "wrap_up"}


class InterviewError(Exception):
    """Raised when the LLM reply is unusable even after defensive parsing."""


# ---------------------------------------------------------------- content ---
def load_rubrics():
    return fileio.read_yaml(os.path.join(_CONTENT_DIR, "rubrics.yaml"), default={})


def load_loops():
    return fileio.read_yaml(os.path.join(_CONTENT_DIR, "loops.yaml"), default={})


def load_question_bank():
    return fileio.read_yaml(os.path.join(_CONTENT_DIR, "question_bank.yaml"), default={})


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _parse_iso(s):
    return datetime.datetime.fromisoformat(s)


# ------------------------------------------------------------- validation ---
def validate_content():
    """Sanity-check the shipped YAML content is internally consistent. Called
    by tests and safe to call at app startup. Returns a list of problems
    (empty = clean)."""
    problems = []
    rubrics = load_rubrics()
    loops = load_loops()
    qbank = load_question_bank()

    round_types_in_rubrics = set(rubrics.get("rounds", {}).keys())
    for key, d in rubrics.get("rounds", {}).items():
        for dim in d.get("dimensions", []):
            if set(dim.get("anchors", {}).keys()) != {1, 2, 3, 4}:
                problems.append(f"rubric '{key}' dimension '{dim.get('key')}' "
                                f"must have anchors 1-4 exactly")

    persona_keys = set(loops.get("personas", {}).keys())
    for loop_key, loop in loops.get("loops", {}).items():
        for r in loop.get("rounds", []):
            if r["type"] not in round_types_in_rubrics:
                problems.append(f"loop '{loop_key}' references unknown round type '{r['type']}'")
            if r["persona"] not in persona_keys:
                problems.append(f"loop '{loop_key}' references unknown persona '{r['persona']}'")
            if r["probe_depth"] not in loops.get("probe_depth_settings", {}):
                problems.append(f"loop '{loop_key}' references unknown probe_depth "
                                f"'{r['probe_depth']}'")

    for rtype in round_types_in_rubrics:
        if rtype not in qbank.get("opening_questions", {}):
            problems.append(f"question bank missing opening_questions for round type '{rtype}'")

    return problems


# ---------------------------------------------------------------- session ---
def new_session(loop_key, job=None, resume_summary="", level="Senior PM",
                mode="realistic", input_mode="text", session_type="full_loop",
                drill_round_type=None, drill_minutes=12):
    """Create a new session dict. Does not call the LLM yet — start_round does.

    session_type: 'full_loop' | 'single_round' | 'drill'
    mode: 'realistic' | 'coach'
    input_mode: 'text' | 'voice'
    """
    loops = load_loops()
    if session_type == "drill":
        if not drill_round_type:
            raise InterviewError("drill session requires drill_round_type")
        rounds_def = [{"type": drill_round_type, "persona": "neutral",
                       "probe_depth": "standard", "minutes": drill_minutes}]
        loop_label = f"Drill: {drill_round_type}"
    else:
        loop = loops.get("loops", {}).get(loop_key)
        if not loop:
            raise InterviewError(f"unknown loop_key '{loop_key}'")
        rounds_def = loop["rounds"] if session_type == "full_loop" else loop["rounds"][:1]
        loop_label = loop["label"]

    rounds = []
    for r in rounds_def:
        rounds.append({
            "type": r["type"], "persona_key": r["persona"], "probe_depth": r["probe_depth"],
            "minutes": r["minutes"], "status": "pending", "transcript": [],
            "question": None, "probe_count": 0,
            "started_at": None, "paused_at": None, "paused_seconds": 0,
            "eval": None,
        })

    return {
        "loop_key": loop_key, "loop_label": loop_label, "session_type": session_type,
        "level": level, "mode": mode, "input_mode": input_mode,
        "job": job, "resume_summary": resume_summary,
        "rounds": rounds, "current_round_idx": 0,
        "status": "in_progress", "created_at": _now_iso(),
        "synthesis": None,
    }


def _current_round(session):
    return session["rounds"][session["current_round_idx"]]


def _job_line(job):
    if not job:
        return ""
    return f"{job.get('company','')} — {job.get('title','')}\n{(job.get('jd_text') or '')[:1200]}"


def _max_followups(session, round_):
    loops = load_loops()
    return loops["probe_depth_settings"][round_["probe_depth"]]["max_followups_per_question"]


def _persona(round_):
    loops = load_loops()
    return loops["personas"][round_["persona_key"]]


def _round_def(round_):
    rubrics = load_rubrics()
    return rubrics["rounds"][round_["type"]]


def _time_remaining_min(round_):
    if not round_["started_at"]:
        return round_["minutes"]
    elapsed = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
              - _parse_iso(round_["started_at"])).total_seconds() - round_["paused_seconds"]
    return max(0.0, round_["minutes"] - elapsed / 60.0)


def time_remaining_min(session):
    """Public helper for UIs: minutes left in the CURRENT round."""
    return _time_remaining_min(_current_round(session))


# ----------------------------------------------------------- pause/resume ---
def pause_round(session):
    r = _current_round(session)
    if r["status"] == "in_progress" and not r["paused_at"]:
        r["paused_at"] = _now_iso()
        r["status"] = "paused"
    return session


def resume_round(session):
    r = _current_round(session)
    if r["paused_at"]:
        gap = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
              - _parse_iso(r["paused_at"])).total_seconds()
        r["paused_seconds"] += gap
        r["paused_at"] = None
        r["status"] = "in_progress"
    return session


def abort_session(session):
    """Mark whatever happened so far as final, without pretending later rounds
    occurred. Pause/abort with dignity (discovery dossier G)."""
    session["status"] = "aborted"
    r = _current_round(session)
    if r["status"] in ("in_progress", "paused"):
        r["status"] = "aborted_partial"
    return session


# --------------------------------------------------------------- rounds ----
def start_round(session, llm_fn):
    """Generate (or fall back to a seed) opening question and open the round."""
    r = _current_round(session)
    rdef = _round_def(r)
    qbank = load_question_bank()
    seeds = qbank.get("opening_questions", {}).get(r["type"], [])

    question = None
    if llm_fn is not None:
        try:
            prompt = ip.build_opening_question_prompt(
                rdef, _job_line(session["job"]), session["resume_summary"], seeds)
            text, _usage = llm_fn(prompt)
            text = (text or "").strip().strip('"')
            if text:
                question = text
        except Exception:
            question = None  # fall through to seed fallback below

    if not question:
        if not seeds:
            raise InterviewError(f"no seed questions available for round type '{r['type']}'")
        question = random.choice(seeds)["text"]

    r["question"] = question
    r["transcript"] = [{"speaker": "interviewer", "text": question, "at": _now_iso()}]
    r["started_at"] = _now_iso()
    r["status"] = "in_progress"
    return session, question


def submit_answer(session, answer_text, llm_json_fn, delivery_metrics=None):
    """Advance the engine by one candidate turn. `llm_json_fn(prompt) -> (dict, usage)`
    must return parsed JSON (mirrors llm.chat_json's contract). Returns
    (session, interviewer_reply_text, round_finished: bool, usage).
    """
    r = _current_round(session)
    if r["status"] != "in_progress":
        raise InterviewError(f"round is not in_progress (status={r['status']})")

    turn = {"speaker": "candidate", "text": answer_text, "at": _now_iso()}
    if delivery_metrics:
        turn["delivery_metrics"] = delivery_metrics
    r["transcript"].append(turn)

    time_left = _time_remaining_min(r)
    max_fu = _max_followups(session, r)

    # Belt-and-braces: the wall clock wins even if the model wants to keep probing.
    if time_left <= 0:
        decision, reply = "wrap_up", "We're out of time for this round — let's stop there."
        usage = {"in": 0, "out": 0}
    else:
        rdef = _round_def(r)
        persona = _persona(r)
        qbank = load_question_bank()
        curveballs = qbank.get("curveballs", {}).get(r["type"], [])
        prompt = ip.build_interviewer_turn_prompt(
            rdef, persona, r["question"], r["transcript"][:-1], answer_text,
            r["probe_count"], max_fu, round(time_left, 1), curveballs,
            _job_line(session["job"]), session["resume_summary"])
        try:
            obj, usage = llm_json_fn(prompt)
        except Exception as e:
            raise InterviewError(f"interviewer turn failed: {e}") from e
        decision, reply = _extract_decision(obj, r["probe_count"], max_fu)

    if decision in ("probe", "challenge"):
        r["probe_count"] += 1
    elif decision == "move_on":
        r["probe_count"] = 0
        r["question"] = reply

    r["transcript"].append({"speaker": "interviewer", "text": reply, "at": _now_iso()})

    round_finished = decision == "wrap_up"
    if round_finished:
        r["status"] = "awaiting_eval"

    return session, reply, round_finished, usage


def _extract_decision(obj, probe_count, max_followups):
    """Defensively coerce a possibly-malformed LLM reply into a safe decision.
    Never lets the engine get stuck: unknown/missing decision -> move_on."""
    if not isinstance(obj, dict):
        return "move_on", "Let's move to the next question."
    decision = obj.get("decision")
    reply = obj.get("interviewer_says")
    if decision not in _VALID_DECISIONS or not isinstance(reply, str) or not reply.strip():
        return "move_on", (reply if isinstance(reply, str) and reply.strip()
                           else "Let's move to the next question.")
    if decision in ("probe", "challenge") and probe_count >= max_followups:
        return "move_on", reply  # engine overrides a model that won't stop probing
    return decision, reply.strip()


# ------------------------------------------------------------- evaluation ---
def evaluate_round(session, llm_json_fn):
    """Score the just-finished round. Returns (session, round_eval dict)."""
    r = _current_round(session)
    if r["status"] != "awaiting_eval":
        raise InterviewError(f"round not ready for evaluation (status={r['status']})")

    rubrics = load_rubrics()
    rubric = rubrics["rounds"][r["type"]]
    prompt = ip.build_evaluator_prompt(_round_def(r), rubric, r["transcript"], _job_line(session["job"]))
    try:
        obj, usage = llm_json_fn(prompt)
    except Exception as e:
        raise InterviewError(f"round evaluation failed: {e}") from e

    obj = _validate_eval(obj, rubric)
    obj["round_label"] = _round_def(r)["label"]
    r["eval"] = obj
    r["status"] = "completed"
    return session, obj, usage


def _validate_eval(obj, rubric):
    """Guard against a malformed evaluator reply: every rubric dimension must
    be present with an int score 1-4 and a non-empty evidence quote, or the
    dimension is marked unscored rather than silently defaulting to a flattering
    number."""
    if not isinstance(obj, dict):
        obj = {}
    scores = obj.get("dimension_scores") if isinstance(obj.get("dimension_scores"), dict) else {}
    clean = {}
    for d in rubric["dimensions"]:
        k = d["key"]
        v = scores.get(k)
        if isinstance(v, dict) and isinstance(v.get("score"), (int, float)) and 1 <= v["score"] <= 4 \
           and isinstance(v.get("evidence_quote"), str) and v["evidence_quote"].strip():
            clean[k] = {"score": int(v["score"]), "evidence_quote": v["evidence_quote"].strip(),
                       "justification": str(v.get("justification", "")).strip()}
        else:
            clean[k] = {"score": None, "evidence_quote": "", "justification": "not scorable — "
                       "evaluator did not provide a valid evidence-backed score"}
    verdict = obj.get("hire_bar_verdict")
    if verdict not in ("strong_hire", "hire", "no_hire", "strong_no_hire"):
        verdict = "no_hire"  # never default to a flattering unearned verdict
    return {
        "dimension_scores": clean,
        "hire_bar_verdict": verdict,
        "verdict_reason": str(obj.get("verdict_reason", "")).strip(),
        "round_strengths": [s for s in (obj.get("round_strengths") or []) if isinstance(s, str)][:3],
        "round_weaknesses": [s for s in (obj.get("round_weaknesses") or []) if isinstance(s, str)][:3],
    }


def advance_or_finish(session):
    """Move to the next round, or mark the whole session completed."""
    if session["current_round_idx"] + 1 < len(session["rounds"]):
        session["current_round_idx"] += 1
        return session, False
    session["status"] = "completed"
    return session, True


def synthesize_session(session, llm_json_fn):
    """Roll up every round's evaluation into a prioritized final report."""
    evals = [r["eval"] for r in session["rounds"] if r["eval"]]
    if not evals:
        raise InterviewError("no evaluated rounds to synthesize")
    prompt = ip.build_session_synthesis_prompt(evals, session["level"])
    try:
        obj, usage = llm_json_fn(prompt)
    except Exception as e:
        raise InterviewError(f"session synthesis failed: {e}") from e
    if not isinstance(obj, dict):
        obj = {}
    synthesis = {
        "top_strengths": [s for s in (obj.get("top_strengths") or []) if isinstance(s, str)][:2],
        "top_fixes": [s for s in (obj.get("top_fixes") or []) if isinstance(s, str)][:3],
        "overall_summary": str(obj.get("overall_summary", "")).strip(),
        "recommended_next_drill": str(obj.get("recommended_next_drill", "")).strip(),
    }
    session["synthesis"] = synthesis
    return session, synthesis, usage
