"""Phase 3: build a study schedule from data/*.json + config.json.

Run:  uv run python src/scheduler.py
"""
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Task:
    subject: str
    label: str
    blocks: int          # study blocks still to place
    start: date          # earliest day allowed
    end: date            # last day allowed (deadline)
    total: int = 0

    def __post_init__(self):
        self.total = self.blocks


def week_start(sem_start: date, week: int) -> date:
    return sem_start + timedelta(days=7 * (week - 1))


def build_tasks(proformas: list[dict], cfg: dict) -> list[Task]:
    sem_start = date.fromisoformat(cfg["semester"]["start_date"])
    n_weeks = cfg["semester"]["weeks"]
    bm = cfg["preferences"]["block_minutes"]
    to_blocks = lambda hours: math.ceil(hours * 60 / bm)
    tasks: list[Task] = []

    for p in proformas:
        code = p["code"]
        # 1) weekly study from the proforma's SLT hours (must happen within that week)
        for w in p["weeks"]:
            if not w["slt_hours"]:
                continue
            title = w["title"][:70]
            ws = week_start(sem_start, w["week"])
            blocks = to_blocks(w["slt_hours"])
            pref = cfg["preferences"]
            start = ws
            if blocks >= pref.get("heavy_week_blocks", 10):  # heavy week: may study ahead
                start = max(sem_start, ws - timedelta(days=pref.get("lookahead_days", 7)))
            tasks.append(Task(code, f"Week {w['week']}: {title}", blocks, start, ws + timedelta(days=6)))

        # 2) assessment prep: before the date if known, otherwise inside default weeks
        for a in p["assessments"]:
            conf = cfg.get("assessments", {}).get(code, {}).get(a["name"], {})
            hours = conf.get("prep_hours", round(a["weight"] * 0.3))
            if conf.get("date"):
                d = date.fromisoformat(conf["date"])
                s, e = max(sem_start, d - timedelta(days=13)), d - timedelta(days=1)
            else:
                w1, w2 = conf.get("default_weeks", [n_weeks - 2, n_weeks])
                s, e = week_start(sem_start, w1), week_start(sem_start, w2) + timedelta(days=6)
            tasks.append(Task(code, f"{a['name']} prep", to_blocks(hours), s, e))
    return tasks


def day_cells(day: date, cfg: dict) -> list[tuple[int, int]]:
    """Free study cells (start, end minutes) for one day."""
    pref = cfg["preferences"]
    name = DAYS[day.weekday()]
    win_s, win_e = to_min(pref["study_window"]["start"]), to_min(pref["study_window"]["end"])
    if name == pref.get("free_evening"):
        win_e = min(win_e, to_min(pref.get("free_evening_starts", "18:00")))
    busy = [(to_min(b["start"]), to_min(b["end"]))
            for b in cfg["week"][name] if b["type"] in ("meal", "class")]

    # free intervals = window minus busy
    free, cur = [], win_s
    for s, e in sorted(busy):
        if e <= cur or s >= win_e:
            continue
        if s > cur:
            free.append((cur, min(s, win_e)))
        cur = max(cur, e)
    if cur < win_e:
        free.append((cur, win_e))

    bm, br = pref["block_minutes"], pref["break_minutes"]
    cells = []
    for a, b in free:
        t = a
        while t + bm <= b:
            cells.append((t, t + bm))
            t += bm + br
    return cells


def spread(cells: list, n: int) -> list:
    """Pick n cells spread across the day (not all back-to-back)."""
    if n >= len(cells):
        return cells
    if n == 1:
        return [cells[len(cells) // 2]]
    return [cells[round(i * (len(cells) - 1) / (n - 1))] for i in range(n)]


def schedule(tasks: list[Task], cfg: dict, pins: list[dict] | None = None,
             blocked: set[str] | None = None) -> list[dict]:
    """Greedy planner.

    pins:    fixed blocks (moved/added by the user) that must stay where they are.
             Pinned study blocks count toward their task; every pin occupies its time.
    blocked: ISO dates with no study at all.
    """
    pref = cfg["preferences"]
    sem_start = date.fromisoformat(cfg["semester"]["start_date"])
    n_days = cfg["semester"]["weeks"] * 7
    out = []
    plan_from = date.fromisoformat(cfg["semester"].get("plan_from", date.today().isoformat()))
    pins, blocked = pins or [], blocked or set()

    # pinned study blocks already cover part of their task's demand
    by_key = {(t.subject, t.label): t for t in tasks}
    for p in pins:
        if p.get("kind", "study") == "study":
            t = by_key.get((p["subject"], p["label"]))
            if t and t.blocks > 0:
                t.blocks -= 1

    for i in range(n_days):
        day = sem_start + timedelta(days=i)
        if day < plan_from:
            continue  # don't schedule the past
        iso = day.isoformat()
        day_pins = [p for p in pins if p["date"] == iso]
        taken = [(to_min(p["start"]), to_min(p["end"])) for p in day_pins]

        cells = [] if iso in blocked else day_cells(day, cfg)
        cells = [c for c in cells if not any(c[0] < e and s < c[1] for s, e in taken)]

        cap = pref["max_study_hours_per_day"] * 60 // pref["block_minutes"]
        if DAYS[day.weekday()] == pref.get("light_day"):
            cap = min(cap, pref.get("light_day_max_blocks", cap))
        cap -= sum(1 for p in day_pins if p.get("kind", "study") == "study")
        cap = max(0, min(cap, len(cells)))

        active = [t for t in tasks if t.blocks > 0 and t.start <= day <= t.end]
        # pace: remaining blocks / days left in window; earliest deadline first
        want = []
        for t in active:
            days_left = (t.end - day).days + 1
            want.append((t.blocks / days_left, t.end, t, math.ceil(t.blocks / days_left)))
        want.sort(key=lambda x: (x[1], -x[0]))  # earliest deadline first, then most urgent

        chosen = []
        for _, _, t, k in want:
            for _ in range(k):
                if len(chosen) < cap and t.blocks > 0:
                    chosen.append(t)
                    t.blocks -= 1
        for t, (s, e) in zip(chosen, spread(cells, len(chosen))):
            out.append({"date": iso, "start": to_hhmm(s), "end": to_hhmm(e),
                        "subject": t.subject, "label": t.label,
                        "pinned": False, "kind": "study"})
        for p in day_pins:
            out.append({"date": iso, "start": p["start"], "end": p["end"],
                        "subject": p["subject"], "label": p["label"],
                        "pinned": True, "kind": p.get("kind", "study")})
    out.sort(key=lambda r: (r["date"], r["start"]))
    return out


def main():
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    proformas = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(Path("data").glob("*.json"))]
    tasks = build_tasks(proformas, cfg)
    result = schedule(tasks, cfg)

    Path("output").mkdir(exist_ok=True)
    Path("output/schedule.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    # console summary
    cur = None
    for r in result:
        if r["date"] != cur:
            cur = r["date"]
            print(f"\n{datetime.fromisoformat(cur):%a %d %b}")
        print(f"  {r['start']}-{r['end']}  {r['subject']}  {r['label']}")

    short = [t for t in tasks if t.blocks > 0]
    print(f"\nScheduled {len(result)} blocks across {len({r['date'] for r in result})} days.")
    for t in short:
        print(f"WARNING: {t.subject} '{t.label}' short by {t.blocks} of {t.total} blocks")


if __name__ == "__main__":
    main()
