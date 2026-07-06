"""Meridian — AI-powered job search copilot.  Run:  streamlit run app.py"""
import json, datetime, os
import streamlit as st
import db, tailor, render, settings, llm, ingest, analytics, connectors, score, ui, run as runner, apply_prep

HERE = os.path.dirname(__file__)
st.set_page_config(page_title="Meridian", page_icon="", layout="wide")
db.init_db()

@st.cache_resource
def get_model():
    cfg = settings.load_config()
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(cfg.get("embedding_model","sentence-transformers/all-MiniLM-L6-v2"))

def loads(x):
    try: return json.loads(x) if x else []
    except Exception: return []
def price_for(cfg):
    tc=cfg.get("tailoring",{})
    if isinstance(tc.get("price"),dict): return tc["price"]
    return llm.provider_price(tc.get("provider",""), tc.get("model",""))
def sess(): return st.session_state.setdefault("usage", {"in":0,"out":0,"cost":0.0,"calls":0})
def acc(u,c):
    s=sess(); s["in"]+=u.get("in",0); s["out"]+=u.get("out",0); s["cost"]+=c; s["calls"]+=1
def _version():
    try: return open(os.path.join(HERE,"VERSION"), encoding="utf-8").read().strip()
    except Exception: return "dev"

def do_tailor(job, sc, cfg):
    obj, usage = tailor.tailor_structured(job, sc, cfg)
    cost = llm.est_cost(usage, price_for(cfg))
    db.add_usage(usage.get("in",0), usage.get("out",0), cost); acc(usage,cost)
    t = tailor.assemble_tailored(obj, job)
    rp = render.render_docx(t, os.path.join(HERE,"outputs","resumes"))
    db.add_document("tailored", rp, company=job.get("company",""), title=job.get("title",""),
                    content_hash=job.get("content_hash"), label="API tailored", mode="api")
    cp = render.render_cover_letter(t, os.path.join(HERE,"outputs","resumes")) \
         if cfg.get("tailoring",{}).get("generate_cover_letter",True) else None
    if cp:
        db.add_document("cover", cp, company=job.get("company",""), title=job.get("title",""),
                        content_hash=job.get("content_hash"), label="API cover letter", mode="api")
    return rp, cp, t.get("fit_notes",""), cost, usage

def do_prep_draft(job, questions, cfg):
    """API-mode: draft application answers, cost-tracked (mirrors do_tailor)."""
    import apply_prep
    answers, usage = apply_prep.draft_answers(job, questions, cfg)
    cost = llm.est_cost(usage, price_for(cfg))
    db.add_usage(usage.get("in",0), usage.get("out",0), cost); acc(usage,cost)
    return answers, cost

def add_and_score(company, title, loc, url, jd, cfg):
    """Persist a manual/imported job and score it. Returns (hash, score_dict|None).
    Shared by both the paste-importer and the field-entry tabs."""
    h = db.add_manual_job(company, title, loc, url, jd)
    job = {"content_hash": h, "company": company.strip(), "title": title.strip(),
           "location": loc.strip(), "url": url.strip(), "jd_text": jd.strip()}
    sc = score.score_job(job, cfg) if hasattr(score, "score_job") else None
    if sc is None:
        pairs = score.score_jobs([job], cfg)
        sc = pairs[0][1] if pairs else None
    if sc:
        db.save_score(h, sc)
    return h, sc

def do_job_extract(raw, deterministic, cfg):
    """LLM fallback for a weak paste-parse. Cost-tracked; ALWAYS returns a
    ParseResult (never raises) — on any failure the deterministic parse is kept."""
    import jobimport, json as _json
    try:
        prompt = jobimport.build_extract_prompt(raw)
        tc = cfg.get("tailoring", {})
        obj, usage = llm.chat_json(prompt, tc.get("base_url"), tc.get("model"),
                                   api=tc.get("api","openai"), api_key=llm.get_api_key())
        cost = llm.est_cost(usage, price_for(cfg))
        db.add_usage(usage.get("in",0), usage.get("out",0), cost); acc(usage,cost)
        return jobimport.merge_llm(deterministic, obj), cost, None
    except Exception as e:
        return deterministic, 0.0, str(e)

def render_prep(r, cfg, mode, cap):
    """Application Prep Pack: show the form's REAL questions, draft editable answers,
    and hand the user a copy-block + apply link. Never submits — manual by design.
    Schema is fetched once per job and cached in session_state to avoid re-hitting
    the network on every Streamlit rerun."""
    import apply_prep
    h = r["content_hash"]
    job = {"company": r.get("company",""), "title": r.get("title",""),
           "location": r.get("location",""), "url": r.get("url",""),
           "jd_text": r.get("jd_text",""), "source": r.get("source",""),
           "content_hash": h}
    sk = "prep_schema_"+h
    if sk not in st.session_state:
        with st.spinner("Fetching the application's real questions…"):
            st.session_state[sk] = apply_prep.fetch_schema(job)
    schema = st.session_state[sk]

    src_label = {"greenhouse":"live Greenhouse form","lever_standard":"Lever standard fields",
                 "generic":"generic checklist"}.get(schema["schema_source"], schema["schema_source"])
    st.markdown(f"**Application questions** · source: _{src_label}_")
    if schema.get("note"): st.caption(schema["note"])
    if schema.get("apply_url"):
        st.markdown(f"[Open the application page]({schema['apply_url']})")

    qs = schema.get("questions", [])
    st.markdown("**The form will ask for:**")
    for q in qs:
        req = " *(required)*" if q.get("required") else ""
        opt = (" — options: "+", ".join(q["options"])) if q.get("options") else ""
        st.markdown(f"- {q['label']}{req}{opt}")

    draftable = apply_prep.draftable_questions(qs)
    if not draftable:
        st.info("No free-text or choice questions to draft for this form.")
        return

    ak = "prep_answers_"+h
    if mode == "api":
        if st.button("Draft answers (API)", key="prep_draft_"+h):
            if db.usage_today()["cost"] >= cap:
                st.warning(f"Cap ${cap:.2f} reached.")
            else:
                with st.spinner("Drafting answers from your CV…"):
                    try:
                        answers, cost = do_prep_draft(job, draftable, cfg)
                        st.session_state[ak] = answers
                        st.success(f"Drafted · ${cost:.4f}. Review and edit below.")
                    except llm.LLMError as e:
                        st.error(f"Draft failed: {e}")
    else:
        with st.expander("Manual mode — copy this prompt into Claude Pro"):
            st.code(apply_prep.manual_prompt(job, draftable), language="markdown")
            st.caption("Paste Claude's answers into the fields below.")

    drafted = st.session_state.get(ak, {})
    st.markdown("**Draft answers — edit before you use them:**")
    edited = {}
    for q in draftable:
        lbl = q["label"] + (" (required)" if q.get("required") else "")
        edited[q["key"]] = st.text_area(lbl, value=drafted.get(q["key"], ""),
                                        key=f"prep_ans_{h}_{q['key']}", height=110)

    block = "\n\n".join(f"## {q['label']}\n{edited.get(q['key'],'').strip()}" for q in draftable)
    st.markdown("**Copy-paste block:**")
    st.text_area("All answers", block, height=200, key="prep_block_"+h)

    if st.button("Save prep pack", key="prep_save_"+h):
        db.save_prep_pack(h, {"schema_source": schema["schema_source"],
                              "apply_url": schema.get("apply_url",""),
                              "questions": qs, "answers": edited})
        st.success("Saved. You can revisit it anytime.")
    st.caption("Review everything, then submit on the application page yourself. "
               "Meridian never submits applications for you — by design.")

@st.dialog("How to use Manual (Claude Pro) tailoring", width="large")
def manual_help():
    st.markdown("""
**One-time setup**
1. claude.ai → **Projects → New Project**, name it **“Resume Tailoring.”**
2. Upload **`resume_master.yaml`** as project knowledge.

**For each role**
3. Review Queue → open a role → click **Claude prompt**.
4. Copy the prompt → paste into your Claude project → send.
5. Claude returns a tailored summary, skills, bullets, and cover letter.
6. Paste into your resume doc and save. Apply, then **Mark applied**.

**Tips** — reuse the same project; use this for Tier A, the no-LLM .docx for Tier B;
switch to **API mode** for fully one-click (~$0.001/role).
""")
    if st.button("Got it", type="primary"): st.rerun()

# ============ HEADER ============
st.title("Meridian"); st.caption(f"version {_version()} · AI-powered job search copilot")
cfg = settings.load_config()
s = db.stats(); u = db.usage_today(); cap = float(cfg.get("tailoring",{}).get("daily_cost_cap_usd",1.0))
due = db.due_followups(); n_due = len(due["applications"]) + len(due["outreach"])
m1,m2,m3,m4,m5,m6 = st.columns([1,1,1,1.1,1.2,1.4])
m1.metric("Total jobs", s["total"], help="Cumulative across ALL runs (deduped) — not just this run. See the per-run summary below the button.")
m2.metric("Tier A (total)", s["by_tier"].get("A",0), help="All Tier-A roles ever found, cumulative.")
m3.metric("Applied", s["applied"]); m4.metric("Follow-ups due", n_due)
m5.metric("Today's cost", f"${u['cost']:.3f}", help=f"cap ${cap:.2f}")
with m6:
    if st.button("▶ Run pipeline now", use_container_width=True, type="primary"):
        bar = st.progress(0.0, text="Starting…")
        def _cb(stage, i, total, msg):
            frac = (i/total)*0.5 if stage=="discover" else 0.5 + (i/total)*0.5
            label = "Discovering" if stage=="discover" else "Scoring"
            try: bar.progress(min(max(frac,0.0),1.0), text=f"{label}: {msg} ({i}/{total})")
            except Exception: pass
        result = runner.main(on_progress=_cb)
        bar.empty()
        st.session_state["last_report"] = result
        st.success(f"Done · {result['ingested']} ingested · {result['scored']} newly scored")
        st.rerun()

tab_review, tab_analytics, tab_cv, tab_settings, tab_tracker = st.tabs(
    [f"{ui.icon('review')} Review Queue", f"{ui.icon('analytics')} Analytics",
     f"{ui.icon('cv')} CV", f"{ui.icon('settings')} Settings", f"{ui.icon('tracker')} Tracker"])

# ===================== REVIEW QUEUE =====================
with tab_review:
    mode = cfg.get("tailoring",{}).get("mode","manual")
    var = st.radio("Tag new applications as variant (for A/B testing)", ["A","B"],
                   index=0 if st.session_state.get("variant","A")=="A" else 1,
                   horizontal=True, key="variant")
    st.caption("Use A/B to test two resume/approach styles, then compare reply rates in Analytics.")
    with st.container(border=True):
        st.markdown("**Token & cost meter**")
        su = sess(); price = price_for(cfg)
        g1,g2,g3,g4 = st.columns(4)
        g1.metric("Session calls", su["calls"]); g2.metric("Tokens in", f"{su['in']:,}")
        g3.metric("Tokens out", f"{su['out']:,}"); g4.metric("Session cost", f"${su['cost']:.4f}")
        st.caption(f"Today **${u['cost']:.3f}** of **${cap:.2f}** cap. "
                   "ℹ Discovery + scoring run locally and use **0 tokens** — cost applies only to tailoring.")
        live_box = st.empty()
    if mode == "manual" and st.button("How to use Manual mode"): manual_help()

    # ── Add a job you found elsewhere (LinkedIn, Naukri, referral, …) ──
    with st.expander("Add a job you found elsewhere (LinkedIn, Naukri, referral…)"):
        st.caption("You looked at a job while logged in — that's fine. Meridian never touches "
                   "LinkedIn; it only reads text you paste here, then scores/tailors/tracks it "
                   "exactly like a discovered job. Re-pasting the same role updates it in place "
                   "(no duplicates).")
        tab_paste, tab_fields = st.tabs(["Paste full posting", "Enter fields manually"])

        # ---- Tab 1: paste the whole posting, parse into fields to confirm ----
        with tab_paste:
            import jobimport
            ui.field("Paste the whole job posting",
                     "On the LinkedIn job page: Ctrl+A, Ctrl+C, then paste here. "
                     "Meridian extracts the fields for you to confirm before scoring.")
            raw = st.text_area("raw paste", key="imp_raw", height=200,
                               label_visibility="collapsed",
                               placeholder="Paste the full job posting text…")
            if st.button("Parse posting", key="imp_parse"):
                st.session_state["imp_parsed"] = dict(jobimport.parse_job_text(raw))

            parsed = st.session_state.get("imp_parsed")
            if parsed is not None:
                res = jobimport.ParseResult(parsed)
                if res.needs_review:
                    miss = ", ".join(res.missing_required) or "low confidence"
                    st.warning(f"Some fields need your review ({miss}). Edit below, or use "
                               "LLM cleanup." + ("" if mode == "api"
                               else " (switch to API mode in Settings to enable one-click cleanup)."))
                    if mode == "api" and st.button("Clean up with LLM", key="imp_llm"):
                        if db.usage_today()["cost"] >= cap:
                            st.warning(f"Daily cost cap ${cap:.2f} reached.")
                        else:
                            with st.spinner("Extracting fields…"):
                                merged, cost, err = do_job_extract(raw, res, cfg)
                                if err:
                                    st.error(f"LLM cleanup failed ({err}); keeping the parsed fields.")
                                else:
                                    st.session_state["imp_parsed"] = dict(merged)
                                    st.success(f"Cleaned up · ${cost:.4f}. Confirm below.")
                                    st.rerun()
                else:
                    st.success("Parsed. Confirm the fields below, then add.")

                pc1, pc2 = st.columns(2)
                i_company = pc1.text_input("Company *", value=res.get("company",""), key="imp_company")
                i_title   = pc2.text_input("Role title *", value=res.get("title",""), key="imp_title")
                pc3, pc4 = st.columns(2)
                i_loc = pc3.text_input("Location", value=res.get("location",""), key="imp_loc")
                i_url = pc4.text_input("Job link", value=res.get("url",""), key="imp_url")
                ui.field("Job description *", "What the scorer and tailoring use.")
                i_jd = st.text_area("jd", value=res.get("jd_text",""), key="imp_jd",
                                    height=160, label_visibility="collapsed")
                if st.button("Add & score this job", type="primary", key="imp_add"):
                    if not (i_company.strip() and i_title.strip() and i_jd.strip()):
                        st.error("Company, role title and job description are required.")
                    else:
                        try:
                            h, sc = add_and_score(i_company, i_title, i_loc, i_url, i_jd, cfg)
                            st.session_state.pop("imp_parsed", None)
                            st.success(f"Added **{i_title.strip()} @ {i_company.strip()}**"
                                       + (f" · scored {sc['fit']} (Tier {sc['tier']})" if sc else "")
                                       + ". Find it in the queue below.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not add job: {e}")

        # ---- Tab 2: the original field-by-field entry ----
        with tab_fields:
            mc1, mc2 = st.columns(2)
            m_company = mc1.text_input("Company *", key="mj_company")
            m_title   = mc2.text_input("Role title *", key="mj_title")
            mc3, mc4 = st.columns(2)
            m_loc = mc3.text_input("Location", key="mj_loc", placeholder="e.g. Bengaluru, India")
            m_url = mc4.text_input("Job link", key="mj_url", placeholder="https://…")
            ui.field("Job description / requirements *",
                      "Paste the full JD text — this is what the scorer and tailoring use.")
            m_jd  = st.text_area("Job description / requirements *", key="mj_jd", height=160,
                                 label_visibility="collapsed")
            if st.button("Add & score this job", type="primary", key="mj_add"):
                if not (m_company.strip() and m_title.strip() and m_jd.strip()):
                    st.error("Company, role title and job description are required.")
                else:
                    try:
                        h, sc = add_and_score(m_company, m_title, m_loc, m_url, m_jd, cfg)
                        st.success(f"Added **{m_title} @ {m_company}**"
                                   + (f" · scored {sc['fit']} (Tier {sc['tier']})" if sc else "")
                                   + ". Find it in the queue below to tailor and track.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not add job: {e}")

    # Prominent THIS-RUN summary (so cumulative header metrics aren't mistaken for run results)
    lastr = st.session_state.get("last_report")
    if lastr:
        st.success(f"**Last run:** {lastr['ingested']} roles matched your filters "
                   f"· {lastr['scored']} new this run · {lastr['ingested']-lastr['scored']} already in queue. "
                   "The header counts above are cumulative totals, not this run.")
    rep = lastr.get("report") if lastr else None
    if rep:
        with st.expander("Last run — funnel per company (why roles were/weren't kept)",
                         expanded=bool([r for r in rep if r['kept']==0 and r['fetched']>0])):
            st.table([{"Portal":r["source"], "Token":r["token"], "Fetched":r["fetched"],
                       "✓ Kept":r["kept"], "Title":r.get("drop_title",0), "Location":r.get("drop_loc",0),
                       "Stale":r.get("drop_stale",0), "Locations seen":r.get("sample_locs","") or r["note"]}
                      for r in rep])
            st.caption("Read each row as a funnel: **Fetched → minus Title-misses → minus Location-misses "
                       "→ minus Stale = Kept.** If a company fetched many but kept 0, the columns show which "
                       "filter removed them, and 'Locations seen' shows where its roles actually are.")

    def _gen(prompt, max_tokens, label, key):
        """API: generate via LLM (cost-tracked). Manual: show prompt to copy. Tab-scoped."""
        if mode=="api" and db.usage_today()["cost"]<cap:
            try:
                txt,us=llm.chat(prompt, cfg["tailoring"]["base_url"], cfg["tailoring"]["model"],
                                api=cfg["tailoring"].get("api","openai"), api_key=llm.get_api_key(), max_tokens=max_tokens)
                cst=llm.est_cost(us,price); db.add_usage(us.get("in",0),us.get("out",0),cst); acc(us,cst)
                st.text_area(label, txt, height=200, key=key); return txt
            except llm.LLMError as e: st.error(f"{label} failed: {e}"); return None
        else:
            st.code(prompt, language="markdown"); st.caption("Paste into Claude Pro."); return None

    # ── Filters: tier, search, status, source, tailored ──
    fc1, fc2, fc3 = st.columns([1,1,2])
    ui.field("Tier", "Default shows all tiers. Filter to A for your strongest matches.")
    tier = fc1.selectbox("Tier", ["(all)","A","B","C"], index=0, label_visibility="collapsed")
    src_opts = ["(all)","greenhouse","lever","ashby","workday","jsearch","manual","other"]
    src_filter = fc2.selectbox("Source", src_opts, index=0)
    search = fc3.text_input("Search company or role", placeholder="e.g. Razorpay, or 'senior product'")
    fc4, fc5 = st.columns([1,3])
    status_filter = fc4.selectbox("Status", ["(all)","not applied","applied","interviewing","rejected","offer"], index=0)
    tailored_only = fc5.checkbox("Only roles not yet tailored", value=False)

    rows = db.ranked_jobs(None if tier=="(all)" else tier)
    _locs = cfg.get("locations", []); _rok = cfg.get("remote_ok", True)
    all_rows_for_locnote = db.ranked_jobs(None if tier=="(all)" else tier)
    if _locs or not _rok:
        before = len(rows)
        rows = [r for r in rows if ingest._loc_match({"location": r.get("location","")}, _locs, _rok)]
        if (before-len(rows))>0 and len(rows)==0:
            locset = sorted({(r.get("location") or "-") for r in all_rows_for_locnote})[:12]
            st.warning(f"**0 roles match your location filter `{', '.join(_locs)}`.** "
                f"Roles found are in: _{', '.join(locset)}_. Broaden locations "
                "(add Bengaluru/Mumbai/Hyderabad) or clear the filter in Settings.")

    def _src_bucket(sv):
        sv = str(sv or "").split(":")[0]
        return sv if sv in src_opts else "other"
    if search.strip():
        q = search.lower().strip()
        rows = [r for r in rows if q in r["company"].lower() or q in r["title"].lower()]
    if src_filter != "(all)":
        rows = [r for r in rows if _src_bucket(r.get("source")) == src_filter]
    if status_filter != "(all)":
        want = status_filter.replace(" ","_")
        rows = [r for r in rows if (r.get("status") or "not_applied") == want]
    if tailored_only:
        rows = [r for r in rows if not r.get("resume_path")]

    st.caption(f"{len(rows)} role(s) shown · tailoring mode: **{mode}**")

    if mode == "api" and rows:
        todo = [r for r in rows if not r.get("resume_path")]
        if st.button(f"{ui.icon('tailor')} Tailor all shown ({len(todo)} pending)"):
            prog=st.progress(0.0); done=bi=bo=0; bc=0.0
            for i,r in enumerate(todo):
                if db.usage_today()["cost"]>=cap: st.warning(f"Cap ${cap:.2f} reached."); break
                job={k:r[k] for k in ["company","title","location","url","jd_text"]}; job["content_hash"]=r["content_hash"]
                scd={"missing_keywords":loads(r["missing_keywords"]),"missing_skills":loads(r["missing_skills"])}
                try:
                    rp,cp,_,c,us=do_tailor(job,scd,cfg); db.set_app(r["content_hash"],resume_path=rp,cover_path=cp)
                    done+=1; bi+=us.get("in",0); bo+=us.get("out",0); bc+=c
                    live_box.info(f"Tailored {done}/{len(todo)} · {bi:,} in / {bo:,} out · ${bc:.4f}")
                except llm.LLMError as e: st.error(f"{r['company']}: {e}")
                prog.progress((i+1)/max(len(todo),1))
            st.success(f"Done {done} · ${bc:.4f}"); st.rerun()

    # ── Comprehensive table + selected-row action bar ──
    if rows:
        import pandas as _pd
        by_hash = {r["content_hash"]: r for r in rows}
        _df = _pd.DataFrame([{
            "Pick": False, "Tier": r["tier"], "Fit": r["fit"], "Company": r["company"], "Role": r["title"],
            "Location": r.get("location",""), "Source": _src_bucket(r.get("source")),
            "Status": (r.get("status") or "not_applied").replace("_"," "),
            "Tailored": "yes" if r.get("resume_path") else "",
            "hash": r["content_hash"],
        } for r in rows])
        edited = st.data_editor(
            _df, use_container_width=True, hide_index=True, height=min(440, 60+len(rows)*35),
            column_config={
                "Pick": st.column_config.CheckboxColumn("Act", help="Tick to act on this row", width="small"),
                "Fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=100, format="%d"),
                "hash": None,
            },
            disabled=["Tier","Fit","Company","Role","Location","Source","Status","Tailored"],
            key="queue_editor")
        picked = edited[edited["Pick"]]["hash"].tolist() if "Pick" in edited else []
        st.caption("Tick a row's checkbox to act on it. Sort by any column header.")

        if picked:
            sel = by_hash.get(picked[0])
            if sel:
                st.markdown(f"**Selected:** {sel['company']} — {sel['title']}  ·  Tier {sel['tier']} · Fit {sel['fit']}")
                a1,a2,a3,a4 = st.columns(4)
                job={k:sel[k] for k in ["company","title","location","url","jd_text"]}; job["content_hash"]=sel["content_hash"]
                scd={"missing_keywords":loads(sel["missing_keywords"]),"missing_skills":loads(sel["missing_skills"])}
                if sel.get("url"):
                    a1.link_button(f"{ui.icon('open')} Open job", sel["url"], use_container_width=True)
                else:
                    a1.button(f"{ui.icon('open')} Open job", disabled=True, use_container_width=True, help="No link")
                if mode=="api":
                    if a2.button(f"{ui.icon('tailor')} Tailor CV", key="bar_tl", use_container_width=True):
                        with st.spinner("Tailoring…"):
                            try:
                                rp,cp,notes,c,us=do_tailor(job,scd,cfg); db.set_app(sel["content_hash"],resume_path=rp,cover_path=cp)
                                st.success(f"Tailored · ${c:.4f}. See CV documents to download."); st.rerun()
                            except llm.LLMError as e: st.error(str(e))
                    if a3.button(f"{ui.icon('cover')} Cover letter", key="bar_cl", use_container_width=True):
                        with st.spinner("Generating…"):
                            try: _gen(tailor.build_outreach_prompt(job), 400, "Cover letter draft", "bar_clp_"+sel["content_hash"])
                            except Exception as e: st.error(str(e))
                else:
                    a2.button(f"{ui.icon('tailor')} Tailor CV", disabled=True, use_container_width=True, help="API mode only")
                    a3.button(f"{ui.icon('cover')} Cover letter", disabled=True, use_container_width=True, help="API mode only")
                if a4.button(f"{ui.icon('apply')} Mark applied", key="bar_ap", use_container_width=True):
                    db.set_app(sel["content_hash"], status="applied",
                               applied_at=datetime.datetime.now().isoformat(timespec="seconds"))
                    st.success("Marked applied."); st.rerun()

    for r in rows:
        with st.expander(f"[{r['tier']}] {r['fit']}  ·  {r['company']} — {r['title']}  ·  {r['location']}"):
            c1,c2,c3,c4=st.columns(4)
            c1.metric("Semantic",r["semantic"]); c2.metric("Keywords",r["keywords"])
            c3.metric("Skills",r["skills"]); c4.metric("Seniority",r["seniority"])
            st.write(f"**Why:** {r['rationale']}")
            mk,ms=loads(r["missing_keywords"]),loads(r["missing_skills"])
            if mk: st.write("**JD keywords you under-use:** "+", ".join(mk))
            if ms: st.write("**Skill gaps:** "+", ".join(ms))
            if r["url"]: st.markdown(f"[Open job posting]({r['url']})")
            job={k:r[k] for k in ["company","title","location","url","jd_text"]}
            sc={"missing_keywords":mk,"missing_skills":ms}
            b1,b2,b3=st.columns(3)
            if b1.button("Tailor (1-click)" if mode=="api" else "Claude prompt", key="tl_"+r["content_hash"]):
                if mode=="api":
                    if db.usage_today()["cost"]>=cap: st.warning(f"Cap ${cap:.2f} reached.")
                    else:
                        with st.spinner("Auto-tailoring..."):
                            try:
                                rp,cp,notes,c,us=do_tailor(job,sc,cfg); db.set_app(r["content_hash"],resume_path=rp,cover_path=cp,variant=var)
                                st.success(f"Done · {us.get('in',0):,} in / {us.get('out',0):,} out · ${c:.4f}")
                                if notes: st.info("**Fit notes:** "+notes)
                                with open(rp,"rb") as f: st.download_button("Resume (.docx)",f,file_name=os.path.basename(rp),key="drp_"+r["content_hash"])
                                if cp: st.text_area("Cover letter",open(cp,encoding="utf-8").read(),height=160,key="cov_"+r["content_hash"])
                            except llm.LLMError as e: st.error(f"API error: {e}")
                else:
                    p=tailor.save_prompt(job,sc,os.path.join(HERE,"outputs","prompts")); db.set_app(r["content_hash"],prompt_path=p,variant=var)
                    st.code(open(p,encoding="utf-8").read(),language="markdown")

            if b2.button("No-LLM .docx", key="tb_"+r["content_hash"]):
                with st.spinner("Selecting your best real bullets..."):
                    tl=tailor.tailor_template(job,get_model()); path=render.render_docx(tl,os.path.join(HERE,"outputs","resumes")); ok,n=render.parse_back_check(path)
                db.add_document("tailored", path, company=r["company"], title=r["title"],
                                content_hash=r["content_hash"], label="No-LLM (template)", mode="template")
                db.set_app(r["content_hash"],resume_path=path,variant=var)
                with open(path,"rb") as f: st.download_button(".docx",f,file_name=os.path.basename(path),key="dl_"+r["content_hash"])
                st.success(f"parse-back: {'OK' if ok else 'CHECK'} · {n} chars")

            if b3.button("Applied", key="ap_"+r["content_hash"]):
                t=datetime.date.today(); db.set_app(r["content_hash"],status="applied",applied_at=t.isoformat(),
                           follow_up_at=(t+datetime.timedelta(days=7)).isoformat(),variant=var)
                st.success(f"Applied (variant {var}) · follow-up in 7 days"); st.rerun()

            b4,b5,b6=st.columns(3)
            if b4.button("Answers", key="aa_"+r["content_hash"]):
                ans = cfg.get("application",{}).get("answers",{})
                st.markdown("**Standard answers**\n"+"\n".join(
                    f"- {k.replace('_',' ').title()}: {v or '(set in Settings)'}" for k,v in ans.items()))
                _gen(tailor.build_screening_prompt(job), 500, "Drafted answers (why this company / first 90 days)", "sa_"+r["content_hash"])
            if b5.button("Prep notes", key="pn_"+r["content_hash"]):
                _gen(tailor.build_prep_prompt(job), 800, "Interview prep (questions + talking points)", "pn2_"+r["content_hash"])
            if b6.button("Draft outreach", key="or_"+r["content_hash"]):
                msg=_gen(tailor.build_outreach_prompt(job), 350, "Outreach message", "or2_"+r["content_hash"])
                note = (msg[:80] if msg else "drafted") + "..."
                if st.button("Log to outreach tracker", key="orlog_"+r["content_hash"]):
                    db.add_outreach(r["company"], "", "LinkedIn", "sent", note,
                                    (datetime.date.today()+datetime.timedelta(days=7)).isoformat())
                    st.success("Logged to Tracker → Outreach.")

            st.divider()
            if st.button(f"{ui.icon('apply')} Prep application", key="prep_btn_"+r["content_hash"]):
                st.session_state["show_prep_"+r["content_hash"]] = True
            if st.session_state.get("show_prep_"+r["content_hash"]):
                render_prep(r, cfg, mode, cap)

# ===================== SETTINGS =====================
with tab_settings:
    cfg = settings.load_config(); res = settings.load_resume()

    st.subheader("Targets")
    st.caption("Pick portals, add company tokens, set desired locations. Hover ⓘ for help.")
    with st.expander("ℹ Why can't I add LinkedIn / Indeed / Naukri?"):
        st.markdown("Those are aggregators with no public API to read postings or apply, and "
            "automating them breaks their terms. Meridian pulls from the **ATS systems "
            "companies use** (Greenhouse, Lever, Ashby, Recruitee, SmartRecruiters, Workday), "
            "which expose public posting endpoints. Find a role on LinkedIn → add that company's ATS here.")

    cur = cfg.get("sources", {})
    all_p = list(ingest.PORTALS.keys())
    default_active = [p for p in all_p if cur.get(p)] or ["greenhouse"]
    # multiselect OUTSIDE the form so token boxes appear/disappear immediately on change
    active = st.multiselect("Active job portals", all_p,
        default=st.session_state.get("active_portals", default_active),
        format_func=lambda p: ingest.PORTALS[p]["name"], key="active_portals",
)
    st.caption("Select ATS providers. A token box appears for each — disappears when you deselect one.")
    if not active:
        st.warning("Select at least one portal to add company tokens.")

    LEVELS=["intern","junior","mid","senior","staff","principal","director"]
    cur_sen = cfg.get("target_seniority","senior")
    cur_sen = [cur_sen] if isinstance(cur_sen,str) else cur_sen
    with st.form("targets"):
        tok = {}
        for p in active:
            m = ingest.PORTALS[p]
            ui.field(f"{m['name']} — tokens (one per line)", f"{m['desc']}. {m['help']}")
            tok[p] = st.text_area(f"{m['name']} — tokens (one per line)", "\n".join(cur.get(p,[])),
                                  placeholder=m["example"], label_visibility="collapsed")
        st.divider()
        ui.field("Desired locations (one per line) — e.g. India, or Bengaluru",
                 "Geography-aware: 'India' auto-matches Bengaluru, Mumbai, Hyderabad, Pune, Delhi NCR … Empty = no filter.")
        locs = st.text_area("Desired locations (one per line) — e.g. India, or Bengaluru",
                            "\n".join(cfg.get("locations",[])), label_visibility="collapsed")
        st.caption("Typing **India** auto-includes Bengaluru, Mumbai, Hyderabad, Pune, Chennai, Delhi/NCR, "
                   "Kolkata, Ahmedabad and major states — you don't need to list cities. Same for US/UK/etc.")
        rok = st.checkbox("Include Remote roles", value=cfg.get("remote_ok",True))
        st.caption("Adds purely-remote roles. Remote roles geo-locked to another country (e.g. 'Remote - US') are still excluded.")
        ui.field("Role keywords (one per line)",
                 "Semantic: 'product manager' matches Group/Staff/Principal PM, Product Lead, etc.")
        roles = st.text_area("Role keywords (one per line)", "\n".join(cfg.get("role_keywords",[])),
                             label_visibility="collapsed")
        strict = st.checkbox("Strict title match (exact substring only)",
                             value=cfg.get("strict_title_filter", False))
        st.caption("OFF (recommended): Group PM / Product Lead / Principal PM all pass. ON: exact substring only.")
        prio = st.text_area("Priority companies (always Tier A)", "\n".join(cfg.get("priority_companies",[])))
        st.caption("Every job gets a **fit score from 0–100** (semantic match + keywords + skills + seniority). "
                   "The two thresholds below split jobs into tiers: **Tier A** = score ≥ A (your strongest matches), "
                   "**Tier B** = score ≥ B but < A (good matches), **Tier C** = everything below B (deprioritized). "
                   "Higher A = fewer, stronger matches. Not sure what to set? Run once, then use Calibrate below.")
        a,b,c = st.columns(3)
        with a:
            ui.field("Tier A ≥", "Top targets. Typical: 70–80. Higher = stricter.")
            tA=st.number_input("Tier A ≥",0,100,int(cfg.get("tiers",{}).get("A",70)), label_visibility="collapsed")
        with b:
            ui.field("Tier B ≥", "Solid matches. Typical: 55–65.")
            tB=st.number_input("Tier B ≥",0,100,int(cfg.get("tiers",{}).get("B",55)), label_visibility="collapsed")
        with c:
            ui.field("Freshness (days)", "Skip postings older than this. Typical: 14–30.")
            fr=st.number_input("Freshness (days)",1,120,int(cfg.get("freshness_days",21)), label_visibility="collapsed")
        ui.field("Target seniority", "Roles matching ANY selected level score highest; others sink to Tier C.")
        sen=st.multiselect("Target seniority (select one or more)", LEVELS,
            default=[s for s in cur_sen if s in LEVELS] or ["senior"], label_visibility="collapsed")
        if st.form_submit_button("Save targets"):
            L=lambda t:[x.strip() for x in t.splitlines() if x.strip()]
            cfg["sources"]={p:L(tok[p]) for p in active}; cfg["locations"]=L(locs); cfg["remote_ok"]=rok
            cfg["role_keywords"]=L(roles); cfg["priority_companies"]=L(prio)
            cfg["strict_title_filter"]=bool(strict)
            cfg["tiers"]={"A":int(tA),"B":int(tB)}; cfg["freshness_days"]=int(fr)
            cfg["target_seniority"]=sen or ["senior"]
            settings.save_config(cfg); st.success("Saved. Re-run the pipeline to apply.")

    # ----- Company token finder (single) -----
    st.markdown("**Find a company's ATS token**")
    fc1,fc2 = st.columns([3,1])
    cname = fc1.text_input("Company name", key="probe_name", placeholder="e.g. Razorpay")
    if fc2.button("Search") and cname.strip():
        with st.spinner("Probing all 9 portals (incl. Workday)..."):
            hits = ingest.probe_company(cname)
        if not hits:
            st.warning("No public ATS endpoint found. The company may use Darwinbox/iCIMS/Keka/Naukri "
                       "(no public API), or a custom site. Apply directly there instead.")
        else:
            for portal, token in hits:
                cc1,cc2 = st.columns([3,1])
                cc1.success(f"**{ingest.PORTALS[portal]['name']}** → token `{token}`")
                if cc2.button("Add", key=f"add_{portal}_{token}"):
                    cfg = settings.load_config()
                    cfg.setdefault("sources",{}).setdefault(portal,[])
                    if token not in cfg["sources"][portal]:
                        cfg["sources"][portal].append(token); settings.save_config(cfg)
                    st.success(f"Added {token} to {portal}. Re-run pipeline to fetch."); st.rerun()

    # ----- Batch company verifier (resolves many companies live, on your machine) -----
    st.divider()
    st.markdown("**Target companies → ATS finder** (Layer 0 registry)")
    st.caption("Your persistent list of companies to track. Verify resolves each to its public ATS "
               "live from your machine, and 'Add confirmed' caches the tokens into your sources. "
               "The list is remembered across sessions.")
    SEED = ("Postman\nRazorpay\nPhonePe\nFreshworks\nChargebee\nBrowserStack\nGroww\nWhatfix\n"
            "Meesho\nCRED\nJuspay\nZepto\nInMobi\nUrban Company\nLivspace\nHealthPlix\nMoEngage\nMyntra")
    saved_companies = cfg.get("target_companies", [])
    default_val = "\n".join(saved_companies) if saved_companies else SEED
    names_txt = st.text_area("Companies", value=st.session_state.get("bulk_names", default_val),
                             height=160, key="bulk_names")
    if st.button("Save company list"):
        cfg["target_companies"] = [n.strip() for n in names_txt.splitlines() if n.strip()]
        settings.save_config(cfg); st.success(f"Saved {len(cfg['target_companies'])} companies to your registry.")
    if st.button("Verify all", type="primary"):
        names = [n.strip() for n in names_txt.splitlines() if n.strip()]
        bar = st.progress(0.0, text="Starting…")
        def _cb(stage,i,total,msg):
            try: bar.progress(min(i/total,1.0), text=f"Probing {msg} ({i}/{total})")
            except Exception: pass
        res = ingest.probe_many(names, quick=True, on_progress=_cb)
        bar.empty()
        st.session_state["bulk_result"] = res

    bres = st.session_state.get("bulk_result")
    if bres:
        rows, found_map = [], {}
        for name, hits in bres.items():
            if hits:
                label = ", ".join(f"{ingest.PORTALS[p]['name']}:{t}" for p,t in hits)
                for p,t in hits: found_map.setdefault(p,[]).append(t)
            else:
                label = "— no public ATS (Darwinbox/iCIMS/Naukri/custom)"
            rows.append({"Company":name, "Found on":label})
        st.table(rows)
        n_found = sum(1 for v in bres.values() if v)
        st.caption(f"{n_found} of {len(bres)} resolved to a public ATS. The rest need manual apply or use a non-public ATS.")
        if found_map and st.button("Add all confirmed tokens to config"):
            cfg = settings.load_config(); src = cfg.setdefault("sources",{})
            added = 0
            for portal, toks in found_map.items():
                src.setdefault(portal,[])
                for t in toks:
                    if t not in src[portal]: src[portal].append(t); added += 1
            settings.save_config(cfg)
            st.success(f"Added {added} confirmed tokens across {len(found_map)} portals. Re-run the pipeline."); st.rerun()

    # ----- Web-wide search (JSearch) -----
    st.divider()
    st.markdown("**Web-wide search (JSearch — Google for Jobs / open web)**")
    st.caption("Beyond the ATS portals: searches the open web (LinkedIn, Indeed, Naukri, company "
               "pages) via a sanctioned API — legal, no scraping, full job descriptions. "
               "Free tier, no credit card: sign up at openwebninja.com → JSearch, paste the key below.")
    js = cfg.get("jsearch", {}) or {}
    jc1, jc2 = st.columns([1,1])
    js_enabled = jc1.checkbox("Enable web-wide search", value=js.get("enabled", False))
    jc1.caption("Queries JSearch on every pipeline run for your role + locations.")
    js_provider = jc2.selectbox("Provider", ["openwebninja","rapidapi"],
                                index=0 if js.get("provider","openwebninja")=="openwebninja" else 1)
    jc2.caption("openwebninja = direct, no card required. rapidapi = via RapidAPI.")
    ui.field("Search endpoint", "Default works for OpenWeb Ninja. RapidAPI: https://jsearch.p.rapidapi.com/search")
    js_base = st.text_input("Search endpoint", js.get("base_url", connectors.DEFAULT_BASE), label_visibility="collapsed")
    jd1, jd2, jd3 = st.columns(3)
    with jd1:
        ui.field("Country code", "ISO country code — 'in' for India.")
        js_country = st.text_input("Country code", js.get("country","in"), label_visibility="collapsed")
    js_posted  = jd2.selectbox("Posted within", ["all","today","3days","week","month"],
                               index=["all","today","3days","week","month"].index(js.get("date_posted","month")))
    with jd3:
        ui.field("Pages per query", "Each page ≈ 10 jobs. More pages = more results + more API usage.")
        js_pages = st.number_input("Pages per query", 1, 5, int(js.get("num_pages",1)), label_visibility="collapsed")
    js_queries_txt = st.text_area("Custom queries (one per line) — leave blank to auto-build from role keywords + locations",
                                  "\n".join(js.get("queries", [])),
                                  placeholder="product manager in India\nsenior product manager in Bengaluru")
    js_key = st.text_input("JSearch API key", type="password", value="",
                           placeholder="paste your x-api-key — blank keeps the saved key")
    st.caption("JSearch key saved." if connectors.get_jsearch_key() else "No JSearch key saved yet.")
    auto_q = connectors.build_queries(cfg.get("role_keywords",[]), cfg.get("locations",[]))
    st.caption(f"Auto queries that will run: _{', '.join(auto_q)}_")
    jb1, jb2 = st.columns(2)
    if jb1.button("Save web-wide settings", use_container_width=True):
        cfg["jsearch"] = {"enabled":bool(js_enabled), "provider":js_provider, "base_url":js_base,
                          "country":js_country.strip() or "in", "date_posted":js_posted,
                          "num_pages":int(js_pages),
                          "queries":[q.strip() for q in js_queries_txt.splitlines() if q.strip()]}
        settings.save_config(cfg)
        if js_key.strip(): connectors.save_jsearch_key(js_key.strip())
        st.success("Saved. Web-wide search will run on the next pipeline run.")
    if jb2.button("Save & Test JSearch", type="primary", use_container_width=True):
        if js_key.strip(): connectors.save_jsearch_key(js_key.strip())
        cfg["jsearch"] = {**cfg.get("jsearch",{}), "enabled":bool(js_enabled), "provider":js_provider,
                          "base_url":js_base, "country":js_country.strip() or "in",
                          "date_posted":js_posted, "num_pages":int(js_pages),
                          "queries":[q.strip() for q in js_queries_txt.splitlines() if q.strip()]}
        settings.save_config(cfg)
        with st.spinner("Testing JSearch…"):
            ok,msg = connectors.health(connectors.get_jsearch_key(), js_provider, js_base, js_country.strip() or "in")
        (st.success if ok else st.error)(f"{'Connected' if ok else 'Failed'}: {msg}")

    # ----- Threshold calibration -----
    with st.expander("Calibrate tier thresholds (after a run)"):
        scores = [r["fit"] for r in db.ranked_jobs()]
        if not scores:
            st.info("Run the pipeline first, then come back to see your score distribution.")
        else:
            step=10; bins={f"{lo}-{lo+step}":0 for lo in range(0,100,step)}
            for sc in scores: bins[f"{min(int(sc//step)*step,90)}-{min(int(sc//step)*step,90)+step}"]+=1
            st.bar_chart(bins)
            import numpy as np
            pa,pb=int(np.percentile(scores,75)),int(np.percentile(scores,50))
            st.caption(f"Suggested by your data: **Tier A ≥ {max(pa,pb+5)}**, **Tier B ≥ {pb}** "
                       f"(top 25% / top 50%). {len(scores)} scored roles.")
            if st.button("Apply suggested thresholds"):
                cfg=settings.load_config(); cfg["tiers"]={"A":max(pa,pb+5),"B":pb}; settings.save_config(cfg)
                st.success("Saved. Re-run pipeline to re-tier."); st.rerun()

    st.divider()
    st.subheader("Tailoring backend")
    tc = cfg.get("tailoring", {})
    key_saved = bool(llm.get_api_key())
    # Mode is the ONE decision the user makes routinely; it saves immediately.
    mode = st.radio("Mode", ["manual","api"],
                    index=0 if tc.get("mode","manual")=="manual" else 1, horizontal=True,
                    format_func=lambda m: "Manual (Claude Pro — free)" if m=="manual" else "API (automatic)")
    st.caption("Manual = copy a prompt into Claude Pro (free). API = one-click tailoring using your saved key.")
    if mode != tc.get("mode"):
        tc["mode"] = mode; cfg["tailoring"] = tc; settings.save_config(cfg)   # persist the choice instantly
    if mode == "api" and key_saved:
        st.success(f"API mode active · provider **{tc.get('provider','—')}** · model `{tc.get('model','—')}` · key saved. "
                   "No need to re-enter your key — it's used automatically.")
    if mode=="manual":
        st.info("Manual mode uses your Claude Pro subscription — free, best quality.")
        if st.button("Show step-by-step instructions"): manual_help()
        if st.button("Save backend (manual)"):
            tc["mode"]="manual"; cfg["tailoring"]=tc; settings.save_config(cfg); st.success("Saved.")
    else:
        provs=list(llm.PROVIDERS.keys())
        ui.field("Provider", "Pick the service — endpoint and API style fill in automatically.")
        prov=st.selectbox("Provider", provs,
            index=provs.index(tc.get("provider")) if tc.get("provider") in provs else 0,
            label_visibility="collapsed")
        P=llm.PROVIDERS[prov]

        if prov=="Custom":
            base=st.text_input("Base URL", tc.get("base_url",""), placeholder="https://api.example.com/v1")
            api_style=st.radio("API style", ["openai","anthropic"],
                index=0 if tc.get("api","openai")=="openai" else 1, horizontal=True)
            st.caption("anthropic = Claude native /v1/messages. openai = /chat/completions (Bearer token).")
            model=st.text_input("Model", tc.get("model",""), placeholder="model-name")
            price={"in":0.0,"out":0.0}
        else:
            base=P["base_url"]; api_style=P["api"]
            models=list(P["models"].keys())
            ui.field("Model", "Versions for this provider. Cheapest first.")
            model=st.selectbox("Model", models,
                index=models.index(tc["model"]) if tc.get("model") in models else 0,
                label_visibility="collapsed")
            price=P["models"][model]
            st.caption(f"Endpoint: `{base}`  ·  API style: **{api_style}**·  "
                       f"est. ${price['in']}/M in · ${price['out']}/M out (editable estimate)")

        ui.field("Daily cost cap (USD)", "Tailoring stops once today's spend hits this — your safety net.")
        capv=st.number_input("Daily cost cap (USD)",0.0,100.0,float(tc.get("daily_cost_cap_usd",1.0)),step=0.5,
            label_visibility="collapsed")
        cov=st.checkbox("Also generate a cover letter",value=tc.get("generate_cover_letter",True))

        # Key is saved once and reused automatically — only surface entry when needed.
        if llm.get_api_key():
            with st.expander("API key — saved ✓ (click only to change it)"):
                ui.field(f"Replace API key ({P.get('key_hint','')})",
                         "Leave blank to keep the existing saved key — used automatically.")
                key=st.text_input(f"Replace API key ({P.get('key_hint','')})", type="password", value="",
                    label_visibility="collapsed")
        else:
            if prov.startswith("Local"):
                key=st.text_input("API key (not needed for local Ollama)", type="password", value="")
                st.caption("ℹ No key needed for local Ollama.")
            else:
                key=st.text_input(f"API key ({P.get('key_hint','')})", type="password", value="",
                    placeholder="paste your key — saved locally, gitignored")
                st.caption("No API key saved yet — paste it above, then Save & Test.")

        def _persist(extra_msg=""):
            tc.update({"mode":"api","provider":prov,"base_url":base,"model":model,"api":api_style,
                       "price":price,"daily_cost_cap_usd":float(capv),"generate_cover_letter":cov})
            cfg["tailoring"]=tc; settings.save_config(cfg)
            if key.strip(): llm.save_api_key(key.strip())

        d1,d2=st.columns(2)
        if d1.button("Save backend", use_container_width=True):
            _persist()
            st.success(f"Saved — tailoring via {prov} · {model} ({api_style} API).")
        if d2.button("Save & Test connection", type="primary", use_container_width=True):
            _persist()                          # <-- saves the key FIRST (fixes the 401)
            with st.spinner("Testing..."):
                ok,msg=llm.health(base,model,api=api_style,api_key=(key.strip() or llm.get_api_key()))
            (st.success if ok else st.error)(f"{'Connected' if ok else 'Failed'}: {msg}")

    st.divider()
    ui.header("edit", "Application answers")
    st.caption("Stored standard answers for common form fields — shown per-role for quick copy-paste.")
    ans = cfg.get("application",{}).get("answers",{})
    with st.form("answers"):
        new={}
        for k in ["work_authorization","notice_period","salary_expectation","years_experience","willing_to_relocate"]:
            new[k]=st.text_input(k.replace("_"," ").title(), ans.get(k,""))
        if st.form_submit_button("Save answers"):
            cfg.setdefault("application",{})["answers"]=new; settings.save_config(cfg); st.success("Saved.")

    with st.expander(f"{ui.icon('generate')} Generate an answer to an application question"):
        st.caption("Draft an answer grounded in your CV + a target role's JD + your saved answers. "
                   "Review and edit before use. (API mode drafts automatically; manual mode shows the prompt.)")
        rows_aa = db.ranked_jobs()
        job_opts = {f"{r['company']} — {r['title']}": r for r in rows_aa}
        aq = st.text_area("The application question",
                          placeholder="e.g. Why do you want to work here? · Describe a product you shipped 0 to 1.")
        sel_job_label = st.selectbox("Tailor to which role (optional)", ["(none)"] + list(job_opts.keys()))
        if st.button(f"{ui.icon('generate')} Generate answer", key="gen_answer"):
            if not aq.strip():
                st.error("Enter a question first.")
            else:
                jb = job_opts.get(sel_job_label, {})
                job = {"company":jb.get("company",""), "title":jb.get("title",""),
                       "jd_text":jb.get("jd_text","")} if jb else {"company":"","title":"","jd_text":""}
                prompt = tailor.build_answer_prompt(aq.strip(), job, res, ans)
                _tc = cfg.get("tailoring", {})
                if _tc.get("mode")=="api" and llm.get_api_key():
                    with st.spinner("Drafting…"):
                        try:
                            txt,us = llm.chat(prompt, _tc["base_url"], _tc["model"],
                                              api=_tc.get("api","openai"), api_key=llm.get_api_key(), max_tokens=400)
                            db.add_usage(us.get("in",0), us.get("out",0), llm.est_cost(us, price_for(cfg)))
                            st.session_state["gen_answer_txt"] = txt
                        except Exception as e:
                            st.error(str(e))
                else:
                    st.code(prompt, language="markdown"); st.caption("Paste into Claude Pro to get your answer.")
        if st.session_state.get("gen_answer_txt"):
            edited_ans = st.text_area("Draft answer (edit freely, then copy or save)",
                                      st.session_state["gen_answer_txt"], height=160, key="gen_answer_edit")
            sa1, sa2 = st.columns(2)
            save_as = sa1.text_input("Save as (key)", placeholder="e.g. why_this_company")
            if sa2.button("Save to my answers", key="save_gen_answer"):
                if save_as.strip():
                    cfg.setdefault("application",{}).setdefault("answers",{})[save_as.strip()] = edited_ans
                    settings.save_config(cfg); st.success(f"Saved as '{save_as.strip()}'."); st.rerun()
                else:
                    st.error("Enter a key to save under.")

    st.divider()
    ui.header("cv", "Your CV")
    res["name"]=st.text_input("Name",res.get("name","")); res["title"]=st.text_input("Headline",res.get("title",""))
    res["summary"]=st.text_area("Summary",res.get("summary",""),height=80)
    res["skills"]=[x.strip() for x in st.text_area("Skills (one per line)","\n".join(res.get("skills",[])),height=110).splitlines() if x.strip()]
    if st.button("Save basics"): settings.save_resume(res); get_model.clear(); st.success("Saved.")
    st.markdown("**Experience** (bullets — one per line)")
    res=settings.load_resume()
    for i,e in enumerate(res.get("experiences",[])):
        with st.expander(f"{e['company']} — {e['role']}"):
            co=st.text_input("Company",e["company"],key=f"co{i}"); ro=st.text_input("Role",e["role"],key=f"ro{i}")
            da=st.text_input("Dates",e["dates"],key=f"da{i}"); bl=st.text_area("Bullets","\n".join(e["bullets"]),height=130,key=f"bl{i}")
            x1,x2=st.columns(2)
            if x1.button("Save",key=f"sv{i}"):
                res["experiences"][i]={"company":co,"role":ro,"dates":da,"bullets":[b.strip() for b in bl.splitlines() if b.strip()]}
                settings.save_resume(res); get_model.clear(); st.success("Saved."); st.rerun()
            if x2.button("Remove",key=f"rm{i}"):
                res["experiences"].pop(i); settings.save_resume(res); get_model.clear(); st.rerun()
    if st.button("Add experience"):
        res.setdefault("experiences",[]).append({"company":"New Co","role":"Role","dates":"YYYY - YYYY","bullets":["Achievement"]})
        settings.save_resume(res); st.rerun()

# ===================== TRACKER =====================
with tab_tracker:
    due = db.due_followups()
    if due["applications"] or due["outreach"]:
        st.subheader("Follow-ups due now")
        for d in due["applications"]:
            cc=st.columns([3,1,1])
            cc[0].write(f"**{d['company']}** — {d['title']} · _{d['status']}_")
            cc[1].write(f"due {d['follow_up_at']}")
            if cc[2].button("Snooze 7d", key="snz_"+d["content_hash"]):
                nd=(datetime.date.today()+datetime.timedelta(days=7)).isoformat()
                db.set_app(d["content_hash"], follow_up_at=nd); st.rerun()
        for d in due["outreach"]:
            cc=st.columns([3,1,1])
            cc[0].write(f"**{d['company']}** — {d['contact']} · _{d['status']}_")
            cc[1].write(f"due {d['follow_up_at']}")
            if cc[2].button("Snooze 7d", key="osnz_"+str(d["id"])):
                nd=(datetime.date.today()+datetime.timedelta(days=7)).isoformat()
                db.update_outreach(d["id"], follow_up_at=nd); st.rerun()
        st.divider()

    st.subheader("Outreach")
    st.caption("Log LinkedIn/email outreach (e.g. the Salesforce PM messages) alongside applications.")
    with st.form("new_outreach"):
        o1,o2,o3=st.columns(3)
        oc=o1.text_input("Company"); on=o2.text_input("Contact"); och=o3.selectbox("Channel",["LinkedIn","Email","Referral","Other"])
        o4,o5=st.columns(2)
        ost=o4.selectbox("Status",["sent","replied","scheduled","closed"]); ofu=o5.date_input("Follow-up date",datetime.date.today()+datetime.timedelta(days=7))
        onotes=st.text_input("Notes")
        if st.form_submit_button("Log outreach") and oc.strip():
            db.add_outreach(oc,on,och,ost,onotes,ofu.isoformat()); st.success("Logged."); st.rerun()
    for o in db.list_outreach():
        cc=st.columns([2,2,1,1.5,1])
        cc[0].write(f"**{o['company']}**"); cc[1].write(o["contact"] or "—")
        cc[2].write(o["channel"])
        ns=cc[3].selectbox("s",["sent","replied","scheduled","closed"],
              index=["sent","replied","scheduled","closed"].index(o["status"]) if o["status"] in ["sent","replied","scheduled","closed"] else 0,
              key="os_"+str(o["id"]),label_visibility="collapsed")
        if ns!=o["status"]: db.update_outreach(o["id"],status=ns)
        cc[4].write(o["follow_up_at"] or "")

    st.divider()
    st.subheader("Applications")
    rows=[r for r in db.ranked_jobs() if r["status"] and r["status"]!="not_applied"]
    if not rows: st.info("No applications yet.")
    else:
        sts=["applied","screen","interview","offer","rejected"]
        for r in rows:
            cc=st.columns([3,1,1,1.5])
            cc[0].write(f"**{r['company']}** — {r['title']}")
            cur=r["status"] if r["status"] in sts else "applied"
            ns=cc[1].selectbox("s",sts,index=sts.index(cur),key="st_"+r["content_hash"],label_visibility="collapsed")
            if ns!=r["status"]: db.set_app(r["content_hash"],status=ns)
            cc[2].write(r["applied_at"] or ""); cc[3].write(f"follow-up: {r['follow_up_at'] or '-'}")

# ===================== CV TAB =====================
with tab_cv:
    import cv as _cv

    # ── Header: completeness score (always visible) ──
    try:
        rdata = settings.load_resume()
        issues = _cv.check_completeness(rdata)
        summ   = _cv.completeness_summary(issues)
    except Exception:
        rdata, issues, summ = {}, [], {"score":0,"errors":0,"warnings":0,"tips":0}

    sc_col, _, btn_col = st.columns([2,3,2])
    score = summ["score"]
    score_color = "normal" if score >= 75 else ("off" if score >= 50 else "inverse")
    sc_col.metric("CV completeness", f"{score}/100",
                  delta=f"{summ['errors']} errors · {summ['warnings']} warnings",
                  delta_color=score_color,
                  help="Based on placeholders, empty required fields, weak bullets and missing metrics.")

    # ── Section A: Upload & parse ──
    st.subheader("Upload & parse")
    st.caption("Upload your existing PDF or Word CV. Claude extracts it into structured YAML — "
               "you review before anything is saved. Your current CV is auto-snapshotted first.")
    tc = cfg.get("tailoring", {})
    can_parse = bool(tc.get("mode")=="api" and tc.get("base_url") and llm.get_api_key()
                     and tc.get("api")=="anthropic")
    if not can_parse:
        st.warning("Set **Settings → API mode → Provider: Anthropic** and save your API key to enable "
                   "one-click parsing. You need the Anthropic API for this feature.")

    ui.field("Drop your CV here", "PDF (native Claude read) or DOCX (text extracted then parsed).")
    uploaded = st.file_uploader("Drop your CV here", type=["pdf","docx"], label_visibility="collapsed")
    if uploaded and can_parse:
        if st.button("Parse with Claude", type="primary"):
            with st.spinner(f"Parsing {uploaded.name} …"):
                try:
                    raw_bytes = uploaded.read()
                    ext = uploaded.name.rsplit(".",1)[-1].lower()
                    if ext == "pdf":
                        parsed = _cv.parse_from_pdf(raw_bytes, tc["base_url"], tc["model"], llm.get_api_key())
                    else:
                        parsed = _cv.parse_from_docx(raw_bytes, tc["base_url"], tc["model"], llm.get_api_key())
                    normed = _cv.parsed_to_yaml(parsed)
                    st.session_state["cv_parsed"] = normed
                    st.success("Parsed — review below, then confirm to save.")
                except Exception as e:
                    st.error(f"Parse failed: {e}")

    parsed_preview = st.session_state.get("cv_parsed")
    if parsed_preview:
        with st.expander("Parsed CV — review before saving", expanded=True):
            st.caption("This is what was extracted. Saving will snapshot your current CV first.")
            p_issues = _cv.check_completeness(parsed_preview)
            p_summ   = _cv.completeness_summary(p_issues)
            pa,pb,pc = st.columns(3)
            pa.metric("Score",     f"{p_summ['score']}/100")
            pb.metric("Errors",    p_summ["errors"])
            pc.metric("Warnings",  p_summ["warnings"])
            if p_issues:
                with st.expander("Issues found in parsed CV"):
                    for i in p_issues[:10]:
                        icon = "" if i["level"]=="error" else ("" if i["level"]=="warning" else "")
                        st.caption(f"{icon} **{i['field']}**: {i['message']}")
            st.json(parsed_preview, expanded=False)
            label = st.text_input("Snapshot label (optional)", placeholder="e.g. from-linkedin-pdf")
            if st.button("Confirm & save parsed CV", type="primary"):
                ok_v, probs = _cv.validate_resume(parsed_preview)
                if not ok_v:
                    st.error("Parsed CV looks malformed and was not saved: " + "; ".join(probs))
                else:
                    settings.save_resume(parsed_preview, label=label or "uploaded-parse", source="upload")
                    try:
                        base = dict(parsed_preview); base["tailored_for"] = "Master_CV"
                        op = render.render_docx(base, os.path.join(HERE,"outputs","resumes"))
                        db.add_document("original", op, company="", title="Master CV",
                                        label=label or "uploaded", mode="upload")
                    except Exception:
                        pass
                    st.session_state.pop("cv_parsed", None)
                    st.success("Saved. Your previous CV was auto-snapshotted.")
                    st.rerun()
            if st.button("Discard — keep current CV"):
                st.session_state.pop("cv_parsed", None)
                st.rerun()

    st.divider()

    # ── Section B: Completeness report ──
    ui.header("score", "Completeness report")
    _tc = cfg.get("tailoring", {})
    _can_fix = bool(_tc.get("mode")=="api" and llm.get_api_key() and _tc.get("api")=="anthropic")
    if not issues:
        st.success("No issues found — CV looks complete.")
    else:
        if not _can_fix:
            st.caption("Tip: in API mode (Anthropic) each issue gets a one-click "
                       "**Generate fix** that drafts a concrete improvement to review and apply.")
        def _issue_row(i, kind):
            cols = st.columns([5,1])
            box = {"error":cols[0].error, "warning":cols[0].warning, "tip":cols[0].info}[kind]
            box(f"**{i['field']}**: {i['message']}")
            fix_key = f"fix_{i['field']}"
            if _can_fix and i["field"] not in ("placeholder",) and cols[1].button(
                    "Generate fix", key=f"btn_{fix_key}", use_container_width=True):
                with st.spinner("Drafting a fix…"):
                    try:
                        txt,us = llm.chat(tailor.build_fix_prompt(i, rdata),
                                          _tc["base_url"], _tc["model"], api=_tc.get("api","anthropic"),
                                          api_key=llm.get_api_key(), max_tokens=400)
                        db.add_usage(us.get("in",0), us.get("out",0), llm.est_cost(us, price_for(cfg)))
                        st.session_state[fix_key] = txt
                    except Exception as e:
                        st.error(str(e))
            if st.session_state.get(fix_key):
                st.text_area("Suggested fix (review, then apply manually to your CV)",
                             st.session_state[fix_key], height=120, key=f"ta_{fix_key}")
                st.caption("Copy the improved text into the relevant field of your master CV, then re-check.")
        err  = [i for i in issues if i["level"]=="error"]
        warn = [i for i in issues if i["level"]=="warning"]
        tips = [i for i in issues if i["level"]=="tip"]
        if err:
            with st.expander(f"{len(err)} error(s) — fix before tailoring", expanded=True):
                for i in err: _issue_row(i, "error")
        if warn:
            with st.expander(f"{len(warn)} warning(s)"):
                for i in warn: _issue_row(i, "warning")
        if tips:
            with st.expander(f"{len(tips)} tip(s)"):
                for i in tips: _issue_row(i, "tip")

    st.divider()

    # ── Section C: Version history ──
    ui.header("restore", "Saved versions")
    snaps = _cv.list_snapshots()
    if not snaps:
        st.caption("No saved versions yet. Every time you save your CV a snapshot is created automatically.")
    else:
        st.caption(f"{len(snaps)} snapshot(s). Restoring auto-saves your current CV first.")
        for s in snaps:
            fn   = s.get("filename","")
            ts   = s.get("timestamp","")[:16].replace("T"," ")
            lbl  = s.get("label","")
            src  = s.get("source","")
            exists = s.get("exists", True)
            c1,c2,c3,c4 = st.columns([2,2,1,2])
            c1.caption(f"**{ts}**")
            c2.caption(lbl or "—")
            c3.caption(src)
            if not exists:
                c4.caption("file missing")
                continue
            btn_r, btn_d = c4.columns(2)
            if btn_r.button("Restore", key=f"rst_{fn}"):
                try:
                    _cv.restore_snapshot(fn)
                    st.success(f"Restored {fn}. Your current CV was auto-snapshotted first.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
            if btn_d.button("Delete", key=f"del_{fn}"):
                _cv.delete_snapshot(fn)
                st.rerun()

    st.divider()
    ui.header("doc", "My CV documents")
    st.caption("Every generated and uploaded CV, versioned and downloadable. "
               "Originals are your master CV; Tailored are role-specific versions.")
    if st.button(f"{ui.icon('download')} Generate a fresh copy of my master CV"):
        try:
            base = dict(settings.load_resume()); base["tailored_for"] = "Master_CV"
            op = render.render_docx(base, os.path.join(HERE,"outputs","resumes"))
            db.add_document("original", op, company="", title="Master CV", label="manual export", mode="export")
            st.success("Master CV generated — see the table below."); st.rerun()
        except Exception as e:
            st.error(f"Could not generate: {e}")

    docs = db.list_documents()
    if not docs:
        st.caption("No CV documents yet. Tailor a role, or upload & save a CV above, to populate this.")
    else:
        import pandas as _pd
        type_label = {"original":"Original","tailored":"Tailored","cover":"Cover letter"}
        dfd = _pd.DataFrame([{
            "Type": type_label.get(d["doc_type"], d["doc_type"]),
            "For": (f"{d['company']} — {d['title']}" if d.get("company") else (d.get("title") or "-")),
            "Label": d.get("label",""), "Mode": d.get("mode",""),
            "Created": (d.get("created_at","") or "")[:16].replace("T"," "),
            "File": os.path.basename(d.get("path","") or ""),
        } for d in docs])
        st.dataframe(dfd, use_container_width=True, hide_index=True, height=min(360, 60+len(docs)*35))

        labels = {f"#{d['id']} · {type_label.get(d['doc_type'],d['doc_type'])} · "
                  f"{(d['company']+' — '+d['title']) if d.get('company') else (d.get('title') or '-')} · "
                  f"{(d.get('created_at') or '')[:16].replace('T',' ')}": d for d in docs}
        pick = st.selectbox("Select a document", list(labels.keys()))
        chosen = labels.get(pick)
        if chosen:
            path = chosen.get("path","")
            cd1, cd2 = st.columns([1,3])
            if path and os.path.exists(path):
                with open(path,"rb") as f:
                    cd1.download_button(f"{ui.icon('download')} Download", f,
                                        file_name=os.path.basename(path), key=f"dlcat_{chosen['id']}",
                                        use_container_width=True)
                if path.endswith(".txt"):
                    with cd2.expander("View contents"):
                        st.text(open(path, encoding="utf-8").read())
                else:
                    cd2.caption("Word document — download to view.")
            else:
                cd1.caption("File missing on disk.")
            if cd1.button("Delete", key=f"delcat_{chosen['id']}", use_container_width=True):
                db.delete_document(chosen["id"]); st.rerun()

        with st.expander(f"{ui.icon('compare')} Compare two CV versions (field-level diff)"):
            snaps2 = _cv.list_snapshots()
            opts = {f"{x['timestamp'][:16].replace('T',' ')} · {x.get('label','') or '-'}": x["filename"]
                    for x in snaps2 if x.get("exists", True)}
            opts = {"Current master": "current", **opts}
            if len(opts) < 2:
                st.caption("Need at least two saved versions to compare.")
            else:
                kk = list(opts.keys())
                ca, cb = st.columns(2)
                aa = ca.selectbox("Version A", kk, index=min(1, len(kk)-1))
                bb = cb.selectbox("Version B", kk, index=0)
                if st.button("Compare"):
                    changes = _cv.diff_snapshots(opts[aa], opts[bb])
                    if not changes:
                        st.success("No differences between these two versions.")
                    else:
                        st.dataframe(_pd.DataFrame(changes), use_container_width=True, hide_index=True)

# ===================== ANALYTICS =====================
with tab_analytics:
    rows = db.ranked_jobs()
    apps = analytics.applied_only(rows)
    st.caption(f"{len(apps)} applications logged. Metrics are directional — small samples are noisy.")
    if not apps:
        st.info("No applications yet. Tailor + Mark applied some roles, then come back here.")
    else:
        counts, total = analytics.funnel(rows)
        st.subheader("Funnel")
        f1,f2,f3,f4 = st.columns(4)
        f1.metric("Applied", counts["applied"])
        f2.metric("Screen", counts["screen"],
                  f"{round(100*counts['screen']/total)}%" if total else "—")
        f3.metric("Interview", counts["interview"],
                  f"{round(100*counts['interview']/max(counts['screen'],1))}% of screens")
        f4.metric("Offer", counts["offer"])

        st.subheader("Conversion by Tier")
        st.caption("Validates the scoring: higher tiers should reply more. If not, the scoring needs tuning.")
        bt = analytics.conversion_by(rows, "tier")
        st.bar_chart({k: v["resp_rate"] for k, v in sorted(bt.items())})
        st.table([{"Tier":k, "Apps":v["n"], "Response %":v["resp_rate"], "Interview %":v["int_rate"]}
                  for k,v in sorted(bt.items())])

        st.subheader("A/B: Conversion by Variant")
        bv = analytics.conversion_by(rows, "variant")
        if len([k for k in bv if k in ("A","B")]) >= 2:
            st.table([{"Variant":k, "Apps":v["n"], "Response %":v["resp_rate"], "Interview %":v["int_rate"]}
                      for k,v in sorted(bv.items())])
            ns = sum(v["n"] for v in bv.values())
            if ns < 20: st.caption(f"Only {ns} tagged apps — treat as directional, not significant.")
        else:
            st.caption("Tag applications as A or B (toggle in the Review Queue) to compare here.")

        st.subheader("Conversion by Portal")
        bp = analytics.conversion_by(rows, "source")
        st.table([{"Portal":k, "Apps":v["n"], "Response %":v["resp_rate"]} for k,v in sorted(bp.items())])

        st.divider()
        st.subheader("Learning loop — suggested scoring weights")
        st.caption("Compares the fit sub-scores of roles that replied vs. those that didn't, and nudges "
                   "the scoring weights toward what correlates with replies. Conservative by design.")
        cfg = settings.load_config()
        cur_w = cfg.get("weights", {})
        sug = analytics.suggest_weights(rows, cur_w)
        if not sug:
            st.info("Need at least 3 responders and 3 non-responders to suggest weights. Keep applying.")
        else:
            st.write(f"Based on **{sug['n_resp']}** responders vs **{sug['n_non']}** non-responders.")
            st.write("**Score gaps** (responder minus non-responder, +ve = predicts replies):")
            st.write({k: f"{'+' if v>=0 else ''}{v}" for k,v in sug["gaps"].items()})
            comp = [{"Feature":f, "Current":cur_w.get(f,0), "Suggested":sug["weights"][f]}
                    for f in ["semantic","keywords","skills","seniority"]]
            st.table(comp)
            if sug["n_resp"]+sug["n_non"] < 15:
                st.caption("Small sample — review before applying; don't over-fit to a few data points.")
            if st.button("Apply suggested weights"):
                cfg["weights"] = sug["weights"]; settings.save_config(cfg)
                st.success("Saved. Re-run the pipeline to re-score with the new weights.")
