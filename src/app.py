"""Phase 5: Streamlit UI.

Run (from study-agent/):  uv run streamlit run src/app.py
"""
import copy
import json
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from export_ics import DEFAULT_TZ, build_ics
from extract import extract
import agent
import store
from scheduler import DAYS

CONFIG, DATA, OUT = Path("config.json"), Path("data"), Path("output")

st.set_page_config(page_title="Study Scheduler", layout="wide")


# ---------- helpers ----------
def load_cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_cfg(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_proformas() -> list[dict]:
    DATA.mkdir(exist_ok=True)
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(DATA.glob("*.json"))]


def day_idx(name: str) -> int:
    return DAYS.index(name) if name in DAYS else 0


cfg = load_cfg()
proformas = load_proformas()
pref = cfg["preferences"]

# ---------- sidebar: settings ----------
with st.sidebar:
    st.header("Settings")
    start = st.date_input("Semester start", date.fromisoformat(cfg["semester"]["start_date"]))
    weeks = st.number_input("Weeks", 1, 30, int(cfg["semester"]["weeks"]))
    max_h = st.number_input("Max study hours/day", 1, 10, int(pref["max_study_hours_per_day"]))
    ahead = st.slider("Study-ahead days (heavy weeks)", 0, 21, int(pref.get("lookahead_days", 7)))
    free_eve = st.selectbox("Free evening", DAYS, day_idx(pref.get("free_evening", "saturday")))
    light = st.selectbox("Light day", DAYS, day_idx(pref.get("light_day", "sunday")))
    light_max = st.number_input("Light day max blocks", 0, 6, int(pref.get("light_day_max_blocks", 2)))
    if st.button("Save settings"):
        cfg["semester"].update(start_date=start.isoformat(), weeks=int(weeks))
        pref.update(max_study_hours_per_day=int(max_h), lookahead_days=int(ahead),
                    free_evening=free_eve, light_day=light, light_day_max_blocks=int(light_max))
        save_cfg(cfg)
        st.success("Saved")

st.title("Study Scheduler")
tab_pro, tab_ass, tab_sch, tab_chat = st.tabs(["1. Proformas", "2. Assessments", "3. Schedule", "4. Chat"])

# ---------- tab 1: proformas ----------
with tab_pro:
    files = st.file_uploader("Upload proforma PDFs (one per subject)", type="pdf",
                             accept_multiple_files=True)
    if files and st.button("Extract uploaded proformas"):
        DATA.mkdir(exist_ok=True)
        for f in files:
            try:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(f.getvalue())
                result = extract(Path(tmp.name))
                (DATA / f"{result.code}.json").write_text(
                    result.model_dump_json(indent=2), encoding="utf-8")
                st.success(f"{f.name}: {result.code} ({len(result.weeks)} weeks, "
                           f"{len(result.assessments)} assessments)")
            except Exception as e:  # noqa: BLE001
                st.error(f"{f.name}: could not read ({e})")
        st.rerun()

    if proformas:
        st.subheader("Loaded subjects")
        st.dataframe(pd.DataFrame([{
            "Code": p["code"], "Name": p["name"], "Credit": p["credit"],
            "Weeks": len(p["weeks"]), "Total SLT (h)": p["total_slt"],
        } for p in proformas]), hide_index=True)
        gone = st.selectbox("Remove a subject", [""] + [p["code"] for p in proformas])
        if gone and st.button(f"Remove {gone}"):
            (DATA / f"{gone}.json").unlink(missing_ok=True)
            st.rerun()
    else:
        st.info("No subjects yet. Upload a proforma PDF above.")

# ---------- tab 2: assessments ----------
with tab_ass:
    if not proformas:
        st.info("Load a proforma first.")
    else:
        st.caption("Leave the due date empty while it is TBD. Prep hours and the week range "
                   "are used until a date is set; with a date, prep is planned in the 2 weeks before it.")
        n_weeks = int(cfg["semester"]["weeks"])
        rows = []
        for p in proformas:
            for a in p["assessments"]:
                c = cfg.get("assessments", {}).get(p["code"], {}).get(a["name"], {})
                w = c.get("default_weeks", [max(1, n_weeks - 2), n_weeks])
                rows.append({
                    "Subject": p["code"], "Assessment": a["name"], "Weight %": a["weight"],
                    "Due date": date.fromisoformat(c["date"]) if c.get("date") else None,
                    "Prep hours": c.get("prep_hours", round(a["weight"] * 0.3)),
                    "From week": w[0], "To week": w[1],
                })
        edited = st.data_editor(
            pd.DataFrame(rows), hide_index=True, key="ass_editor",
            disabled=["Subject", "Assessment", "Weight %"],
            column_config={"Due date": st.column_config.DateColumn("Due date")},
        )
        if st.button("Save assessments"):
            out: dict = {}
            for _, r in edited.iterrows():
                d = None if pd.isna(r["Due date"]) else pd.Timestamp(r["Due date"]).date().isoformat()
                out.setdefault(r["Subject"], {})[r["Assessment"]] = {
                    "date": d, "status": "Set" if d else "TBD",
                    "prep_hours": int(r["Prep hours"]),
                    "default_weeks": [int(r["From week"]), int(r["To week"])],
                }
            cfg["assessments"] = out
            save_cfg(cfg)
            st.success("Saved")

# ---------- tab 3: schedule ----------
with tab_sch:
    if not proformas:
        st.info("Load a proforma first.")
    else:
        sem_start = date.fromisoformat(cfg["semester"]["start_date"])
        plan_from = st.date_input("Plan from (earlier days are skipped)",
                                  max(date.today(), sem_start))
        if st.button("Generate schedule", type="primary"):
            res = store.replan(plan_from)  # keeps pinned blocks and blocked days
            if res["ok"]:
                st.session_state["short"] = res["unplaced"]
            else:
                st.error(res["error"])

        blocks = store.load_schedule()
        if blocks:
            c1, c2, c3 = st.columns(3)
            c1.metric("Study blocks", len(blocks))
            c2.metric("Study days", len({b["date"] for b in blocks}))
            c3.metric("Blocks not placed", sum(u["short"] for u in st.session_state.get("short", [])))
            for u in st.session_state.get("short", []):
                st.warning(f"{u['subject']} '{u['label']}': {u['short']} of {u['total']} blocks did not fit")

            df = pd.DataFrame(blocks)
            df["Week"] = df["date"].map(lambda d: (date.fromisoformat(d) - sem_start).days // 7 + 1)
            pick = st.selectbox("Show", ["All weeks"] + [f"Week {w}" for w in sorted(df["Week"].unique())])
            if pick != "All weeks":
                df = df[df["Week"] == int(pick.split()[1])]
            view = pd.DataFrame({
                "Date": df["date"].map(lambda d: f"{datetime.fromisoformat(d):%a %d %b}"),
                "Time": df["start"] + "-" + df["end"],
                "Subject": df["subject"], "Task": df["label"],
                "Pinned": df["pinned"].map(lambda p: "yes" if p else ""),
            })
            st.dataframe(view, hide_index=True)

            ics = build_ics(blocks, cfg.get("timezone", DEFAULT_TZ))
            OUT.mkdir(exist_ok=True)
            (OUT / "study_schedule.ics").write_text(ics, encoding="utf-8", newline="")
            st.download_button("Download .ics (Google Calendar)", ics,
                               file_name="study_schedule.ics", mime="text/calendar")

# ---------- tab 4: chat ----------
with tab_chat:
    model = cfg.get("model", agent.DEFAULT_MODEL)
    if "chat" not in st.session_state or st.session_state["chat"].day != date.today():
        st.session_state["chat"] = agent.Chat(model)  # new day -> fresh date in the prompt
    chat = st.session_state["chat"]

    if not store.load_schedule():
        st.info("Generate a schedule in tab 3 first.")
    st.caption(f"Model: {model}. Changes ask for your approval before they are applied.")

    for role, text in chat.display:
        if role == "tool":
            st.caption(f"tool: {text}")
        else:
            st.chat_message(role).write(text)

    if chat.pending:
        st.warning(chat.pending["text"])
        ok_col, no_col, _ = st.columns([1, 1, 4])
        if ok_col.button("Apply", type="primary"):
            chat.resolve(True)
            st.rerun()
        if no_col.button("Cancel"):
            chat.resolve(False)
            st.rerun()

    prompt = st.chat_input("e.g. move my first DES3103 block in week 6 to Saturday 2pm",
                           disabled=chat.pending is not None)
    if prompt:
        with st.spinner("Thinking..."):
            chat.send(prompt)
        st.rerun()  # redraw so the Schedule tab shows any change

    u_col, c_col, _ = st.columns([1, 1, 4])
    if u_col.button("Undo last change"):
        res = store.undo()
        chat.note("Undo: " + (res.get("message") or res.get("error")))
        st.rerun()
    if c_col.button("Clear chat"):
        st.session_state["chat"] = agent.Chat(model)
        st.rerun()
