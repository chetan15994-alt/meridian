"""apply_prep: schema parsing, field classification, graceful degradation,
answer drafting (key filtering + bool→option mapping), URL quoting."""
import apply_prep as ap

FAKE_GH = {
    "questions": [
        {"label": "First Name", "required": True,
         "fields": [{"name": "first_name", "type": "input_text"}]},
        {"label": "Resume", "required": True,
         "fields": [{"name": "resume", "type": "input_file"}]},
        {"label": "Why Acme?", "required": True,
         "fields": [{"name": "question_111", "type": "textarea"}]},
        {"label": "Years of PM experience", "required": True,
         "fields": [{"name": "question_222", "type": "multi_value_single_select",
                     "values": [{"value": 1, "label": "3-5"}, {"value": 2, "label": "6-9"}]}]},
        {"label": "Gender", "required": False,
         "fields": [{"name": "gender", "type": "multi_value_single_select"}]},
    ],
}
GH_JOB = {"source": "greenhouse", "company": "acme", "title": "Senior PM",
          "url": "https://boards.greenhouse.io/acme/jobs/4567", "jd_text": "x"}


def test_greenhouse_schema_and_classification():
    sc = ap.fetch_schema(GH_JOB, fetcher=lambda url: FAKE_GH)
    assert sc["schema_source"] == "greenhouse"
    kinds = {q["key"]: q["kind"] for q in sc["questions"]}
    assert kinds["first_name"] == "identity"
    assert kinds["resume"] == "document"
    assert kinds["gender"] == "eeo"
    draftable = {q["key"] for q in ap.draftable_questions(sc["questions"])}
    assert draftable == {"question_111", "question_222"}   # never identity/doc/eeo


def test_network_failure_degrades_never_raises():
    def boom(url): raise RuntimeError("offline")
    sc = ap.fetch_schema(GH_JOB, fetcher=boom)
    assert sc["schema_source"] == "generic" and sc["note"]


def test_lever_and_generic_paths():
    lv = ap.fetch_schema({"source": "lever", "company": "globex",
                          "url": "https://jobs.lever.co/globex/abc-123", "jd_text": "x"})
    assert lv["schema_source"] == "lever_standard"
    assert lv["apply_url"].endswith("/abc-123/apply")
    gen = ap.fetch_schema({"source": "jsearch", "company": "W", "url": "u", "jd_text": "y"})
    assert gen["schema_source"] == "generic"


def test_greenhouse_url_is_quoted():
    seen = {}
    def spy(url): seen["url"] = url; return FAKE_GH
    job = dict(GH_JOB, company="ac me/../x")   # hostile-shaped token
    ap.fetch_schema(job, fetcher=spy)
    assert "/boards/ac%20me%2F..%2Fx/" in seen["url"]   # no path escape possible


def test_draft_answers_filters_keys_and_maps_bools():
    qs = [{"key": "q1", "label": "Authorized?", "type": "boolean", "required": True,
           "options": ["Yes", "No"], "kind": "choice"},
          {"key": "q2", "label": "Why?", "type": "textarea", "required": True,
           "options": [], "kind": "freetext"}]
    fake = lambda *a, **k: ({"answers": {"q1": True, "q2": "Because.",
                                         "EVIL": "drop me", "q3": ["not scalar"]}},
                            {"in": 1, "out": 1})
    ans, usage = ap.draft_answers({"company": "x", "title": "y", "jd_text": ""},
                                  qs, {"tailoring": {}}, llm_fn=fake)
    assert ans == {"q1": "Yes", "q2": "Because."}    # bool→option, bad keys dropped
    assert usage == {"in": 1, "out": 1}
