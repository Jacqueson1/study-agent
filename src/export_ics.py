"""Phase 4: turn output/schedule.json into an .ics calendar file.

Run:  uv run python src/export_ics.py
Import the result into Google Calendar (Settings > Import & export).
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TZ = "Asia/Kuala_Lumpur"

VTIMEZONE_KL = [
    "BEGIN:VTIMEZONE",
    "TZID:Asia/Kuala_Lumpur",
    "BEGIN:STANDARD",
    "DTSTART:19700101T000000",
    "TZOFFSETFROM:+0800",
    "TZOFFSETTO:+0800",
    "TZNAME:MYT",
    "END:STANDARD",
    "END:VTIMEZONE",
]


def esc(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def fold(line: str) -> list[str]:
    """RFC 5545: lines longer than 75 bytes are folded."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > (75 if not out else 74):
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return [out[0]] + [" " + p for p in out[1:]]


def stamp(day: str, hhmm: str) -> str:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").strftime("%Y%m%dT%H%M%S")


def build_ics(blocks: list[dict], tz: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Study Agent//EN",
             "CALSCALE:GREGORIAN", f"X-WR-CALNAME:Study Schedule"]
    if tz == DEFAULT_TZ:
        lines += VTIMEZONE_KL

    for b in blocks:
        uid_src = f"{b['date']}{b['start']}{b['subject']}{b['label']}"
        uid = hashlib.md5(uid_src.encode()).hexdigest() + "@study-agent"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;TZID={tz}:{stamp(b['date'], b['start'])}",
            f"DTEND;TZID={tz}:{stamp(b['date'], b['end'])}",
            f"SUMMARY:{esc(b['subject'] + ' - ' + b['label'])}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Study block starts soon",
            "TRIGGER:-PT10M",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    folded = [piece for line in lines for piece in fold(line)]
    return "\r\n".join(folded) + "\r\n"


def main():
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    blocks = json.loads(Path("output/schedule.json").read_text(encoding="utf-8"))
    tz = cfg.get("timezone", DEFAULT_TZ)
    out = Path("output/study_schedule.ics")
    out.write_text(build_ics(blocks, tz), encoding="utf-8", newline="")
    print(f"Saved {out}: {len(blocks)} events ({tz})")


if __name__ == "__main__":
    main()
