"""
Streamlit front-end for the HR/CRM data migration agent.
"""
from __future__ import annotations

import io
import os
import sys
import uuid

import streamlit as st
from dotenv import load_dotenv

# Ensure backend modules are importable
_backend_dir = os.path.join(os.path.dirname(__file__), "backend")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

load_dotenv()

# On Streamlit Cloud, secrets live in st.secrets — bridge them into os.environ
# so that database.py, llm.py etc. can still use os.getenv() unchanged.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str) and _k not in os.environ:
            os.environ[_k] = _v
except Exception:
    pass

import agent as ag
import mock_api
import database

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="HR Migration Agent",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS — minimal, keeps it clean ──────────────────────────────────────
st.markdown("""
<style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; }
    .stTabs [data-baseweb="tab"] { font-size: 0.9rem; }
</style>
""", unsafe_allow_html=True)


# ── Session state ─────────────────────────────────────────────────────────────
def _init_session():
    defaults = {
        "pipeline": ag.AgentState(),
        "last_batch_id": None,
        "push_results": [],
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


_init_session()
s: ag.AgentState = st.session_state.pipeline


# ── Helpers ───────────────────────────────────────────────────────────────────
def _public(record: dict) -> dict:
    return {k: v for k, v in record.items() if not k.startswith("__")}


def _is_pending(v) -> bool:
    return isinstance(v, str) and v.startswith("__pending__")


def _pending_count() -> int:
    return sum(1 for e in s.escalations.values() if e.status == "pending")


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## HR Migration Agent")
    st.caption("Human-in-the-loop data migration pipeline")
    st.divider()

    uploaded_files = st.file_uploader(
        "Upload source CSV files",
        type=["csv"],
        accept_multiple_files=True,
        help="Upload one or more HR/CRM CSV exports.",
    )
    use_demo = st.checkbox(
        "Use bundled demo files",
        value=(not bool(uploaded_files)),
        help="Use the sample data/hris_export.csv and data/crm_export.csv files.",
    )

    st.divider()

    col_run, col_reset = st.columns(2)
    run_clicked = col_run.button("Run Pipeline", type="primary", use_container_width=True)
    reset_clicked = col_reset.button("Reset", use_container_width=True)

    st.divider()

    # LLM status indicator
    try:
        import llm as _llm
        if _llm.enabled():
            st.success(f"AI assist on  ({_llm.PROVIDER} / {_llm.MODEL})")
        else:
            st.info("AI assist off — set GROQ_API_KEY to enable")
    except Exception:
        st.info("AI assist off")

    st.divider()
    stage_label = s.stage if s.stage else "idle"
    st.markdown(f"**Stage:** `{stage_label}`")
    open_esc = _pending_count()
    if open_esc:
        st.warning(f"{open_esc} escalation(s) need your attention")


# ── Button actions ─────────────────────────────────────────────────────────────
if reset_clicked:
    st.session_state.pipeline = ag.AgentState()
    st.session_state.push_results = []
    st.session_state.last_batch_id = None
    mock_api.reset()
    st.rerun()

if run_clicked:
    files: dict = {}
    if uploaded_files and not use_demo:
        for f in uploaded_files:
            files[f.name] = io.BytesIO(f.read())
    else:
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        if os.path.isdir(data_dir):
            for name in sorted(os.listdir(data_dir)):
                if name.endswith(".csv"):
                    files[name] = os.path.join(data_dir, name)

    if not files:
        st.sidebar.error("No CSV files to process. Upload files or check the data/ folder.")
    else:
        st.session_state.pipeline = ag.AgentState()
        st.session_state.push_results = []
        st.session_state.last_batch_id = None
        s = st.session_state.pipeline
        with st.spinner(f"Processing {len(files)} file(s)..."):
            ag.run_pipeline(s, files)
        st.rerun()


# ── Main tabs ─────────────────────────────────────────────────────────────────
tab_log, tab_esc, tab_data, tab_target, tab_audit = st.tabs([
    "Pipeline Log",
    f"Escalations  [{_pending_count()} pending]",
    "Dataset",
    "Target Records",
    "Audit Log",
])


# ── Tab 1: Pipeline Log ───────────────────────────────────────────────────────
with tab_log:
    if not s.events:
        st.info("Run the pipeline to see activity here.")
    else:
        _level_color = {
            "info": "blue",
            "action": "green",
            "warn": "orange",
            "error": "red",
            "escalate": "red",
            "success": "green",
        }
        for ev in reversed(s.events):
            lvl = ev.get("level", "info")
            color = _level_color.get(lvl, "gray")
            st.markdown(f":{color}[**{lvl.upper()}**] `{ev['ts']}` — {ev['message']}")


# ── Tab 2: Escalations ────────────────────────────────────────────────────────
with tab_esc:
    pending_escs = [e for e in s.escalations.values() if e.status == "pending"]
    resolved_escs = [e for e in s.escalations.values() if e.status != "pending"]

    c1, c2, c3 = st.columns(3)
    c1.metric("Pending", len(pending_escs))
    c2.metric("Resolved", len(resolved_escs))
    c3.metric("Total", len(s.escalations))

    if not pending_escs:
        if s.stage not in ("idle", ""):
            st.success("All escalations resolved.")
        else:
            st.info("No escalations yet. Run the pipeline first.")
    else:
        for esc in pending_escs:
            with st.container(border=True):
                st.markdown(f"**{esc.title}**")
                st.caption(f"Kind: `{esc.kind}`  |  ID: `{esc.id}`")
                st.write(esc.detail)

                if esc.ai_suggestion:
                    sug = esc.ai_suggestion
                    st.info(
                        f"AI suggestion: **{sug.get('suggestion')}** "
                        f"({sug.get('confidence', '?')} confidence) — {sug.get('rationale', '')}"
                    )

                with st.form(key=f"form_{esc.id}"):
                    if esc.kind == "mapping":
                        choice = st.selectbox("Map this column to:", esc.options, key=f"sel_{esc.id}")
                        c1, c2 = st.columns(2)
                        apply_btn = c1.form_submit_button("Apply Mapping", type="primary")
                        ignore_btn = c2.form_submit_button("Ignore Column")
                        if apply_btn:
                            ag.resolve_escalation(s, esc.id, "correct", choice)
                            st.rerun()
                        if ignore_btn:
                            ag.resolve_escalation(s, esc.id, "reject")
                            st.rerun()

                    elif esc.kind == "merge_conflict":
                        choice = st.selectbox("Choose the correct value:", esc.options, key=f"sel_{esc.id}")
                        c1, c2 = st.columns(2)
                        use_btn = c1.form_submit_button("Use Selected", type="primary")
                        rej_btn = c2.form_submit_button("Reject Record")
                        if use_btn:
                            ag.resolve_escalation(s, esc.id, "correct", choice)
                            st.rerun()
                        if rej_btn:
                            ag.resolve_escalation(s, esc.id, "reject")
                            st.rerun()

                    elif esc.context.get("field") == "__full_name__":
                        raw = esc.context.get("raw_value", "")
                        parts = raw.split(" ", 1) if raw else ["", ""]
                        first = st.text_input("First name", value=parts[0], key=f"fn_{esc.id}")
                        last = st.text_input(
                            "Last name",
                            value=parts[1] if len(parts) > 1 else "",
                            key=f"ln_{esc.id}",
                        )
                        c1, c2 = st.columns(2)
                        save_btn = c1.form_submit_button("Save Name", type="primary")
                        skip_btn = c2.form_submit_button("Skip Record")
                        if save_btn:
                            ag.resolve_escalation(s, esc.id, "correct", {"first_name": first, "last_name": last})
                            st.rerun()
                        if skip_btn:
                            ag.resolve_escalation(s, esc.id, "reject")
                            st.rerun()

                    else:
                        # Email correction, date fix, missing required field
                        if "email" in esc.title.lower():
                            label = "Corrected email address"
                        elif "date" in esc.title.lower():
                            label = "Date (YYYY-MM-DD format)"
                        else:
                            label = "Corrected value"
                        correction = st.text_input(label, key=f"txt_{esc.id}")
                        c1, c2 = st.columns(2)
                        sub_btn = c1.form_submit_button("Submit", type="primary")
                        skip_btn = c2.form_submit_button("Skip Record")
                        if sub_btn:
                            ag.resolve_escalation(s, esc.id, "correct", correction)
                            st.rerun()
                        if skip_btn:
                            ag.resolve_escalation(s, esc.id, "reject")
                            st.rerun()


# ── Tab 3: Dataset ────────────────────────────────────────────────────────────
with tab_data:
    ready_recs = [_public(r) for r in s.final_records.values() if r.get("__ready__")]
    blocked_recs = [_public(r) for r in s.final_records.values() if not r.get("__ready__")]
    rejected_recs = [_public(r) for r in s.rejected_records]

    c1, c2, c3 = st.columns(3)
    c1.metric("Ready to push", len(ready_recs))
    c2.metric("Blocked on escalation", len(blocked_recs))
    c3.metric("Rejected", len(rejected_recs))

    if ready_recs:
        st.subheader("Ready records")
        st.dataframe(ready_recs, use_container_width=True)

    if blocked_recs:
        st.subheader("Waiting on escalation resolution")
        st.dataframe(blocked_recs, use_container_width=True)

    if rejected_recs:
        st.subheader("Rejected")
        st.dataframe(rejected_recs, use_container_width=True)

    if not any([ready_recs, blocked_recs, rejected_recs]):
        st.info("No records yet. Run the pipeline first.")


# ── Tab 4: Target Records ─────────────────────────────────────────────────────
with tab_target:
    ready_count = sum(1 for r in s.final_records.values() if r.get("__ready__"))
    push_res: list = st.session_state.push_results
    has_failed = any(not r["ok"] for r in push_res)
    has_batch = bool(st.session_state.last_batch_id)

    col_push, col_retry, col_roll = st.columns(3)
    push_btn = col_push.button("Push to Target", type="primary", disabled=(ready_count == 0))
    retry_btn = col_retry.button("Retry Failed", disabled=(not has_failed))
    roll_btn = col_roll.button("Rollback Last Batch", disabled=(not has_batch))

    if push_btn:
        batch_id = f"batch_{uuid.uuid4().hex[:8]}"
        st.session_state.last_batch_id = batch_id
        records_to_push = [r for r in s.final_records.values() if r.get("__ready__")]
        results = []
        with st.spinner(f"Pushing {len(records_to_push)} record(s)..."):
            for record in records_to_push:
                pub = _public(record)
                res, attempts = None, 0
                while attempts < 3:
                    attempts += 1
                    res = mock_api.push_record(pub, batch_id)
                    if res["ok"]:
                        break
                res["attempts"] = attempts
                results.append(res)
                emp_id = record.get("employee_id", "?")
                if res["ok"]:
                    s.audit_entry(emp_id, "pushed", f"Pushed after {attempts} attempt(s).")
                else:
                    s.audit_entry(emp_id, "push_failed", f"Failed after {attempts} attempt(s): {res.get('error')}")
        st.session_state.push_results = results
        ok_count = sum(1 for r in results if r["ok"])
        if ok_count == len(results):
            st.success(f"All {ok_count} records pushed successfully.")
        else:
            st.warning(f"{ok_count}/{len(results)} succeeded. Use Retry Failed for the rest.")
        st.rerun()

    if retry_btn:
        failed = [r for r in push_res if not r["ok"]]
        by_id = {r.get("employee_id"): r for r in s.final_records.values() if r.get("__ready__")}
        new_res = []
        with st.spinner("Retrying failed records..."):
            for f in failed:
                record = by_id.get(f.get("employee_id"))
                if not record:
                    continue
                res = mock_api.push_record(_public(record), st.session_state.last_batch_id)
                res["attempts"] = 1
                new_res.append(res)
        st.session_state.push_results = [r for r in push_res if r["ok"]] + new_res
        st.success(f"Retry done: {sum(1 for r in new_res if r['ok'])}/{len(new_res)} succeeded.")
        st.rerun()

    if roll_btn:
        removed = mock_api.rollback_batch(st.session_state.last_batch_id)
        s.audit_entry("__batch__", "rollback", f"Rolled back {st.session_state.last_batch_id}: {removed} record(s) removed.")
        st.session_state.push_results = []
        st.session_state.last_batch_id = None
        st.warning(f"Rollback complete — {removed} record(s) removed from target.")
        st.rerun()

    if push_res:
        st.subheader("Last push results")
        st.dataframe(push_res, use_container_width=True)

    st.subheader("Records in target system")
    live_records = mock_api.list_records()
    if live_records:
        st.dataframe(live_records, use_container_width=True)
    else:
        st.info("No records in the target system yet.")


# ── Tab 5: Audit Log ──────────────────────────────────────────────────────────
with tab_audit:
    if s.audit:
        st.dataframe(s.audit, use_container_width=True)
    else:
        st.info("Audit entries appear here after the pipeline runs and records are resolved/pushed.")
