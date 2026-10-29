"""Seminar prep: syllabus → topics and readings → matching lectures and textbook pages."""
import json
import re

import pymupdf

import ai


def find_syllabus(sections: list[dict]) -> dict | None:
    for s in sections:
        for m in s["modules"]:
            for f in m["files"]:
                if re.search(r"syllab|силаб|силлаб", f"{s['section']} {m['name']} {f['name']}", re.I):
                    return f
    return None


def parse_syllabus(model: str, text: str) -> list[dict]:
    """List of seminars from the syllabus (via the AI)."""
    start = text.find("Schedule")
    schedule = text[start:] if start != -1 else text
    raw = ai._chat(model, f"""Below is the schedule part of a university course syllabus.
Extract EVERY seminar (marked like "S.1", "S 2", "Seminar 3"). Ignore debates, exams and project presentations.
For each seminar return:
- "number": seminar number (int)
- "title": seminar topic exactly as written
- "lecture": the lecture number whose material is used (int or null), e.g. "Learning material of Lecture 2" -> 2
- "readings": list of assigned readings. Each item: {{"book": author surname (e.g. "Mankiw"),
   "chapters": [chapter numbers], "pages": [[from, to], ...] }}. For "Lecture material (in Moodle)" use book "Moodle".
- "activities": class activities text.
Also use the lecture rows: the lecture with the same number usually lists "Learning outcomes" — copy them
into "outcomes" (list of strings).
Return JSON: {{"seminars": [ ... ]}}

SYLLABUS:
{schedule[:30000]}""", json_mode=True)
    try:
        seminars = json.loads(raw)["seminars"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise ai.AIError("Couldn't parse the syllabus — try again.")
    seminars = sorted((s for s in seminars if isinstance(s.get("number"), int)), key=lambda s: s["number"])
    return attach_rows(seminars, text)


def syllabus_rows(text: str) -> dict[str, str]:
    """Schedule rows from the syllabus: {"L2": text of the lecture 2 row, "S1": text of the seminar 1 row, ...}."""
    marks = list(re.finditer(r"^\s*(L|S|E|D|P)\.?\s*(\d+)\.?", text, re.M))
    rows = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        key = f"{m.group(1)}{m.group(2)}"
        if key[0] in "LS" and key not in rows:
            rows[key] = " ".join(text[m.start():end].split())[:2500]
    return rows


def attach_rows(seminars: list[dict], text: str) -> list[dict]:
    """Attach the exact syllabus text (learning outcomes, readings) to each seminar."""
    rows = syllabus_rows(text)
    for sem in seminars:
        sem["syllabus_seminar"] = rows.get(f"S{sem['number']}", "")
        sem["syllabus_lecture"] = rows.get(f"L{sem.get('lecture')}", "") if sem.get("lecture") else ""
    return seminars


def lecture_title(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as d:
        return " ".join(d[0].get_text().split())[:160] if len(d) else ""


def _lecture_numbers(name: str, title: str) -> set[int]:
    """Lecture number from the file name (L6 …) and from the first slide (Lecture 5. …) — they can differ."""
    nums = {int(n) for n in re.findall(r"\bL\s*(\d+)", name)}
    nums |= {int(n) for n in re.findall(r"Lecture\s*(\d+)", title, re.I)}
    if re.search(r"Course introduction", title, re.I):
        nums.add(1)
    return nums


def match_lectures(model: str, seminar: dict, lectures: list[dict]) -> list[str]:
    """Which lecture files belong to the seminar. lectures: [{name, title}]."""
    number = seminar.get("lecture")
    candidates = [l for l in lectures if number in _lecture_numbers(l["name"], l["title"])]
    if len(candidates) <= 1:
        return [l["name"] for l in candidates]
    listing = "\n".join(f"- {l['name']} :: {l['title']}" for l in candidates)
    raw = ai._chat(model, f"""Seminar topic: "{seminar['title']}".
Candidate lecture files (file name :: text of the first slide):
{listing}
Keep a file if its topic is part of the seminar topic (a seminar can have two topics — keep files for both).
Drop files about a different topic. Return JSON {{"files": [file names to keep]}}.""", json_mode=True)
    try:
        names = set(json.loads(raw)["files"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return [l["name"] for l in candidates]
    return [l["name"] for l in candidates if l["name"] in names] or [l["name"] for l in candidates]


def book_text(data: bytes, chapters: list[int], pages: list[list[int]]) -> tuple[str, str]:
    """Text of the needed textbook chapters and pages. Returns (text, description of what was taken)."""
    if pages:
        chapters = []  # pages are more precise than chapters: "Ch.34 (p.683-684)" means only these pages
    with pymupdf.open(stream=data, filetype="pdf") as d:
        wanted, found = set(), []
        toc = d.get_toc()
        for ch in chapters or []:
            for i, (lvl, title, page) in enumerate(toc):
                if re.match(rf"\s*Chapter\s+{ch}\b", title, re.I):
                    end = next((p for l, _, p in toc[i + 1:] if l <= lvl), len(d) + 1)
                    wanted.update(range(page - 1, end - 1))
                    found.append(f"ch. {ch}")
                    break
        if pages:
            labels = {d[i].get_label(): i for i in range(len(d))}
            for rng in pages:
                if len(rng) != 2:
                    continue
                a, b = labels.get(str(rng[0])), labels.get(str(rng[1]))
                if a is not None and b is not None and 0 <= b - a < 120:
                    wanted.update(range(a, b + 1))
                    found.append(f"pp. {rng[0]}–{rng[1]}")
        text = "\n\n".join(f"--- p. {d[i].get_label() or i + 1} ---\n{d[i].get_text()}" for i in sorted(wanted))
    return text, ", ".join(found)
