"""jobimport: deterministic parsing of pasted LinkedIn / generic postings."""
import jobimport as ji


LINKEDIN_PASTE = """\
Senior Product Manager, AI Platform
Acme Corp \u00b7 Bengaluru, Karnataka, India \u00b7 2 days ago \u00b7 47 applicants
Promoted
On-site
Easy Apply
Save
About the job
We are looking for a Senior Product Manager to lead our AI platform.
You will own the roadmap, work with engineering, and drive GTM.
Requirements:
- 6+ years of product management experience
- Experience with B2B SaaS and AI/ML products
Show more
"""


def test_linkedin_paste_extracts_all_required_fields():
    r = ji.parse_job_text(LINKEDIN_PASTE)
    assert r["title"] == "Senior Product Manager, AI Platform"
    assert r["company"] == "Acme Corp"
    assert "Bengaluru" in r["location"]
    assert "roadmap" in r["jd_text"].lower()
    assert r.missing_required == []


def test_source_guess_linkedin_when_url_present():
    r = ji.parse_job_text(LINKEDIN_PASTE + "\nhttps://www.linkedin.com/jobs/view/123/")
    assert r.source_guess == "linkedin"


def test_chrome_lines_stripped_from_jd():
    r = ji.parse_job_text(LINKEDIN_PASTE)
    for junk in ("Easy Apply", "Promoted", "Show more", "Save"):
        assert junk not in r["jd_text"]


def test_url_detected_when_present():
    txt = LINKEDIN_PASTE + "\nhttps://www.linkedin.com/jobs/view/1234567890/"
    r = ji.parse_job_text(txt)
    assert r["url"] == "https://www.linkedin.com/jobs/view/1234567890/"


def test_generic_paste_without_headers_still_usable():
    txt = ("Staff Engineer\nGlobex Inc\nRemote\n"
           "We build distributed systems at scale. You will design services, "
           "mentor engineers, and improve reliability across the platform stack.")
    r = ji.parse_job_text(txt)
    assert r["title"] == "Staff Engineer"
    assert r["company"] == "Globex Inc"
    assert "distributed systems" in r["jd_text"].lower()


def test_empty_and_garbage_never_crash():
    assert ji.parse_job_text("").missing_required  # empty -> needs review, no crash
    assert ji.parse_job_text(None)["jd_text"] == ""
    r = ji.parse_job_text("\n\n   \n")
    assert r["confidence"] == 0.0


def test_needs_review_flag_logic():
    good = ji.parse_job_text(LINKEDIN_PASTE)
    assert good.needs_review is False
    bad = ji.parse_job_text("just one line")
    assert bad.needs_review is True


def test_nav_chrome_and_save_button_stripped():
    """LinkedIn pastes often start with 'Skip to…' nav and include a
    'Save <title> at <company>' button — neither should become the title or
    leak into the JD."""
    paste = ("Skip to search Skip to main content\n"
             "Lead Product Manager - Payments\n"
             "Razorpay \u00b7 Bengaluru, Karnataka, India (Hybrid) \u00b7 3 days ago\n"
             "Hybrid\nEasy Apply\nSave Lead Product Manager - Payments at Razorpay\n"
             "About the job\n"
             "Razorpay is building the financial backbone. You will own the payments "
             "roadmap and drive metrics across the org for many teams.\n"
             "Set alert for similar jobs")
    r = ji.parse_job_text(paste)
    assert r["title"] == "Lead Product Manager - Payments"
    assert r["company"] == "Razorpay"
    assert "Skip to" not in r["jd_text"]
    assert "Save Lead" not in r["jd_text"]
    assert "Set alert" not in r["jd_text"]
    assert r.missing_required == []


def test_merge_llm_overlays_only_nonblank():
    det = ji.parse_job_text("Some Title\n\nshort body that is quite long indeed to pass length checks here ok")
    merged = ji.merge_llm(det, {"company": "Recovered Co", "title": "", "location": "Pune"})
    assert merged["company"] == "Recovered Co"      # filled from LLM
    assert merged["title"] == det["title"]           # LLM blank -> deterministic kept
    assert merged["location"] == "Pune"


def test_merge_llm_handles_bad_input():
    det = ji.parse_job_text(LINKEDIN_PASTE)
    assert ji.merge_llm(det, None)["company"] == det["company"]   # no crash
    assert ji.merge_llm(det, "not a dict")["title"] == det["title"]
