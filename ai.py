"""Study notes, translation and quizzes with a local Ollama model (free, offline)."""
import base64
import json
import re

import requests

OLLAMA = "http://localhost:11434"
CHUNK_CHARS = 12000

LANGS = {"English": "English", "Українська": "Ukrainian (українська мова)", "Русский": "Russian (русский язык)"}
# How a lecture is named in source citations, per notes language
LECTURE_LABEL = {"English": "Lecture", "Українська": "Лекція", "Русский": "Лекция"}
# Marker for facts in chat answers that are not from the lecture
NOT_FROM_LECTURE = {"English": "(not from the lecture)", "Українська": "(не з лекції)", "Русский": "(не из лекции)"}


class AIError(Exception):
    pass


def installed_models() -> list[str]:
    try:
        r = requests.get(f"{OLLAMA}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except requests.RequestException:
        return []


def _chat(model: str, prompt: str, json_mode: bool = False, images: list[bytes] | None = None,
          num_ctx: int = 16384) -> str:
    message = {"role": "user", "content": prompt}
    if images:
        message["images"] = [base64.b64encode(img).decode() for img in images]
    payload = {
        "model": model,
        "messages": [message],
        "stream": False,
        "think": False,
        "options": {"num_ctx": num_ctx, "temperature": 0.3},
    }
    if json_mode:
        payload["format"] = "json"
    try:
        r = requests.post(f"{OLLAMA}/api/chat", json=payload, timeout=900)
    except requests.ConnectionError:
        raise AIError("Ollama is not running. Open the Ollama app or run `ollama serve`.")
    if r.status_code == 400 and "think" in r.text:
        payload.pop("think")  # the model has no thinking mode
        r = requests.post(f"{OLLAMA}/api/chat", json=payload, timeout=900)
    if json_mode and r.status_code >= 400 and "structured output" in r.text:
        # the model has no strict JSON mode (e.g. gemma4) — ask for JSON as text and cut it out
        payload.pop("format")
        payload["messages"][-1]["content"] += "\n\nOutput ONLY the JSON object, nothing else."
        r = requests.post(f"{OLLAMA}/api/chat", json=payload, timeout=900)
        r.raise_for_status()
        return _extract_json(r.json()["message"]["content"])
    if r.status_code >= 400 and images:
        raise AIError(f"Model {model} can't read images — pick another one under Advanced.")
    r.raise_for_status()
    return r.json()["message"]["content"]


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a reply like ```json {...} ``` or one with text around it."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def read_image(model: str, image: bytes) -> str:
    """Transcribe text and formulas from an image (a page, a slide, a screenshot of a problem)."""
    return _chat(model, """Transcribe ALL text from this image exactly, in its original language.
Write every math formula in LaTeX between $...$ (or $$...$$ for display formulas).
If there is a chart, graph or diagram, briefly describe what it shows.
Output only the transcription, no comments.""", images=[image])


def lang_tail(lang: str) -> str:
    """Language reminder at the end of a prompt — otherwise the model answers in the material's language."""
    return (f"\n\nIMPORTANT: write your ENTIRE answer in {LANGS[lang]} only, "
            f"even if the material above is in another language.")


def _chunks(text: str) -> list[str]:
    chunks, current = [], ""
    for para in text.split("\n\n"):
        if len(current) + len(para) > CHUNK_CHARS and current:
            chunks.append(current)
            current = ""
        current += para + "\n\n"
    if current.strip():
        chunks.append(current)
    return chunks


def make_summary(model: str, text: str, lang: str, progress=None) -> str:
    language = LANGS[lang]
    chunks = _chunks(text)
    parts = []
    for i, chunk in enumerate(chunks, 1):
        if progress:
            progress(i, len(chunks))
        parts.append(_chat(model, f"""You are helping a university student study.
Write a detailed, well-structured study summary of the lecture material below.
Write ONLY in {language}, translating everything from the original language.
Rules:
- Use Markdown: headings (##), bullet points, **bold** key terms.
- Keep all definitions, formulas, dates, names, numbers and examples.
- Write math formulas in LaTeX: $...$ inline, $$...$$ for display formulas.
- Use ONLY information from the material. Do not add facts that are not in it.
- For important terms, put the original term in parentheses after the translation.
- Do not add an introduction or closing remarks — output only the summary.

MATERIAL:
{chunk}{lang_tail(lang)}"""))
    return "\n\n".join(parts)


def make_quiz(model: str, summary: str, lang: str, n: int = 10) -> list[dict]:
    language = LANGS[lang]
    raw = _chat(model, f"""Based on the study notes below, create a test of {n} multiple-choice questions
that check understanding (not only memorization). Write everything in {language}.
Each question has exactly 4 options and exactly one correct answer.
Return JSON in this exact format:
{{"questions": [{{"question": "...", "options": ["...", "...", "...", "..."], "correct": 0, "explanation": "..."}}]}}
where "correct" is the index (0-3) of the correct option.

NOTES:
{summary[:CHUNK_CHARS * 2]}{lang_tail(lang)}""", json_mode=True)
    try:
        questions = json.loads(raw)["questions"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise AIError("The AI returned the quiz in a wrong format — try again.")
    return [q for q in questions
            if isinstance(q.get("options"), list) and len(q["options"]) >= 2
            and isinstance(q.get("correct"), int) and 0 <= q["correct"] < len(q["options"])]


QUICK_ACTIONS = {
    "quick_detail": "Make a much more detailed and extended summary of the whole lecture: explain every concept "
                   "step by step, add examples from the material, and explain why each idea matters.",
    "quick_seminar": "Help me prepare for the seminar on this topic: list the key questions likely to be discussed, "
                    "give a full answer to each, typical mistakes, and 3–5 tricky questions I could ask myself.",
    "quick_simple": "Explain the main ideas of this lecture in very simple words, with everyday analogies, "
               "as if to a first-year student who missed the lecture.",
}


def chat_stream(model: str, history: list[dict], summary: str, source: str, lang: str):
    """Chat about the notes: yields the answer piece by piece (for live output)."""
    system = f"""You are a friendly study assistant for a university student.
Always answer in {LANGS[lang]}. Use Markdown, math in LaTeX ($...$).
Base your answers on the lecture material and the summary below. Every fact, example or number that is NOT
in the material must be marked right after it with "{NOT_FROM_LECTURE[lang]}".
Do not greet, do not praise the question, no filler phrases at the start or end — go straight to the content,
so the answer can be pasted directly into study notes.

=== SUMMARY ===
{summary}

=== ORIGINAL LECTURE MATERIAL ===
{source[:60000] if source else "(not available — use the summary)"}"""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + history[:-1]
                    + [{**history[-1], "content": history[-1]["content"] + lang_tail(lang)}],
        "stream": True,
        "think": False,
        "options": {"num_ctx": 32768, "temperature": 0.5},
    }
    try:
        r = requests.post(f"{OLLAMA}/api/chat", json=payload, stream=True, timeout=900)
    except requests.ConnectionError:
        raise AIError("Ollama is not running. Open the Ollama app or run `ollama serve`.")
    if r.status_code == 400 and "think" in r.text:
        payload.pop("think")
        r = requests.post(f"{OLLAMA}/api/chat", json=payload, stream=True, timeout=900)
    r.raise_for_status()
    for line in r.iter_lines():
        if line:
            chunk = json.loads(line)
            yield chunk.get("message", {}).get("content", "")
            if chunk.get("done"):
                break


SECTION_TITLES = {
    "English": {"🧩": "🧩 Examples and cases from the materials", "➕": "➕ Additions (not from the course materials)",
                "✅": "✅ Pre-seminar checklist"},
    "Українська": {"🧩": "🧩 Приклади та кейси з матеріалів", "➕": "➕ Доповнення (не з матеріалів курсу)",
                   "✅": "✅ Чекліст перед семінаром"},
    "Русский": {"🧩": "🧩 Примеры и кейсы из материалов", "➕": "➕ Дополнения (не из материалов курса)",
                "✅": "✅ Чек-лист перед семинаром"},
}
NOTES_CACHE = None  # folder for cached notes (set by app.py)
BOOK_CHUNK = 24000


def _notes(model: str, topic: str, source: str, text: str, lang: str, chunk_size: int, progress=None) -> str:
    """Detailed notes on one source, chunk by chunk (cached)."""
    import hashlib
    parts = []
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    for i, chunk in enumerate(chunks, 1):
        if progress:
            progress(source, i, len(chunks))
        key = hashlib.sha1(f"{model}|{lang}|{topic}|{chunk}".encode()).hexdigest()
        cached = NOTES_CACHE / f"notes_{key}.md" if NOTES_CACHE else None
        if cached and cached.exists():
            parts.append(cached.read_text())
            continue
        note = _chat(model, f"""You are preparing a student for a university seminar on "{topic}".
Below is part of the source "{source}". Write detailed study notes of everything in it that is relevant
to the seminar topic: definitions, key ideas, models, graphs (described in words), examples, numbers, cases.
Write ONLY in {LANGS[lang]}. Keep original terms in parentheses. Put the page number in brackets after
facts when it is shown in the text, like [p. 12]. Use ONLY information from this text. Markdown bullets, no intro.

TEXT:
{chunk}{lang_tail(lang)}""")
        if cached:
            cached.write_text(note)
        parts.append(note)
    return "\n".join(parts)


def make_seminar_prep(model: str, seminar: dict, sources: list[tuple[str, str]], lang: str,
                      extras: bool = True, progress=None) -> tuple[str, str]:
    """Seminar prep notes. sources: [(source name, text)]. Returns (prep document, raw notes)."""
    topic = seminar["title"]
    notes = []
    for name, text in sources:
        size = BOOK_CHUNK if len(text) > 40000 else CHUNK_CHARS
        notes.append(f"### SOURCE: {name}\n{_notes(model, topic, name, text, lang, size, progress)}")
    all_notes = "\n\n".join(notes)[:90000]
    language = LANGS[lang]
    context = f"""Seminar {seminar['number']}: "{topic}".
Syllabus entry for this seminar: {seminar.get('syllabus_seminar') or '—'}
Syllabus entry for the related lecture (contains the official learning outcomes): {seminar.get('syllabus_lecture') or '—'}
Seminar activities: {seminar.get('activities') or '—'}"""
    rules = f"""Write ONLY in {language}, Markdown, LaTeX for math. Translate headings into {language}.
Use ONLY information from the NOTES below. After facts cite the source in brackets, e.g. ({LECTURE_LABEL[lang]}), (Mankiw, p. 12),
(McConnell, p. 30). No introduction or closing remarks."""

    steps = [
        ("sem_step_notes", ["🎯", "📖"], f"""Write the first part of a seminar preparation document with exactly two sections:
## 🎯 Що треба знати до семінару
The official learning outcomes from the syllabus entry (translated), each with one sentence on what exactly to know.
Do not invent outcomes.
## 📖 Конспект за матеріалами курсу
A LONG, DETAILED and complete study summary of ALL the notes, organized into numbered sub-topics (###).
Cover every concept, definition, model, graph, example and number from the notes — do not skip or merge topics.
Explain each idea in 2–5 sentences, not one line. This section should be long."""),
        ("sem_step_questions", ["💬", "⚠️"], """Write two sections of a seminar preparation document:
## 💬 Питання для обговорення — з відповідями
10 likely seminar discussion questions. For EACH question write a full answer of 4–8 sentences based on the notes,
with an example from the notes where possible. Format: **1. Question?** then the answer as a paragraph.
## ⚠️ Типові помилки
5–8 common misconceptions about these topics and the correct understanding (based on the notes)."""),
        ("sem_step_examples", ["🧩"], """Write one section of a seminar preparation document:
## 🧩 Приклади та кейси з матеріалів
6–10 examples and cases that appear IN THE NOTES (cite the source of each), and for each one explain
how to analyse it at the seminar. Do not add examples that are not in the notes."""),
    ]
    if extras:
        steps.append(("sem_step_extras", ["➕"], """Write one section of a seminar preparation document:
## ➕ Доповнення (не з матеріалів курсу)
6–10 items of useful knowledge that is NOT in the notes and will help at the seminar: real-world examples
(Ukraine and the world), well-known cases, data, links to other topics. For each item say which concept from
the notes it illustrates. Only facts you are certain are correct. Never contradict or replace the course
materials — only complement them. Here (and only here) you may use knowledge outside the notes.
Do NOT cite the lecture, Mankiw, McConnell or any course source in this section — none of it comes from them.
Prefer fewer items over uncertain ones; never invent terms, effects or numbers."""))
    steps.append(("sem_step_checklist", ["✅"], """Write one section of a seminar preparation document:
## ✅ Чекліст перед семінаром
8–12 short items in the form "I can explain …" (in the notes language), covering the key concepts from the notes."""))

    parts = []
    for i, (label, headings, task) in enumerate(steps, 1):
        if progress:
            progress(label, i, len(steps))
        prompt = f"""NOTES (made from the course materials):
{all_notes}

=== CONTEXT ===
{context}

=== YOUR TASK ===
{task}

{rules}
Output the section text itself — not comments about it, not a review, no questions to the user.
Start directly with "##".{lang_tail(lang)}"""
        text = ""
        for _attempt in range(3):
            text = _chat(model, prompt, num_ctx=32768).strip()
            text = text[text.find("##"):] if "##" in text else text  # drop any intro
            if all(h in text for h in headings):
                break
        if len(headings) == 1:
            # single-heading sections always get the proper title
            body = text.split("\n", 1)[1] if text.startswith("##") else text
            if headings == ["➕"]:
                # additions must not cite course materials — this knowledge is not from them
                body = re.sub(r"\s*\((?:[^()]*?(?:Лекц|Lecture|Mankiw|McConnell|p\.\s*\d)[^()]*?)\)", "", body)
            text = f"## {SECTION_TITLES[lang][headings[0]]}\n\n{body.strip()}"
        parts.append(text)
    return "\n\n".join(parts), all_notes
