"""interview: engine state machine, defensive parsing, timing, evaluation."""
import datetime

import pytest

import interview as iv


def test_content_validates_clean():
    assert iv.validate_content() == []


def test_new_session_generic_loop_has_four_rounds():
    sess = iv.new_session(loop_key="generic", resume_summary="Senior PM")
    assert [r["type"] for r in sess["rounds"]] == ["product_sense", "execution", "rca", "behavioral"]
    assert sess["status"] == "in_progress"


def test_new_session_unknown_loop_raises():
    with pytest.raises(iv.InterviewError):
        iv.new_session(loop_key="not_a_real_loop")


def test_new_session_drill_requires_round_type():
    with pytest.raises(iv.InterviewError):
        iv.new_session(loop_key="generic", session_type="drill")


def test_start_round_falls_back_to_seed_without_llm():
    sess = iv.new_session(loop_key="generic")
    sess, q = iv.start_round(sess, llm_fn=None)
    assert q and sess["rounds"][0]["status"] == "in_progress"
    assert sess["rounds"][0]["transcript"][0]["text"] == q


def test_probe_then_move_on_flow():
    sess = iv.new_session(loop_key="generic")
    sess, _ = iv.start_round(sess, llm_fn=None)
    calls = {"n": 0}

    def fake(prompt):
        calls["n"] += 1
        if calls["n"] <= 2:
            return ({"decision": "probe", "interviewer_says": "Prove it."}, {"in": 1, "out": 1})
        return ({"decision": "move_on", "interviewer_says": "New question: X?"}, {"in": 1, "out": 1})

    sess, r1, done1, _ = iv.submit_answer(sess, "ans1", fake)
    assert not done1 and sess["rounds"][0]["probe_count"] == 1
    sess, r2, done2, _ = iv.submit_answer(sess, "ans2", fake)
    assert not done2 and sess["rounds"][0]["probe_count"] == 2
    sess, r3, done3, _ = iv.submit_answer(sess, "ans3", fake)
    assert not done3 and sess["rounds"][0]["probe_count"] == 0
    assert sess["rounds"][0]["question"] == r3


def test_engine_enforces_followup_budget_even_if_llm_keeps_probing():
    sess = iv.new_session(loop_key="india_startup")  # rca round, probe_depth=deep -> max 4
    sess, _ = iv.start_round(sess, llm_fn=None)

    def always_probe(prompt):
        return ({"decision": "probe", "interviewer_says": "Prove it."}, {"in": 1, "out": 1})

    for _ in range(6):
        sess, _, _, _ = iv.submit_answer(sess, "answer", always_probe)
    assert sess["rounds"][0]["probe_count"] == 0  # engine forced a reset via move_on


def test_malformed_llm_reply_never_crashes():
    sess = iv.new_session(loop_key="generic")
    sess, _ = iv.start_round(sess, llm_fn=None)

    def garbage(prompt):
        return ({"oops": "no decision"}, {"in": 1, "out": 1})

    sess, reply, done, _ = iv.submit_answer(sess, "answer", garbage)
    assert reply and not done

    def non_dict(prompt):
        return ("not a dict", {"in": 1, "out": 1})

    sess, reply2, done2, _ = iv.submit_answer(sess, "answer2", non_dict)
    assert reply2 and not done2


def test_wall_clock_forces_wrapup_without_calling_llm():
    sess = iv.new_session(loop_key="generic")
    sess, _ = iv.start_round(sess, llm_fn=None)
    past = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
           - datetime.timedelta(minutes=999)).isoformat(timespec="seconds")
    sess["rounds"][0]["started_at"] = past
    calls = {"n": 0}

    def should_not_run(prompt):
        calls["n"] += 1
        return ({"decision": "probe", "interviewer_says": "..."}, {"in": 1, "out": 1})

    sess, reply, done, usage = iv.submit_answer(sess, "answer", should_not_run)
    assert done and calls["n"] == 0
    assert usage == {"in": 0, "out": 0}


def test_submit_answer_on_non_active_round_raises():
    sess = iv.new_session(loop_key="generic")
    with pytest.raises(iv.InterviewError):
        iv.submit_answer(sess, "answer", lambda p: ({}, {}))  # round is "pending", not started


def test_evaluate_round_rejects_unevidenced_scores():
    sess = iv.new_session(loop_key="generic")
    sess, _ = iv.start_round(sess, llm_fn=None)
    sess["rounds"][0]["status"] = "awaiting_eval"

    def bad_eval(prompt):
        return ({"dimension_scores": {"user_empathy": {"score": 4}},  # no evidence_quote
                 "hire_bar_verdict": "amazing_hire"}, {"in": 1, "out": 1})

    sess, ev, _ = iv.evaluate_round(sess, bad_eval)
    assert ev["hire_bar_verdict"] == "no_hire"  # invalid verdict never defaults flattering
    assert ev["dimension_scores"]["user_empathy"]["score"] is None
    assert ev["dimension_scores"]["strategic_awareness"]["score"] is None  # missing entirely


def test_evaluate_round_accepts_well_evidenced_scores():
    sess = iv.new_session(loop_key="generic")
    sess, _ = iv.start_round(sess, llm_fn=None)
    sess["rounds"][0]["status"] = "awaiting_eval"
    dims = ["user_empathy", "structure", "product_taste", "strategic_awareness", "communication"]

    def good_eval(prompt):
        return ({"dimension_scores": {k: {"score": 3, "evidence_quote": "said X", "justification": "ok"}
                                      for k in dims},
                 "hire_bar_verdict": "hire", "verdict_reason": "solid",
                 "round_strengths": ["clear"], "round_weaknesses": ["thin metrics"]}, {"in": 1, "out": 1})

    sess, ev, _ = iv.evaluate_round(sess, good_eval)
    assert all(v["score"] == 3 for v in ev["dimension_scores"].values())
    assert ev["hire_bar_verdict"] == "hire"


def test_pause_resume_excludes_paused_time():
    import time
    sess = iv.new_session(loop_key="generic")
    sess, _ = iv.start_round(sess, llm_fn=None)
    before = iv._time_remaining_min(sess["rounds"][0])
    sess = iv.pause_round(sess)
    time.sleep(1.1)
    sess = iv.resume_round(sess)
    after = iv._time_remaining_min(sess["rounds"][0])
    assert abs(before - after) < 0.05


def test_abort_marks_partial_not_completed():
    sess = iv.new_session(loop_key="generic")
    sess, _ = iv.start_round(sess, llm_fn=None)
    sess = iv.abort_session(sess)
    assert sess["status"] == "aborted"
    assert sess["rounds"][0]["status"] == "aborted_partial"


def test_full_session_to_synthesis():
    sess = iv.new_session(loop_key="amazon_style")  # 2 rounds

    def instant_wrap(prompt):
        return ({"decision": "wrap_up", "interviewer_says": "Stop there."}, {"in": 1, "out": 1})

    def flat_eval(prompt):
        dims = ["ownership", "evidence", "conflict_handling", "introspection", "communication"]
        return ({"dimension_scores": {k: {"score": 2, "evidence_quote": "x", "justification": "y"} for k in dims},
                 "hire_bar_verdict": "hire", "verdict_reason": "avg",
                 "round_strengths": ["ownership"], "round_weaknesses": ["thin evidence"]}, {"in": 1, "out": 1})

    def synth(prompt):
        return ({"top_strengths": ["ownership"], "top_fixes": ["sharper numbers"],
                 "overall_summary": "ok", "recommended_next_drill": "story stress-test"}, {"in": 1, "out": 1})

    finished = False
    for _ in range(len(sess["rounds"])):
        sess, _ = iv.start_round(sess, llm_fn=None)
        sess, _, done, _ = iv.submit_answer(sess, "answer", instant_wrap)
        assert done
        sess, _, _ = iv.evaluate_round(sess, flat_eval)
        sess, finished = iv.advance_or_finish(sess)
    assert finished and sess["status"] == "completed"
    sess, syn, _ = iv.synthesize_session(sess, synth)
    assert syn["top_strengths"] == ["ownership"]


def test_synthesize_with_no_evaluated_rounds_raises():
    sess = iv.new_session(loop_key="generic")
    with pytest.raises(iv.InterviewError):
        iv.synthesize_session(sess, lambda p: ({}, {}))
