import json
import sys
import unicodedata
from pathlib import Path

import pdfplumber
from pydantic import BaseModel


class Week(BaseModel):
    week: int
    title: str  # English topic title
    content: str  # bilingual text (Malay + English)
    slt_hours: int | None


class Assessment(BaseModel):
    name: str
    weight: int


class Proforma(BaseModel):
    code: str
    name: str
    credit: int
    total_slt: int
    weeks: list[Week]
    assessments: list[Assessment]


def english(text: str) -> str:
    """Bilingual cell -> English part (after ' / ')."""
    return text.split(" / ")[-1].strip()


def clean(cell) -> str:
    return (cell or "").replace("\n", " ").strip()


def english_title(page, bbox) -> str:
    """English titles are bold-italic ('BoldOblique') in the week cell."""
    if bbox is None:
        return ""
    chars = [c for c in page.crop(bbox).chars if "BoldOblique" in c["fontname"]]
    lines: dict[int, list[str]] = {}
    for c in chars:
        lines.setdefault(round(c["top"]), []).append(c["text"])
    text = " ".join("".join(lines[k]) for k in sorted(lines))
    return unicodedata.normalize("NFKC", text).strip()


def extract(pdf_path: Path) -> Proforma:
    code = name = ""
    credit = total_slt = 0
    weeks: dict[int, Week] = {}
    assessments: list[Assessment] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.find_tables():
                for r_idx, row in enumerate(table.extract()):
                    cells = [clean(c) for c in row]
                    first = cells[0] if cells else ""

                    if first.startswith("Nama Kursus"):
                        name = english(cells[1])
                    elif first.startswith("Kod Kursus"):
                        code, credit = cells[1], int(cells[3])
                    elif first.isdigit() and len(cells) > 10 and len(cells[1]) > 20:
                        # week row: cells[1] = bilingual content, last non-empty cell = SLT
                        wk = int(first)
                        slt = next((c for c in reversed(cells) if c), "")
                        weeks.setdefault(
                            wk,
                            Week(
                                week=wk,
                                title=english_title(page, table.rows[r_idx].cells[1]) or cells[1][:40],
                                content=cells[1],
                                slt_hours=int(slt) if slt.isdigit() else None,
                            ),
                        )
                    elif first.isdigit():
                        # assessment row: empty cells are noise, so keep non-empty only
                        vals = [c for c in cells if c]
                        if len(vals) >= 3 and "(" in vals[1] and vals[2].isdigit():
                            label = vals[1].split("(")[-1].rstrip(")")
                            assessments.append(Assessment(name=label, weight=int(vals[2])))
                    elif "GRAND TOTAL" in cells:
                        total_slt = int(next(c for c in reversed(cells) if c.isdigit()))

    # duplicate names (e.g. three "Group") -> "Group", "Group #2", "Group #3"
    seen: dict[str, int] = {}
    for a in assessments:
        seen[a.name] = seen.get(a.name, 0) + 1
        if seen[a.name] > 1:
            a.name = f"{a.name} #{seen[a.name]}"

    return Proforma(
        code=code, name=name, credit=credit, total_slt=total_slt,
        weeks=sorted(weeks.values(), key=lambda w: w.week),
        assessments=assessments,
    )


if __name__ == "__main__":
    pdf = Path(sys.argv[1])
    result = extract(pdf)
    out = Path("data") / f"{result.code}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(f"Saved {out}: {len(result.weeks)} weeks, {len(result.assessments)} assessments")
