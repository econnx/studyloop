"""StudyLoop: Moodle lectures → study notes → quizzes, with a local AI. Run: ./start.sh"""
import hashlib
import html
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

import ai
import i18n
import jobs
import seminar
from extract import extract_text, is_supported
from i18n import count, t
from moodle import Moodle, MoodleError, google_login_link, normalize_url, token_from_app_link

ROOT = Path(__file__).parent
DATA = ROOT / "data"
NOTES = DATA / "notes"
CONFIG = DATA / "config.json"
NOTES.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title=i18n.APP_NAME, page_icon="📚", layout="wide")
st.markdown(f"<style>{(ROOT / 'style.css').read_text()}</style>", unsafe_allow_html=True)


def load_config() -> dict:
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def save_config(cfg: dict):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    CONFIG.chmod(0o600)


def slugify(title: str) -> str:
    return re.sub(r"[^\w\-]+", "_", title, flags=re.UNICODE).strip("_")[:80] or "notes"


def list_notes() -> list[Path]:
    return sorted(NOTES.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)


def step(num: int, title: str, subtitle: str = ""):
    st.markdown(f'<div class="step"><div class="num">{num}</div><div><div class="t">{title}</div>'
                f'<div class="s">{subtitle}</div></div></div>', unsafe_allow_html=True)


def empty(icon: str, title: str, text: str):
    st.markdown(f'<div class="empty"><div class="big">{icon}</div><div class="t">{title}</div>{text}</div>',
                unsafe_allow_html=True)


def connect(url: str, token: str):
    info = Moodle(url, token).site_info()
    cfg.update(url=normalize_url(url), token=token, firstname=info.get("firstname", ""),
               site_name=info.get("sitename", ""))
    save_config(cfg)
    st.rerun()


cfg = load_config()
i18n.LANG = cfg.get("ui_lang", "en")
CACHE = DATA / "cache"
CACHE.mkdir(exist_ok=True)
ai.NOTES_CACHE = CACHE


@st.cache_data(ttl=600, show_spinner=False)
def get_courses(_url, _token):
    return Moodle(_url, _token).courses()


@st.cache_data(ttl=600, show_spinner=False)
def get_files(_url, _token, course_id):
    return Moodle(_url, _token).course_files(course_id)


def save_note(title: str, body: str, sources: list[str], source_text: str, lang: str) -> Path:
    meta = json.dumps({"date": f"{datetime.now():%d.%m.%Y}", "lang": lang, "sources": sources}, ensure_ascii=False)
    path = NOTES / f"{slugify(title)}.md"
    path.write_text(f"<!-- {meta} -->\n# {title}\n\n{body}")
    path.with_suffix(".source.txt").write_text(source_text)
    for extra in (".quiz.json", ".chat.json"):
        path.with_suffix(extra).unlink(missing_ok=True)
    return path


def download(url: str) -> bytes:
    """Download a file from Moodle (cached on disk — textbooks are big)."""
    path = CACHE / hashlib.sha1(url.encode()).hexdigest()
    if not path.exists():
        path.write_bytes(Moodle(cfg["url"], cfg["token"]).download(url))
    return path.read_bytes()


# ---------- Sidebar ----------
with st.sidebar:
    st.markdown(f'<div class="brand">{i18n.APP_NAME}<span>.</span></div>'
                f'<div class="sub">{t("tagline")}</div>', unsafe_allow_html=True)

    ui_lang = st.segmented_control(t("ui_lang"), list(i18n.UI_LANGS), format_func=i18n.UI_LANGS.get,
                                   default=i18n.LANG, selection_mode="single") or i18n.LANG
    if ui_lang != i18n.LANG:
        cfg["ui_lang"] = ui_lang
        save_config(cfg)
        st.rerun()

    default_lang = cfg.get("lang") or i18n.DEFAULT_NOTES_LANG[i18n.LANG]
    lang = st.segmented_control(t("notes_lang"), list(ai.LANGS), default=default_lang,
                                selection_mode="single") or default_lang

    models = ai.installed_models()
    model = None
    if not models:
        st.error(t("ollama_down"))
    else:
        model = cfg.get("model") if cfg.get("model") in models else models[0]

    st.markdown("&nbsp;", unsafe_allow_html=True)
    if cfg.get("token"):
        st.markdown(f'<div class="status"><span class="dot"></span><b>{t("moodle_on")}</b><br>'
                    f'<span style="color:#9C97B5">{html.escape(cfg.get("site_name", ""))}</span></div>',
                    unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="status"><span class="dot off"></span><b>{t("moodle_off")}</b></div>',
                    unsafe_allow_html=True)
        url = st.text_input(t("moodle_url"), cfg.get("url", "https://teaching.kse.org.ua"))
        st.caption(t("login_hint"))
        if st.button(t("login_btn"), type="primary", use_container_width=True):
            with st.spinner(t("login_wait")):
                try:
                    res = subprocess.run([sys.executable, str(ROOT / "google_login.py"), google_login_link(url)],
                                         capture_output=True, text=True, timeout=360)
                    if res.returncode != 0:
                        raise MoodleError(res.stderr.strip().splitlines()[-1] if res.stderr.strip()
                                          else t("window_closed"))
                    connect(url, token_from_app_link(res.stdout.strip()))
                except Exception as e:
                    st.error(t("login_failed", e=e))
        with st.expander(t("other_login")):
            token_in = st.text_input(t("token"), type="password")
            if st.button(t("token_btn"), disabled=not token_in):
                try:
                    connect(url, token_in)
                except Exception as e:
                    st.error(t("login_failed", e=e))

    with st.expander(t("advanced")):
        if models:
            model = st.selectbox(t("model"), models, index=models.index(model))
        if cfg.get("token") and st.button(t("logout")):
            cfg.pop("token", None)
            save_config(cfg)
            st.rerun()

    if lang != cfg.get("lang") or (model and model != cfg.get("model")):
        cfg.update(lang=lang, model=model)
        save_config(cfg)

# ---------- Header ----------
notes_all = list_notes()
quizzes = sum(p.with_suffix(".quiz.json").exists() for p in notes_all)
if cfg.get("token") and "firstname" not in cfg:
    try:
        cfg["firstname"] = Moodle(cfg["url"], cfg["token"]).site_info().get("firstname", "")
        save_config(cfg)
    except Exception:
        pass
name = html.escape(cfg.get("firstname") or "")
hour = datetime.now().hour
greeting = t("greet_morning") if hour < 12 else t("greet_day") if hour < 18 else t("greet_evening")
st.markdown(f"""
<div class="hero">
  <div class="kicker">{datetime.now():%d.%m.%Y}</div>
  <h1>{greeting}{', ' + name if name else ''} 👋</h1>
  <p>{t("hero", lang=i18n.LANG_IN[i18n.LANG][lang])}</p>
  <div class="chips">
    <div class="chip">📝 {count(len(notes_all), "notes")}</div>
    <div class="chip">🎯 {count(quizzes, "quizzes")}</div>
    <div class="chip">🌐 {lang}</div>
  </div>
</div>""", unsafe_allow_html=True)


@st.fragment(run_every=2)
def job_panel():
    """Background generation progress — refreshes by itself while the rest of the page stays usable."""
    job = jobs.latest()
    if not job or (job["status"] != "running" and job["seen"]):
        return
    with st.container(border=True, key="card_job"):
        if job["status"] == "running":
            minutes = int((time.time() - job["started"]) // 60)
            st.markdown(t("job_running", title=job["title"], m=minutes))
            st.progress(job["progress"], job["message"] or t("starting"))
        else:
            c1, c2 = st.columns([5, 1], vertical_alignment="center")
            if job["status"] == "done":
                c1.success(t("job_done", title=job["title"]))
            else:
                c1.error(t("job_error", title=job["title"], error=job["error"]))
            if c2.button("OK", key="job_ok", use_container_width=True):
                job["seen"] = True
                st.rerun(scope="app")
    if job["status"] != "running" and not job.get("announced"):
        job["announced"] = True
        st.rerun(scope="app")  # refresh the notes list


job_panel()
busy = jobs.running() is not None

tab_mat, tab_sem, tab_notes, tab_quiz = st.tabs([t("tab_create"), t("tab_seminar"), t("tab_notes"), t("tab_quiz")])

# ---------- Create ----------
with tab_mat:
    selected = []  # materials: (title, [(file name, function returning bytes)])

    with st.container(border=True, key="card_pick"):
        step(1, t("pick_title"), t("pick_sub"))
        if cfg.get("token"):
            try:
                with st.spinner(t("loading_courses")):
                    courses = get_courses(cfg["url"], cfg["token"])
                ids = [c["id"] for c in courses]
                default = next((i for i, c in enumerate(courses) if "Econom" in c["fullname"]), 0)
                course = st.selectbox(t("course"), courses, format_func=lambda c: c["fullname"],
                                      index=ids.index(cfg["course_id"]) if cfg.get("course_id") in ids else default)
                if course and course["id"] != cfg.get("course_id"):
                    cfg["course_id"] = course["id"]
                    save_config(cfg)
                if course:
                    with st.spinner(t("loading_files")):
                        sections = get_files(cfg["url"], cfg["token"], course["id"])
                    for section in sections:
                        modules = [(m, [f for f in m["files"] if is_supported(f["name"])]) for m in section["modules"]]
                        modules = [(m, files) for m, files in modules if files]
                        if not modules:
                            continue
                        with st.expander(f"📂 {section['section']}  ·  {len(modules)}"):
                            for i, (m, files) in enumerate(modules):
                                kinds = sorted({Path(f["name"]).suffix.lower().lstrip(".") for f in files})
                                label = f"**{m['name']}**  \n:gray[{count(len(files), 'files')} · {', '.join(kinds)}]"
                                if st.checkbox(label, key=f"cb_{course['id']}_{section['section']}_{i}"):
                                    selected.append((m["name"], [(f["name"], lambda u=f["url"]: download(u))
                                                                 for f in files]))
            except MoodleError as e:
                st.error(t("moodle_error", e=e))
        else:
            st.info(t("connect_hint"))

        uploads = st.file_uploader(t("upload"),
                                   type=["pdf", "pptx", "docx", "txt", "md", "html", "png", "jpg", "jpeg", "webp"],
                                   accept_multiple_files=True)
        for up in uploads or []:
            selected.append((up.name, [(up.name, lambda u=up: u.getvalue())]))

    with st.container(border=True, key="card_make"):
        step(2, t("make_title"), t("make_selected", n=len(selected)) if selected else t("make_none"))
        title = st.text_input(t("title"), value=selected[0][0] if len(selected) == 1 else "",
                              placeholder=t("title_ph"))
        pdf_as_images = st.toggle(t("formula_mode"))
        go = st.button(t("create_btn"), type="primary", disabled=busy or not (selected and model and title))
        if busy:
            st.caption(t("busy"))

    if go:
        def make_note(progress, model=model, lang=lang, title=title, selected=selected, pdf_as_images=pdf_as_images):
            texts, seen = [], set()
            for k, (item, files) in enumerate(selected, 1):
                progress(0.3 * k / len(selected), t("reading", item=item))
                parts = []
                for fname, fetch in files:
                    data = fetch()
                    digest = hashlib.sha1(data).hexdigest()
                    if digest in seen:
                        continue  # identical files (copies of images) are read once
                    seen.add(digest)

                    def read_image(img: bytes, item=item) -> str:
                        progress(None, t("reading_image", item=item))
                        return ai.read_image(model, img)

                    text = extract_text(fname, data, read_image=read_image, pdf_as_images=pdf_as_images)
                    if text.strip():
                        parts.append(text)
                if parts:
                    texts.append(f"# {item}\n\n" + "\n\n".join(parts))
            if not texts:
                raise ai.AIError(t("no_text"))
            summary = ai.make_summary(model, "\n\n".join(texts), lang,
                                      progress=lambda i, n: progress(0.3 + 0.7 * (i - 1) / n,
                                                                     t("summarizing", i=i, n=n)))
            save_note(title, summary, [n for n, _ in selected], "\n\n".join(texts), lang)

        jobs.start(t("job_note", title=title), make_note)
        st.rerun()


# ---------- Seminar ----------
with tab_sem:
    if not cfg.get("token"):
        empty("🎓", t("need_moodle_t"), t("need_moodle"))
    else:
        try:
            courses = get_courses(cfg["url"], cfg["token"])
            ids = [c["id"] for c in courses]
            course = st.selectbox(t("course"), courses, format_func=lambda c: c["fullname"], key="sem_course",
                                  index=ids.index(cfg["course_id"]) if cfg.get("course_id") in ids else 0)
            sections = get_files(cfg["url"], cfg["token"], course["id"])
        except MoodleError as e:
            st.error(t("moodle_error", e=e))
            st.stop()

        syl_path = DATA / f"syllabus_{course['id']}.json"
        syl_file = seminar.find_syllabus(sections)
        if not syl_path.exists():
            with st.container(border=True, key="card_syl"):
                step(1, t("syl_title"), t("syl_sub"))
                if not syl_file:
                    st.warning(t("syl_missing"))
                elif st.button(t("syl_btn"), type="primary", disabled=not model):
                    with st.spinner(t("syl_reading")):
                        try:
                            text = extract_text(syl_file["name"], download(syl_file["url"]))
                            syl_path.write_text(json.dumps(seminar.parse_syllabus(model, text),
                                                           ensure_ascii=False, indent=2))
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))
        else:
            seminars = json.loads(syl_path.read_text())
            all_files = [f for s_ in sections for m_ in s_["modules"] for f in m_["files"]]
            books = {}  # author -> textbook file
            for r in (r for s_ in seminars for r in s_.get("readings") or []):
                book = next((f for f in all_files if r["book"].lower() in f["name"].lower()), None)
                if book and r["book"].lower() != "moodle":
                    books[r["book"]] = book
            skip = {b["url"] for b in books.values()} | ({syl_file["url"]} if syl_file else set())
            lecture_files = [f for f in all_files
                             if f["name"].lower().endswith((".pdf", ".pptx")) and f["url"] not in skip]

            with st.container(border=True, key="card_sem1"):
                c1, c2 = st.columns([6, 1], vertical_alignment="bottom")
                sem = c1.selectbox(t("seminar"), seminars, key="sem_pick",
                                   format_func=lambda s_: t("seminar_n", n=s_["number"], title=s_["title"]))
                if c2.button("🔄", help=t("reread"), use_container_width=True):
                    syl_path.unlink()
                    for old_match in DATA.glob(f"match_{course['id']}_*.json"):
                        old_match.unlink()
                    st.rerun()
                if sem.get("outcomes"):
                    st.markdown(t("outcomes") + "\n" + "\n".join(f"- {o}" for o in sem["outcomes"]))
                if sem.get("activities"):
                    st.caption(t("activities", a=sem["activities"]))

            with st.container(border=True, key="card_sem2"):
                step(1, t("sem_mat"), t("sem_mat_sub"))
                match_path = DATA / f"match_{course['id']}_{sem['number']}.json"
                pool = sorted(f["url"] for f in lecture_files)
                saved = json.loads(match_path.read_text()) if match_path.exists() else {}
                if saved.get("pool") != pool and model:  # new lectures in Moodle — match again
                    with st.spinner(t("finding_lectures")):
                        lects = []
                        for f in lecture_files:
                            data = download(f["url"])
                            title_ = (seminar.lecture_title(data) if f["name"].lower().endswith(".pdf")
                                      else extract_text(f["name"], data)[:160])
                            lects.append({"name": f["name"], "title": title_})
                        saved = {"pool": pool, "files": seminar.match_lectures(model, sem, lects)}
                        match_path.write_text(json.dumps(saved, ensure_ascii=False))
                matched = saved.get("files", [])

                st.markdown(t("lectures"))
                if not matched:
                    st.warning(t("no_lecture", n=sem.get("lecture") or ""))
                lecture_label = ai.LECTURE_LABEL[lang]
                chosen = []
                for f in (f for f in lecture_files if f["name"] in matched):
                    if st.checkbox(f["name"], value=True, key=f"semlec_{course['id']}_{sem['number']}_{f['name']}"):
                        chosen.append((f"{lecture_label}: {f['name']}", f))
                with st.expander(t("other_files")):
                    for f in (f for f in lecture_files if f["name"] not in matched):
                        if st.checkbox(f["name"], key=f"semlec_{course['id']}_{sem['number']}_{f['name']}"):
                            chosen.append((f"{lecture_label}: {f['name']}", f))

                st.markdown(t("textbooks"))
                chosen_books = []
                for j, r in enumerate(sem.get("readings") or []):
                    if r["book"].lower() == "moodle":
                        continue
                    book = books.get(r["book"])
                    if not book:
                        st.caption(t("book_missing", book=r["book"]))
                        continue
                    bkey = f"book_{course['id']}_{sem['number']}_{j}"
                    if bkey not in st.session_state:
                        with st.spinner(t("opening_book", book=r["book"])):
                            st.session_state[bkey] = seminar.book_text(download(book["url"]),
                                                                       r.get("chapters"), r.get("pages"))
                    text_, what = st.session_state[bkey]
                    if not text_:
                        st.caption(t("book_no_pages", book=r["book"]))
                        continue
                    pages_n = text_.count("--- p.")
                    if st.checkbox(f"{r['book']} — {what}  ·  {t('pages_n', n=pages_n)}", value=True,
                                   key=bkey + "_cb"):
                        chosen_books.append((f"{r['book']}, {what}", text_))

            with st.container(border=True, key="card_sem3"):
                total = sum(len(t_) for _, t_ in chosen_books)
                minutes = max(1, round(-(-total // ai.BOOK_CHUNK) * 2.3 + len(chosen) * 1.8 + 15))
                step(2, t("sem_make"), t("sem_make_sub", m=minutes))
                extras = st.toggle(t("extras"), value=True)
                st.caption(t("extras_hint"))
                go_sem = st.button(t("sem_btn"), type="primary",
                                   disabled=busy or not (model and (chosen or chosen_books)))
                if busy:
                    st.caption(t("busy"))

            if go_sem:
                def make_prep(progress, model=model, lang=lang, sem=sem, chosen=chosen,
                              chosen_books=chosen_books, extras=extras):
                    sources = []
                    for name, f in chosen:
                        progress(0.0, t("reading", item=name))
                        sources.append((name, extract_text(f["name"], download(f["url"]),
                                                           read_image=lambda b: ai.read_image(model, b))))
                    sources += chosen_books
                    total_chunks = sum(-(-len(t_) // (ai.BOOK_CHUNK if len(t_) > 40000 else ai.CHUNK_CHARS))
                                       for _, t_ in sources) + 5
                    done = {"n": 0, "last": None}

                    def step_progress(src, i, n):
                        if (src, i) != done["last"]:
                            done["n"] += 1
                            done["last"] = (src, i)
                        progress((done["n"] - 1) / total_chunks, t("part_progress", src=t(src), i=i, n=n))

                    prep, notes_text = ai.make_seminar_prep(model, sem, sources, lang, extras, step_progress)
                    save_note(t("seminar_n", n=sem["number"], title=sem["title"]), prep,
                              [n for n, _ in sources], notes_text, lang)

                jobs.start(t("job_sem", n=sem["number"]), make_prep)
                st.rerun()


def note_meta(path: Path) -> tuple[dict, str]:
    """Note metadata and text (also supports the old format without metadata)."""
    text = path.read_text()
    m = re.match(r"<!-- (\{.*?\}) -->\n", text)
    if m:
        return json.loads(m.group(1)), text[m.end():]
    return {}, text


# ---------- My notes ----------
with tab_notes:
    notes = list_notes()
    if not notes:
        empty("📭", t("empty_notes_t"), t("empty_notes"))
    else:
        note = st.selectbox(t("notes"), notes, format_func=lambda p: p.stem.replace("_", " "), key="note_view",
                            label_visibility="collapsed")
        meta, content = note_meta(note)
        c1, c2, c3 = st.columns([3, 1.2, 1])
        if meta:
            c1.markdown(f'<div class="note-meta">📅 {meta["date"]} · {meta["lang"]} · '
                        f'{html.escape(", ".join(meta["sources"]))}</div>', unsafe_allow_html=True)
        c2.download_button(t("download"), content, file_name=f"{note.stem}.md", use_container_width=True)
        if c3.button(t("delete"), use_container_width=True):
            note.unlink()
            for extra in (".quiz.json", ".chat.json", ".source.txt"):
                note.with_suffix(extra).unlink(missing_ok=True)
            st.rerun()

        col_note, col_chat = st.columns([1.35, 1], gap="large")
        with col_note:
            with st.container(border=True, key="card_note"):
                st.markdown(content)

        with col_chat:
            chat_path = note.with_suffix(".chat.json")
            source_path = note.with_suffix(".source.txt")
            history = json.loads(chat_path.read_text()) if chat_path.exists() else []
            with st.container(border=True, key="card_chat"):
                h1, h2 = st.columns([3, 1], vertical_alignment="center")
                h1.markdown(f'<div class="chat-title">{t("chat_title")}</div>', unsafe_allow_html=True)
                if history and h2.button(t("clear"), key="chat_clear"):
                    chat_path.unlink(missing_ok=True)
                    st.rerun()
                if not source_path.exists():
                    st.caption(t("no_source"))

                quick = None
                qcols = st.columns(len(ai.QUICK_ACTIONS))
                for qc, (key, prompt) in zip(qcols, ai.QUICK_ACTIONS.items()):
                    if qc.button(t(key), use_container_width=True, key=f"quick_{key}"):
                        quick = (t(key), prompt)

                messages_box = st.container(height=520, border=False)
                with messages_box:
                    if not history:
                        st.markdown(f'<div class="chat-empty">{t("chat_empty")}</div>', unsafe_allow_html=True)
                    for msg in history:
                        with st.chat_message(msg["role"], avatar="🧑‍🎓" if msg["role"] == "user" else "✨"):
                            st.markdown(msg.get("label") or msg["content"])

                question = st.chat_input(t("chat_input"), key=f"chat_{note.stem}")
                if quick:
                    question = quick[1]
                if question and model:
                    history.append({"role": "user", "content": question, **({"label": quick[0]} if quick else {})})
                    with messages_box:
                        with st.chat_message("user", avatar="🧑‍🎓"):
                            st.markdown(quick[0] if quick else question)
                        with st.chat_message("assistant", avatar="✨"):
                            try:
                                answer = st.write_stream(ai.chat_stream(
                                    model, [{"role": m["role"], "content": m["content"]} for m in history],
                                    content, source_path.read_text() if source_path.exists() else "", lang))
                                history.append({"role": "assistant", "content": answer})
                                chat_path.write_text(json.dumps(history, ensure_ascii=False, indent=2))
                            except Exception as e:
                                st.error(str(e))
                                history.pop()

                if history and history[-1]["role"] == "assistant":
                    last = history[-1]["content"]
                    b1, b2 = st.columns(2)
                    if b1.button(t("to_note"), use_container_width=True):
                        note.write_text(note.read_text().rstrip() + "\n\n---\n\n" + last + "\n")
                        st.toast(t("added"))
                        st.rerun()
                    if b2.button(t("replace"), use_container_width=True):
                        meta_line = note.read_text().split("\n", 1)[0] if meta else ""
                        title_line = next((l for l in content.splitlines() if l.startswith("# ")), f"# {note.stem}")
                        note.write_text((meta_line + "\n" if meta_line else "") + f"{title_line}\n\n{last}\n")
                        st.session_state.pop(f"checked_{note.stem}", None)
                        st.toast(t("replaced"))
                        st.rerun()

# ---------- Quizzes ----------
with tab_quiz:
    notes = list_notes()
    if not notes:
        empty("🎯", t("empty_quiz_t"), t("empty_quiz"))
    else:
        with st.container(border=True, key="card_quizbar"):
            c1, c2, c3 = st.columns([3, 2, 1.4], vertical_alignment="bottom")
            note = c1.selectbox(t("quiz_note"), notes, format_func=lambda p: p.stem.replace("_", " "),
                                key="note_quiz")
            n = c2.slider(t("questions"), 5, 25, 10)
            make = c3.button(t("new_quiz"), type="primary", disabled=not model, use_container_width=True)
        quiz_path = note.with_suffix(".quiz.json")
        checked_key = f"checked_{note.stem}"
        if make:
            with st.spinner(t("making_quiz")):
                try:
                    questions = ai.make_quiz(model, note_meta(note)[1], lang, n)
                    quiz_path.write_text(json.dumps(questions, ensure_ascii=False, indent=2))
                    st.session_state.pop(checked_key, None)
                    for k in [k for k in st.session_state if k.startswith(f"q_{note.stem}_")]:
                        del st.session_state[k]
                except Exception as e:
                    st.error(str(e))

        if not quiz_path.exists():
            empty("🎲", t("press_new_t"), t("press_new"))
        else:
            questions = json.loads(quiz_path.read_text())
            checked = st.session_state.get(checked_key)

            if checked is not None:
                score = sum(a == q["correct"] for a, q in zip(checked, questions))
                pct = round(score / len(questions) * 100)
                verdict = t("v90") if pct >= 90 else t("v70") if pct >= 70 else t("v50") if pct >= 50 else t("v0")
                st.markdown(f'<div class="score"><div class="ring" style="--p:{pct}"><span>{pct}%</span></div>'
                            f'<div><div class="t">{verdict}</div><div class="s">'
                            f'{t("score", s=score, n=len(questions))}</div></div></div>', unsafe_allow_html=True)

            with st.form(f"quiz_{note.stem}"):
                answers = []
                for i, q in enumerate(questions):
                    with st.container(border=True, key=f"card_q_{note.stem}_{i}"):
                        answers.append(st.radio(f"**{i + 1}. {q['question']}**", range(len(q["options"])),
                                                format_func=lambda j, q=q: q["options"][j],
                                                index=None, key=f"q_{note.stem}_{i}"))
                        if checked is not None:
                            if checked[i] == q["correct"]:
                                st.success(t("correct"))
                            else:
                                st.error(t("right_answer", a=q["options"][q["correct"]]) + "  \n"
                                         + q.get("explanation", ""))
                if st.form_submit_button(t("check"), type="primary", use_container_width=True):
                    st.session_state[checked_key] = answers
                    st.rerun()
