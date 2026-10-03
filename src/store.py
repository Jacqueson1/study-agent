"""Phase 6, step 1: schedule editing (no LLM involved).

Everything the chat layer can do lives here as plain functions, so it can be
tested on its own. Each function returns {"ok": True, ...} or {"ok": False, "error": "..."}.

Files:
  output/schedule.json  current schedule (each block has an id, pinned flag, kind)
  pins.json             user-fixed blocks + blocked days (kept when re-planning)
  output/undo.json      previous states for undo
"""
import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from scheduler import DAYS, build_tasks, schedule, to_hhmm, to_min

CONFIG, DATA, OUT = Path("config.json"), Path("data"), Path("output")
SCHEDULE, PINS, UNDO = OUT / "schedule.json", Path("pins.json"), OUT / "undo.json"
MAX_UNDO = 20


# ---------- storage ----------
def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _proformas() -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(DATA.glob("*.json"))]


def load_schedule() -> list[dict]:
    blocks = _read(SCHEDULE, [])
    for b in blocks:  # files from before Phase 6 lack these fields
        b.setdefault("pinned", False)
        b.setdefault("kind", "study")
    if blocks and any("id" not in b for b in blocks):
        _assign_ids(blocks)
    return blocks


def _state() -> dict:
    return _read(PINS, {"pins": [], "blocked_days": []})


def _assign_ids(blocks: list[dict]) -> list[dict]:
    blocks.sort(key=lambda b: (b["date"], b["start"]))
    for n, b in enumerate(blocks, 1):
        b["id"] = f"B{n:03d}"
    return blocks


def _save(blocks: list[dict], state: dict) -> None:
    _write(SCHEDULE, _assign_ids(blocks))
    _write(PINS, state)


def _push_undo() -> None:
    hist = _read(UNDO, [])
    hist.append({"schedule": load_schedule(), "state": _state()})
    _write(UNDO, hist[-MAX_UNDO:])


def _key(b: dict) -> tuple:
    return (b["date"], b["start"], b["subject"], b["label"])


def _find(blocks: list[dict], block_id: str) -> dict | None:
    return next((b for b in blocks if b["id"].upper() == str(block_id).upper()), None)


def _pin_record(b: dict) -> dict:
    return {k: b[k] for k in ("date", "start", "end", "subject", "label", "kind")}


# ---------- validation ----------
def _parse_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def _parse_time(text: str) -> int | None:
    try:
        h, m = str(text).split(":")
        v = int(h) * 60 + int(m)
        return v if 0 <= v <= 1440 else None
    except (ValueError, TypeError):
        return None


def _check_slot(cfg: dict, blocks: list[dict], state: dict, day: date, s: int, e: int,
                ignore_id: str | None = None) -> str | None:
    """Return an error message if the slot is not usable, else None."""
    sem_start = date.fromisoformat(cfg["semester"]["start_date"])
    sem_end = sem_start + timedelta(days=cfg["semester"]["weeks"] * 7 - 1)
    if not sem_start <= day <= sem_end:
        return f"{day} is outside the semester ({sem_start} to {sem_end})."
    if day < date.today():
        return f"{day} is in the past."
    if day.isoformat() in state["blocked_days"]:
        return f"{day} is a blocked day. Unblock it first."
    if s >= e:
        return "End time must be after start time."
    sleep_end = to_min(cfg["preferences"].get("sleep", {}).get("end", "07:30"))
    if s < sleep_end or e > 1440:
        return f"Time must be between {to_hhmm(sleep_end)} and 24:00 (sleep time)."
    for c in cfg["week"][DAYS[day.weekday()]]:
        if c["type"] in ("meal", "class") and s < to_min(c["end"]) and to_min(c["start"]) < e:
            return f"Clashes with {c['name']} ({c['start']}-{c['end']})."
    for b in blocks:
        if b["id"] != ignore_id and b["date"] == day.isoformat() \
                and s < to_min(b["end"]) and to_min(b["start"]) < e:
            return f"Clashes with {b['id']} ({b['subject']} {b['start']}-{b['end']})."
    return None


def _fmt(b: dict) -> dict:
    d = date.fromisoformat(b["date"])
    return {"id": b["id"], "date": b["date"], "weekday": DAYS[d.weekday()],
            "time": f"{b['start']}-{b['end']}", "subject": b["subject"],
            "label": b["label"], "pinned": b["pinned"], "kind": b["kind"]}


# ---------- read ----------
def get_schedule(day: str | None = None, week: int | None = None,
                 subject: str | None = None) -> dict:
    """List blocks. Filter by ISO date, semester week number, or subject code."""
    cfg, blocks = _cfg(), load_schedule()
    if not blocks:
        return {"ok": False, "error": "No schedule yet. Generate one first."}
    sem_start = date.fromisoformat(cfg["semester"]["start_date"])
    out = []
    for b in blocks:
        d = date.fromisoformat(b["date"])
        if day and b["date"] != day:
            continue
        if week and (d - sem_start).days // 7 + 1 != int(week):
            continue
        if subject and b["subject"].lower() != subject.lower():
            continue
        out.append(_fmt(b))
    return {"ok": True, "count": len(out), "blocks": out}


# ---------- edit ----------
def move_block(block_id: str, new_date: str, new_start: str, dry_run: bool = False) -> dict:
    """Move one block to another day/time (keeps its length) and pin it."""
    cfg, blocks, state = _cfg(), load_schedule(), _state()
    b = _find(blocks, block_id)
    if not b:
        return {"ok": False, "error": f"No block with id {block_id}."}
    day, s = _parse_day(new_date), _parse_time(new_start)
    if day is None or s is None:
        return {"ok": False, "error": "Use date YYYY-MM-DD and time HH:MM."}
    e = s + (to_min(b["end"]) - to_min(b["start"]))
    err = _check_slot(cfg, blocks, state, day, s, e, ignore_id=b["id"])
    if err:
        return {"ok": False, "error": err}
    msg = (f"Move {b['subject']} '{b['label']}' from {b['date']} {b['start']} "
           f"to {day} {to_hhmm(s)}-{to_hhmm(e)} (pinned).")
    if dry_run:
        return {"ok": True, "preview": msg}
    _push_undo()
    state["pins"] = [p for p in state["pins"] if _key(p) != _key(b)]
    b.update(date=day.isoformat(), start=to_hhmm(s), end=to_hhmm(e), pinned=True)
    state["pins"].append(_pin_record(b))
    _save(blocks, state)
    return {"ok": True, "message": msg}


def add_event(day: str, start: str, end: str, title: str, dry_run: bool = False) -> dict:
    """Add a personal event (e.g. a meeting). It is pinned and study is planned around it."""
    cfg, blocks, state = _cfg(), load_schedule(), _state()
    d, s, e = _parse_day(day), _parse_time(start), _parse_time(end)
    if d is None or s is None or e is None or not title.strip():
        return {"ok": False, "error": "Need date YYYY-MM-DD, start/end HH:MM and a title."}
    err = _check_slot(cfg, blocks, state, d, s, e)
    if err:
        return {"ok": False, "error": err}
    msg = f"Add '{title}' on {d} {to_hhmm(s)}-{to_hhmm(e)}."
    if dry_run:
        return {"ok": True, "preview": msg}
    _push_undo()
    b = {"date": d.isoformat(), "start": to_hhmm(s), "end": to_hhmm(e), "subject": "Personal",
         "label": title.strip(), "pinned": True, "kind": "event"}
    blocks.append(b)
    state["pins"].append(_pin_record(b))
    _save(blocks, state)
    return {"ok": True, "message": msg}


def delete_event(block_id: str) -> dict:
    """Delete a personal event added with add_event (study blocks are moved, not deleted)."""
    blocks, state = load_schedule(), _state()
    b = _find(blocks, block_id)
    if not b or b["kind"] != "event":
        return {"ok": False, "error": "Only personal events can be deleted. Use move_block for study blocks."}
    _push_undo()
    blocks.remove(b)
    state["pins"] = [p for p in state["pins"] if _key(p) != _key(b)]
    _save(blocks, state)
    return {"ok": True, "message": f"Deleted '{b['label']}'."}


def pin_block(block_id: str) -> dict:
    blocks, state = load_schedule(), _state()
    b = _find(blocks, block_id)
    if not b:
        return {"ok": False, "error": f"No block with id {block_id}."}
    _push_undo()
    b["pinned"] = True
    if all(_key(p) != _key(b) for p in state["pins"]):
        state["pins"].append(_pin_record(b))
    _save(blocks, state)
    return {"ok": True, "message": f"{block_id} pinned: re-planning will keep it here."}


def unpin_block(block_id: str) -> dict:
    blocks, state = load_schedule(), _state()
    b = _find(blocks, block_id)
    if not b:
        return {"ok": False, "error": f"No block with id {block_id}."}
    if b["kind"] == "event":
        return {"ok": False, "error": "Personal events stay pinned. Delete it instead."}
    _push_undo()
    b["pinned"] = False
    state["pins"] = [p for p in state["pins"] if _key(p) != _key(b)]
    _save(blocks, state)
    return {"ok": True, "message": f"{block_id} unpinned: re-planning may move it."}


def clear_pins() -> dict:
    """Unpin all study blocks (personal events and blocked days stay)."""
    blocks, state = load_schedule(), _state()
    _push_undo()
    state["pins"] = [p for p in state["pins"] if p["kind"] == "event"]
    for b in blocks:
        if b["kind"] == "study":
            b["pinned"] = False
    _save(blocks, state)
    return {"ok": True, "message": "All study pins cleared."}


def block_day(day: str) -> dict:
    """No study on this day. Its study blocks are re-planned onto other days."""
    d = _parse_day(day)
    if d is None:
        return {"ok": False, "error": "Use date YYYY-MM-DD."}
    state = _state()
    _push_undo()
    if d.isoformat() not in state["blocked_days"]:
        state["blocked_days"].append(d.isoformat())
    state["pins"] = [p for p in state["pins"] if not (p["date"] == d.isoformat() and p["kind"] == "study")]
    _write(PINS, state)
    res = replan(_record_undo=False)
    res["message"] = f"{d} blocked. " + res.get("message", "")
    return res


def unblock_day(day: str) -> dict:
    state = _state()
    if day not in state["blocked_days"]:
        return {"ok": False, "error": f"{day} is not blocked."}
    _push_undo()
    state["blocked_days"].remove(day)
    _write(PINS, state)
    res = replan(_record_undo=False)
    res["message"] = f"{day} unblocked. " + res.get("message", "")
    return res


def replan(plan_from: date | None = None, _record_undo: bool = True) -> dict:
    """Rebuild the schedule from today. Pinned blocks and blocked days are kept."""
    cfg, state = _cfg(), _state()
    if not _proformas():
        return {"ok": False, "error": "No proformas loaded."}
    if _record_undo:
        _push_undo()
    plan_from = plan_from or date.today()
    run_cfg = copy.deepcopy(cfg)
    run_cfg["semester"]["plan_from"] = plan_from.isoformat()
    state["pins"] = [p for p in state["pins"] if p["date"] >= plan_from.isoformat()]

    tasks = build_tasks(_proformas(), run_cfg)
    blocks = schedule(tasks, run_cfg, state["pins"], set(state["blocked_days"]))
    _save(blocks, state)
    unplaced = [{"subject": t.subject, "label": t.label, "short": t.blocks, "total": t.total}
                for t in tasks if t.blocks > 0]
    n_pin = sum(1 for b in blocks if b["pinned"])
    return {"ok": True, "blocks": blocks, "unplaced": unplaced,
            "message": f"Planned {len(blocks)} blocks ({n_pin} pinned); "
                       f"{sum(u['short'] for u in unplaced)} did not fit."}


def undo() -> dict:
    hist = _read(UNDO, [])
    if not hist:
        return {"ok": False, "error": "Nothing to undo."}
    last = hist.pop()
    _write(UNDO, hist)
    _write(SCHEDULE, last["schedule"])
    _write(PINS, last["state"])
    return {"ok": True, "message": "Last change undone."}
