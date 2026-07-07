"""interview_prompts — every prompt the Interview Coach sends to the LLM.

Three distinct calls, deliberately kept separate (never merged into one):
  1. interviewer turn   — classify the last answer + decide + speak the next thing
  2. round evaluator     — score a completed round against its rubric, evidence-quoted
  3. session synthesis   — top strengths/fixes across a whole session

Anti-sycophancy is structural, not a hope: the interviewer prompt forbids
praise and reassurance outright (discovery dossier B), and the evaluator
prompt REQUIRES a verbatim transcript citation for every score, borrowing the
copyright discipline already used elsewhere in this app (short quotes,
attributed) so feedback can never be unfalsifiable.
"""

import json

_ANTI_SYCOPHANCY = (
    "You are conducting a real interview, not coaching. NEVER praise the answer, "
    "never say things like 'great answer' or 'that's interesting', and never "
    "reassure the candidate about how they're doing. Acknowledge only neutrally "
    "('Okay.', 'Understood.', 'Go on.', 'Noted.') before moving to your next line. "
    "Do not summarize or repeat back what the candidate said before it's necessary."
)


def build_interviewer_turn_prompt(round_def, persona, question, transcript_so_far,
                                  candidate_answer, probe_count, max_followups,
                                  time_remaining_min, curveballs, job, resume_summary):
    """One call that classifies the last answer AND decides AND speaks the next
    line — kept as a single call deliberately, for both latency and cost.
    Returns a prompt whose JSON reply has keys: classification, decision,
    interviewer_says. `decision` is one of probe|challenge|move_on|wrap_up.
    """
    history = "\n".join(f"{t['speaker'].upper()}: {t['text']}" for t in transcript_so_far[-12:])
    curveball_block = "\n".join(f"- {c}" for c in curveballs) or "(none configured for this round)"

    return f"""{_ANTI_SYCOPHANCY}

You are role-playing a {persona['label']} in a Product Management interview.
Style: {persona['style'].strip()}

ROUND: {round_def['label']}
CURRENT QUESTION: {question}
TIME REMAINING IN ROUND: about {time_remaining_min} minutes
FOLLOW-UPS ASKED ON THIS QUESTION SO FAR: {probe_count} (max {max_followups} before you must move on)

{"JOB CONTEXT: " + job if job else "No specific job selected — this is a generic practice question."}
{"CANDIDATE BACKGROUND (for grounding behavioral/CV questions): " + resume_summary if resume_summary else ""}

CONVERSATION SO FAR
{history or "(nothing yet — this is the opening question)"}

CANDIDATE'S LATEST ANSWER
{candidate_answer}

Decide what a real interviewer would do next. Options:
- "probe": push on the single weakest claim or missing piece of evidence in the answer.
  {question_bank_note()}
- "challenge": inject a curveball constraint from this list (pick the most relevant, or
  write a comparable one in the same spirit):
{curveball_block}
- "move_on": the answer sufficiently addressed the question and follow-up budget — move
  to a genuinely new question within this round.
- "wrap_up": time remaining is short enough that the round should close now.

Rules: never choose "probe" more than {max_followups} times on the same question. If
follow-ups are exhausted, choose "move_on" or "wrap_up". If time remaining is under
3 minutes, prefer "wrap_up".

Return ONLY valid JSON, no markdown fences:
{{"classification": {{"claims": ["..."], "evidence_given": true/false, "gaps": ["..."]}},
  "decision": "probe|challenge|move_on|wrap_up",
  "interviewer_says": "the exact next thing the interviewer says out loud, in character"}}

Contract for interviewer_says depending on decision:
- probe/challenge: the follow-up question itself, nothing else.
- move_on: a brief neutral transition PLUS a genuinely new question in this round (never
  repeat the current question).
- wrap_up: a short close-out line for this round only (no new question).
"""


def question_bank_note():
    return ("Escalate through stages: exact metric/number -> how they verified it -> "
            "a counterfactual -> what they'd do differently. Don't repeat a stage already used.")


def build_opening_question_prompt(round_def, job, resume_summary, seed_questions):
    """Generate a fresh, JD/CV-grounded opening question for this round, falling
    back in spirit (not literally) on the seed bank. This is the moat: a
    question themed on the candidate's actual target job and real background,
    never invented facts about the candidate — only the QUESTION is generated,
    never a claim about them."""
    seeds = "\n".join(f"- {q['text']}" for q in seed_questions)
    context = (f"JOB: {job}" if job else "No specific job — write a strong generic question.")
    cv_line = f"\nCANDIDATE BACKGROUND: {resume_summary}" if resume_summary else ""
    return f"""Write ONE opening interview question for a "{round_def['label']}" round.
{context}{cv_line}

Style examples from real loops (write a NEW question in this spirit — do not copy one
verbatim unless it already fits perfectly):
{seeds}

If a job is given, theme the question around that company's actual domain/product.
If candidate background is given and this is a behavioral round, you may reference their
real resume claims (e.g. "your background mentions X — tell me about...") — never invent
achievements they didn't state.

Return ONLY the question text, nothing else — no quotes, no preamble."""


def build_evaluator_prompt(round_def, rubric, transcript, job):
    """Score a completed round. Every dimension score MUST cite a short, verbatim
    line from the transcript — unfalsifiable feedback is not accepted."""
    convo = "\n".join(f"{t['speaker'].upper()}: {t['text']}" for t in transcript)
    dims = []
    for d in rubric["dimensions"]:
        anchors = "; ".join(f"{k}={v}" for k, v in sorted(d["anchors"].items()))
        dims.append(f'- {d["key"]} ("{d["label"]}"): anchors {anchors}')
    dims_block = "\n".join(dims)

    return f"""You are scoring a completed PM interview round as an experienced, calibrated
hiring panel would — not a coach. Be exacting. Most real candidates do NOT hit anchor 4
on most dimensions; reserve 4 for genuinely exceptional moments.

ROUND: {round_def['label']}
{"JOB CONTEXT: " + job if job else ""}

RUBRIC DIMENSIONS (score each 1-4 against these anchors)
{dims_block}

FULL TRANSCRIPT
{convo}

For EVERY dimension, you MUST quote a short (under 15 words) verbatim line from the
CANDIDATE's own turns above that justifies the score. If no line supports a score above 1,
score it 1 and say so plainly.

Also give an overall hire-bar verdict: one of "strong_hire", "hire", "no_hire",
"strong_no_hire", plus the single reason that drove it most.

Return ONLY valid JSON, no markdown fences:
{{"dimension_scores": {{"<dim_key>": {{"score": 1-4, "evidence_quote": "...", "justification": "..."}}, ...}},
  "hire_bar_verdict": "strong_hire|hire|no_hire|strong_no_hire",
  "verdict_reason": "...",
  "round_strengths": ["...", "..."],
  "round_weaknesses": ["...", "..."]}}
"""


def build_session_synthesis_prompt(round_evals, level):
    """Roll up all rounds in a session into a prioritized, non-overwhelming
    summary — top 2 strengths, top 3 fixes ranked by score impact, not 15 notes."""
    blocks = []
    for i, ev in enumerate(round_evals, 1):
        dims = ", ".join(f"{k}={v['score']}" for k, v in ev["dimension_scores"].items())
        blocks.append(f"Round {i} ({ev.get('round_label','')}): verdict={ev['hire_bar_verdict']} "
                      f"| {dims} | weaknesses: {', '.join(ev.get('round_weaknesses', []))}")
    rounds_block = "\n".join(blocks)

    return f"""Synthesize this candidate's full interview session, targeting a {level} level bar.
Do not list every note from every round — prioritize.

ROUND RESULTS
{rounds_block}

Return ONLY valid JSON, no markdown fences:
{{"top_strengths": ["at most 2, the ones most worth leaning on"],
  "top_fixes": ["at most 3, ranked by how much they'd move the overall bar"],
  "overall_summary": "3-4 sentences, direct and specific, no generic encouragement",
  "recommended_next_drill": "one specific drill to practice next, and why"}}
"""


def build_model_answer_prompt(question, weak_answer, resume_summary, round_def):
    """A strong answer to the SAME question, built only from the candidate's own
    real background — reuses the fact-integrity principle from CV tailoring:
    reshape their real experience, never invent achievements they don't have."""
    return f"""Write a strong model answer to the interview question below, for a
"{round_def['label']}" round — but built ONLY from this candidate's real background.
Never invent an employer, metric, or achievement they haven't stated. If their real
background doesn't give you enough to fully answer, say so honestly within the answer
rather than fabricating specifics.

QUESTION: {question}

CANDIDATE'S REAL BACKGROUND
{resume_summary}

THE CANDIDATE'S OWN (WEAKER) ANSWER, FOR CONTRAST
{weak_answer}

Return ONLY the model answer text, 150-250 words, in first person."""
