import os
import re
import json
import math
import time
import base64
import hashlib
import unicodedata
from collections import OrderedDict

import fitz  # PyMuPDF
import anthropic
from google import genai
from google.genai import types
from fastapi import FastAPI, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

# ───────────────────────── الإعدادات ─────────────────────────
# المفاتيح من Render -> Environment. إذا وُجد المفتاحان يجرّب Claude ثم Gemini تلقائياً.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client = (anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=40.0, max_retries=0)
          if ANTHROPIC_API_KEY else None)
gemini_client = (genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=40000))
                 if GEMINI_API_KEY else None)

# Claude: Haiku أسرع وأرخص. للجودة الأعلى ضع CLAUDE_MODEL=claude-sonnet-5-5
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
# قائمة موديلات Gemini بالترتيب: إذا كان الأول مشغولاً (503) ينتقل للثاني تلقائياً
GEMINI_MODELS = [m.strip() for m in
                 os.environ.get("GEMINI_MODELS", "gemini-3.8-flash,gemini-2.5-flash").split(",") if m.strip()]

ANALYSIS_DPI = 100   # دقة الصور المرسلة للموديل (للكتب الممسوحة فقط)
DISPLAY_DPI = 110    # دقة الصفحة المعروضة للطالب
BOOKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "books")

# إذا كان عندك ملفات بأسماء قديمة، اربطها بالكود الجديد هنا. مثال:
# ALIASES = {"s1_t2_physics": "physics2_t1"}
ALIASES = {}

app = FastAPI(title="Smart Curriculum Educational Platform")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


# ───────────────────────── كتالوج المواد ─────────────────────────
# كود الكتاب = {حرف المرحلة}{رقم الصف}_t{رقم الفصل}_{اسم المادة}
# مثال: p3_t1_math  ->  books/p3_t1_math.pdf   (والمحلول: p3_t1_math_solved.pdf)
# لمعرفة كل الأسماء المطلوبة افتح:  /api/missing

STAGES = [
    {"id": "primary", "key": "p", "years": 6,
     "ar": "المرحلة الابتدائية", "en": "Primary school"},
    {"id": "intermediate", "key": "m", "years": 3,
     "ar": "المرحلة المتوسطة", "en": "Middle school"},
    {"id": "secondary", "key": "s", "years": 3,
     "ar": "المرحلة الثانوية (المسارات)", "en": "High school (Tracks)"},
]
ORD_AR = ["الأول", "الثاني", "الثالث", "الرابع", "الخامس", "السادس"]
TERMS = [(1, "الفصل الدراسي الأول", "First semester"), (2, "الفصل الدراسي الثاني", "Second semester")]


def year_names(stage_key, y):
    if stage_key == "p":
        return f"الصف {ORD_AR[y - 1]} الابتدائي", f"Grade {y} (Primary)"
    if stage_key == "m":
        return f"الصف {ORD_AR[y - 1]} المتوسط", f"Grade {y} (Middle)"
    return ({1: "السنة الأولى (المشتركة)", 2: "السنة الثانية", 3: "السنة الثالثة"}[y],
            {1: "Year 1 (Common)", 2: "Year 2", 3: "Year 3"}[y])


def subjects_for(stage_key, year, term):
    """يرجع [(slug, اسم_عربي, اسم_إنجليزي)] لكل مادة في هذا الصف والفصل.
    لتعديل مادة أو إضافة أخرى عدّل السطر المناسب هنا فقط."""
    if stage_key == "p":
        s = [("quran", "القرآن الكريم", "Holy Quran"),
             ("islamic", "الدراسات الإسلامية", "Islamic Studies"),
             ("lugati", "لغتي", "My Language"),
             ("math", "الرياضيات", "Mathematics"),
             ("science", "العلوم", "Science"),
             ("english", "اللغة الإنجليزية", "English Language")]
        if year >= 4:
            s += [("social", "الدراسات الاجتماعية", "Social Studies"),
                  ("digital", "المهارات الرقمية", "Digital Skills")]
        s += [("art", "التربية الفنية", "Art Education"),
              ("pep", "التربية البدنية والدفاع عن النفس", "Physical Education & Self-Defense"),
              ("life", "المهارات الحياتية والأسرية", "Life & Family Skills")]
        return s
    if stage_key == "m":
        return [("quran", "القرآن الكريم", "Holy Quran"),
                ("islamic", "الدراسات الإسلامية", "Islamic Studies"),
                ("arabic", "لغتي الخالدة", "Arabic Language"),
                ("english", "اللغة الإنجليزية", "English Language"),
                ("math", "الرياضيات", "Mathematics"),
                ("science", "العلوم", "Science"),
                ("social", "الدراسات الاجتماعية", "Social Studies"),
                ("digital", "المهارات الرقمية", "Digital Skills"),
                ("art", "التربية الفنية", "Art Education"),
                ("pe", "التربية البدنية والصحية", "Physical & Health Education"),
                ("life", "المهارات الحياتية والأسرية", "Life & Family Skills")]
    # الثانوية (نظام المسارات): المقررات مرقّمة حسب ترتيبها خلال السنوات الثلاث
    n = (year - 1) * 2 + term
    s = [("islamic", "الدراسات الإسلامية", "Islamic Studies"),
         ("kafaayat", "الكفايات اللغوية", "Language Competencies"),
         ("english", "اللغة الإنجليزية", "English Language"),
         ("math", f"الرياضيات {n}", f"Mathematics {n}"),
         ("physics", f"الفيزياء {n}", f"Physics {n}"),
         ("chem", f"الكيمياء {n}", f"Chemistry {n}"),
         ("bio", f"الأحياء {n}", f"Biology {n}")]
    if n <= 3:
        s.append(("tech", f"التقنية الرقمية {n}", f"Digital Technology {n}"))
    if year <= 2:
        s.append(("social", "الدراسات الاجتماعية", "Social Studies"))
    if year == 1:
        s.append(("critical", "التفكير الناقد", "Critical Thinking"))
    s.append(("health", "الصحة واللياقة", "Health & Fitness"))
    return s


_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def book_path(code, solved=False):
    if not _CODE_RE.match(code or ""):
        return None
    real = ALIASES.get(code, code)
    path = os.path.join(BOOKS_DIR, f"{real}{'_solved' if solved else ''}.pdf")
    return path if os.path.exists(path) else None


def build_catalog():
    stages = []
    for st in STAGES:
        years = []
        for y in range(1, st["years"] + 1):
            yar, yen = year_names(st["key"], y)
            terms = []
            for tn, tar, ten in TERMS:
                subs = []
                for slug, sar, sen in subjects_for(st["key"], y, tn):
                    code = f"{st['key']}{y}_t{tn}_{slug}"
                    subs.append({"code": code, "ar": sar, "en": sen,
                                 "pdf": book_path(code) is not None,
                                 "solved": book_path(code, True) is not None})
                terms.append({"n": tn, "ar": tar, "en": ten, "subjects": subs})
            years.append({"n": y, "ar": yar, "en": yen, "terms": terms})
        stages.append({"id": st["id"], "ar": st["ar"], "en": st["en"], "years": years})
    return {"stages": stages}


# ───────────────────────── معالجة النص العربي والترتيب ─────────────────────────
_DIAC = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")  # يوحّد أشكال الحروف العربية المتصلة
    s = _DIAC.sub("", s)
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ٱ", "ا"), ("ى", "ي"), ("ة", "ه")):
        s = s.replace(a, b)
    s = _PUNCT.sub(" ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


_STOP = {norm(w) for w in
         "ما ماذا من هو هي هل كيف لماذا متى اين في على عن الى ذلك هذا هذه الذي التي اذكر عرف وضح اشرح علل فسر عدد".split()}


def query_tokens(q: str):
    out = []
    for w in norm(q).split():
        if w in _STOP:
            continue
        if w.startswith("ال") and len(w) > 4:
            w = w[2:]
        if len(w) >= 2 and w not in out:
            out.append(w)
    return out


_INDEX_CACHE = OrderedDict()
_ANS_CACHE = OrderedDict()


def get_index(doc, key):
    """فهرس نصي للكتاب يُبنى مرة واحدة ثم يُحفظ في الذاكرة (هذا سر السرعة في الأسئلة التالية)."""
    if key in _INDEX_CACHE:
        _INDEX_CACHE.move_to_end(key)
        return _INDEX_CACHE[key]
    idx = []
    for page in doc:
        t = page.get_text("text")
        idx.append((norm(t), len(t)))
    _INDEX_CACHE[key] = idx
    while len(_INDEX_CACHE) > 12:
        _INDEX_CACHE.popitem(last=False)
    return idx


def rank_pages(index, query, top_n=3):
    toks = query_tokens(query)
    if not toks:
        return [], 0.0
    n = len(index)
    df = {t: sum(1 for p, _ in index if t in p) for t in toks}
    scored = []
    for i, (p, _) in enumerate(index):
        s = 0.0
        for t in toks:
            c = p.count(t)
            if c:
                s += (1 + math.log(c)) * math.log(1 + n / (1 + df[t]))
        for a, b in zip(toks, toks[1:]):
            if f"{a} {b}" in p:
                s += 1.5
        if s > 0:
            scored.append((s, i))
    scored.sort(reverse=True)
    top = scored[:top_n]
    return [i for _, i in top], (top[0][0] if top else 0.0)


def page_words(page):
    words = page.get_text("words")  # x0,y0,x1,y1,word,block,line,wordno
    words.sort(key=lambda w: (w[5], w[6], w[7]))
    return words


def words_to_text(words, limit=5000):
    lines, cur, last = [], [], None
    for w in words:
        key = (w[5], w[6])
        if last is not None and key != last:
            lines.append(" ".join(cur))
            cur = []
        cur.append(unicodedata.normalize("NFKC", w[4]))
        last = key
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)[:limit]


def locate_rects(page, exact_text):
    """يحدد مكان الجملة داخل الصفحة على مستوى الكلمات (أدق من البحث النصي المباشر)."""
    words = page_words(page)
    nw = [norm(w[4]).replace(" ", "") for w in words]
    nt = [x for x in norm(exact_text).split() if x]
    m = len(nt)
    if not m or not nw:
        return []
    best_i, best_hits = -1, 0
    for i in range(max(1, len(nw) - m + 1)):
        hits = sum(1 for j in range(m) if i + j < len(nw) and nw[i + j] == nt[j])
        if hits > best_hits:
            best_hits, best_i = hits, i
    if best_i < 0 or best_hits < max(2, int(m * 0.6)):
        return []
    lines = {}
    for w in words[best_i:best_i + m]:
        r = fitz.Rect(w[:4])
        k = (w[5], w[6])
        lines[k] = (lines[k] | r) if k in lines else r
    return list(lines.values())


# ───────────────────────── الذكاء الاصطناعي ─────────────────────────
class AIUnavailable(Exception):
    def __init__(self, msg, status=503, retry=True):
        super().__init__(msg)
        self.msg, self.status, self.retry = msg, status, retry


def _is_transient(e) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code in (408, 429, 500, 502, 503, 504, 529):
        return True
    text = f"{type(e).__name__} {e}".lower()
    return any(k in text for k in ("timeout", "timed out", "unavailable", "overloaded", "connection", "deadline"))


def call_claude(pages, instructions):
    content = []
    for p in pages:
        if p.get("text") is not None:
            content.append({"type": "text", "text": f"{p['label']}\n{p['text']}"})
        else:
            content.append({"type": "text", "text": p["label"]})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(p["jpeg"]).decode()}})
    content.append({"type": "text", "text": instructions})
    resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=700,
                                  messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def call_gemini(model, pages, instructions):
    contents = []
    for p in pages:
        contents.append(p["label"])
        if p.get("text") is not None:
            contents.append(p["text"])
        else:
            contents.append(types.Part.from_bytes(data=p["jpeg"], mime_type="image/jpeg"))
    contents.append(instructions)
    think = (types.ThinkingConfig(thinking_budget=0) if model.startswith("gemini-2.5")
             else types.ThinkingConfig(thinking_level="low"))
    resp = gemini_client.models.generate_content(
        model=model, contents=contents,
        config=types.GenerateContentConfig(thinking_config=think, max_output_tokens=900, temperature=0.2))
    return (resp.text or "").strip()


def run_models(pages, make_instructions):
    """يجرّب الموديلات بالترتيب. الخطأ المؤقت (503/429/مهلة) يعيد المحاولة مرة ثم ينتقل للموديل التالي."""
    attempts = []
    if client:
        attempts.append(("claude", CLAUDE_MODEL))
    if gemini_client:
        attempts += [("gemini", m) for m in GEMINI_MODELS]
    if not attempts:
        raise AIUnavailable("لم يتم ضبط مفتاح: أضف ANTHROPIC_API_KEY أو GEMINI_API_KEY في إعدادات Render.",
                            status=500, retry=False)
    for provider, model in attempts:
        instructions = make_instructions(provider)
        for i in range(2):
            try:
                raw = (call_claude(pages, instructions) if provider == "claude"
                       else call_gemini(model, pages, instructions))
                if raw:
                    return raw, provider
                break
            except Exception as e:
                print(f"[AI] {provider}/{model} attempt {i + 1} failed: {type(e).__name__}: {str(e)[:200]}")
                if i == 0 and _is_transient(e):
                    time.sleep(1.0)
                    continue
                break
    raise AIUnavailable("الذكاء الاصطناعي مشغول حالياً بسبب كثرة الطلبات. انتظر لحظات وأعد المحاولة.")


def parse_reply(raw, page_indices):
    first = re.search(r"(?m)^\s*(PAGE|PRINTED_PAGE|BOXES|EXACT_TEXT)\s*:", raw)
    answer = (raw[:first.start()] if first else raw).strip().replace("**", "").replace("__", "")
    page_idx = page_indices[0]
    m = re.search(r"(?m)^\s*PAGE\s*:\s*(\d+)", raw)
    if m and (int(m.group(1)) - 1) in page_indices:
        page_idx = int(m.group(1)) - 1
    printed = None
    m = re.search(r"(?m)^\s*PRINTED_PAGE\s*:\s*(\d+)", raw)
    if m and int(m.group(1)) > 0:
        printed = int(m.group(1))
    boxes = []
    m = re.search(r"(?m)^\s*BOXES\s*:\s*(\[.*\])\s*$", raw)
    if m:
        try:
            boxes = [b for b in json.loads(m.group(1)) if isinstance(b, list) and len(b) == 4]
        except Exception:
            boxes = []
    highlight = ""
    m = re.search(r"(?m)^\s*EXACT_TEXT\s*:\s*(.+)$", raw)
    if m:
        highlight = m.group(1).strip(" \"'«»")
    return answer, page_idx, printed, boxes, highlight


def to_pixel_boxes(boxes, provider, w, h):
    out, pad = [], 3
    for box in boxes:
        try:
            v = [float(n) for n in box]
        except Exception:
            continue
        if provider == "gemini":  # [ymin,xmin,ymax,xmax] من 0 إلى 1000
            x0, y0, x1, y1 = v[1] / 1000 * w, v[0] / 1000 * h, v[3] / 1000 * w, v[2] / 1000 * h
        else:
            x0, y0, x1, y1 = v
        out.append([x0 - pad, y0 - pad, x1 + pad, y1 + pad])
    return out


COMMON_TAIL = ("اكتب الإجابة نصاً عادياً بدون Markdown وبإيجاز (من جملتين إلى أربع جمل). "
               "إذا لم تجد الإجابة في الصفحات فقل ذلك بصراحة ولا تخترع إجابة.\n"
               "بعد الإجابة أضف الأسطر التالية بالضبط، كل واحد في سطر مستقل:\n")


def text_instructions(query):
    return (f"أنت معلم خبير في المناهج الدراسية. اعتماداً على نصوص صفحات الكتاب المرفقة فقط أجب بالعربية عن السؤال: '{query}'.\n"
            + COMMON_TAIL +
            "PAGE: رقم الصفحة كما في عنوانها (أو 0 إذا لم تجد الإجابة)\n"
            "PRINTED_PAGE: الرقم المطبوع على الصفحة إن ظهر في نصها، وإلا 0\n"
            "EXACT_TEXT: الجملة القصيرة (حتى 25 كلمة) من الكتاب التي تحتوي الإجابة، منسوخة حرفياً من النص")


def image_instructions(query, provider):
    if provider == "gemini":
        boxes = ("BOXES: مصفوفة JSON من المستطيلات [ymin,xmin,ymax,xmax] بإحداثيات من 0 إلى 1000 على صورة تلك الصفحة، "
                 "مستطيل لكل سطر من سطور الجملة التي فيها الإجابة، مثال: [[300,150,330,700]]. إذا لم تجد اكتب []\n")
    else:
        boxes = ("BOXES: مصفوفة JSON من المستطيلات [x0,y0,x1,y1] بالبكسل على صورة تلك الصفحة (0,0 أعلى اليسار)، "
                 "مستطيل لكل سطر من سطور الجملة التي فيها الإجابة، مثال: [[80,300,500,324]]. إذا لم تجد اكتب []\n")
    return (f"أنت معلم خبير في المناهج الدراسية. اعتماداً على صور صفحات الكتاب المرفقة فقط أجب بالعربية عن السؤال: '{query}'.\n"
            + COMMON_TAIL +
            "PAGE: رقم الصفحة المرفقة كما في عنوان الصورة (أو 0)\n"
            "PRINTED_PAGE: الرقم المطبوع على الصفحة نفسها (غالباً أسفلها) أو 0\n"
            + boxes +
            "EXACT_TEXT: الجملة الدقيقة من الكتاب التي تحتوي الإجابة")


def render_page(doc, page_num, rects):
    page = doc[page_num]
    for r in rects:
        r = fitz.Rect(r) & page.rect
        if r.is_empty:
            continue
        r = r + (-1.5, -1.5, 1.5, 1.5)
        page.draw_rect(r, color=(0.96, 0.55, 0.05), fill=(1, 0.8, 0.1), fill_opacity=0.40, width=1, overlay=True)
    pix = page.get_pixmap(dpi=DISPLAY_DPI)
    enc = base64.b64encode(pix.tobytes("jpeg", jpg_quality=82)).decode()
    return f"data:image/jpeg;base64,{enc}"


def answer_question(doc, key, query, empty_msg):
    index = get_index(doc, key)
    cands, best = rank_pages(index, query, top_n=3)
    text_ok = bool(cands) and best > 0 and all(index[i][1] >= 120 for i in cands[:2])

    pages, dims = [], {}
    if text_ok:
        # كتاب فيه نص قابل للقراءة: نرسل النص فقط (أسرع بكثير من الصور)
        for i in cands:
            pages.append({"label": f"[صفحة رقم {i + 1}]", "text": words_to_text(page_words(doc[i]))})
        raw, provider = run_models(pages, lambda p: text_instructions(query))
    else:
        # كتاب ممسوح ضوئياً (صور): نرسل صورتين فقط بدقة معتدلة
        cands = (cands or list(range(min(len(doc), 2))))[:2]
        for i in cands:
            pix = doc[i].get_pixmap(dpi=ANALYSIS_DPI)
            dims[i] = (pix.width, pix.height)
            pages.append({"label": f"صفحة رقم {i + 1} (أبعاد الصورة: {pix.width}x{pix.height} بكسل):",
                          "jpeg": pix.tobytes("jpeg", jpg_quality=75)})
        raw, provider = run_models(pages, lambda p: image_instructions(query, p))

    answer, page_idx, printed, boxes, highlight = parse_reply(raw, cands)
    rects = []
    if highlight:
        rects = locate_rects(doc[page_idx], highlight)
        if not rects:
            try:
                rects = list(doc[page_idx].search_for(highlight))
            except Exception:
                rects = []
    if not rects and boxes and page_idx in dims:
        k = 72.0 / ANALYSIS_DPI
        w, h = dims[page_idx]
        for b in to_pixel_boxes(boxes, provider, w, h):
            rects.append(fitz.Rect(min(b[0], b[2]) * k, min(b[1], b[3]) * k, max(b[0], b[2]) * k, max(b[1], b[3]) * k))

    return {
        "page_number": printed if printed else page_idx + 1,
        "pdf_page": page_idx + 1,
        "answer": answer or empty_msg,
        "highlight": highlight,
        "page_image": render_page(doc, page_idx, rects),
    }


def cached_answer(doc_factory, key, query, empty_msg):
    ck = (key, " ".join(query_tokens(query)) or norm(query))
    if ck in _ANS_CACHE:
        _ANS_CACHE.move_to_end(ck)
        return _ANS_CACHE[ck]
    doc = doc_factory()
    try:
        result = answer_question(doc, key, query, empty_msg)
    finally:
        doc.close()
    _ANS_CACHE[ck] = result
    while len(_ANS_CACHE) > 40:
        _ANS_CACHE.popitem(last=False)
    return result


def respond(fn):
    try:
        return fn()
    except AIUnavailable as e:
        return JSONResponse(status_code=e.status, content={"error": e.msg, "error_ar": e.msg, "retry": e.retry})
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        msg = "حدث خطأ غير متوقع أثناء معالجة الملف. حاول مرة أخرى."
        return JSONResponse(status_code=500, content={"error": msg, "error_ar": msg, "detail": str(e)[:200], "retry": True})


# ───────────────────────── واجهات API ─────────────────────────
@app.get("/api/catalog")
def api_catalog():
    return build_catalog()


@app.get("/api/missing")
def api_missing():
    """قائمة بأسماء ملفات PDF المطلوب وضعها في مجلد books."""
    missing = []
    for st in build_catalog()["stages"]:
        for y in st["years"]:
            for t in y["terms"]:
                for s in t["subjects"]:
                    if not s["pdf"]:
                        missing.append({"file": f"{s['code']}.pdf", "title": f"{s['ar']} - {t['ar']} - {y['ar']}"})
    return {"count": len(missing), "files": missing}


@app.get("/api/book/{code}/pdf")
def api_book_pdf(code: str, solved: int = 0):
    path = book_path(code, bool(solved))
    if not path:
        return JSONResponse(status_code=404, content={"error_ar": "الكتاب غير متوفر حالياً في المكتبة الرقمية."})
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{code}.pdf"'})


@app.get("/api/search")
def search_in_book(book_code: str = Query(...), query: str = Query(..., min_length=2, max_length=300)):
    path = book_path(book_code)
    if not path:
        return JSONResponse(status_code=404, content={
            "error_ar": "عذراً، الكتاب غير متوفر حالياً في المكتبة الرقمية.",
            "error_en": "Sorry, this book is currently not available in the digital library.",
            "error": "عذراً، الكتاب غير متوفر حالياً في المكتبة الرقمية.", "retry": False})
    key = (path, os.path.getmtime(path))
    return respond(lambda: cached_answer(lambda: fitz.open(path), key, query, "لم يتم العثور على إجابة واضحة."))


@app.post("/api/custom-pdf-search")
def search_custom_pdf(file: UploadFile = File(...), query: str = Form(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        return JSONResponse(status_code=400, content={"error": "يجب أن يكون الملف بصيغة PDF فقط.", "retry": False})
    contents = file.file.read()
    if len(contents) > 60 * 1024 * 1024:
        return JSONResponse(status_code=413, content={"error": "حجم الملف كبير جداً (الحد 60 ميجابايت).", "retry": False})
    key = hashlib.sha1(contents).hexdigest()
    return respond(lambda: cached_answer(lambda: fitz.open(stream=contents, filetype="pdf"), key, query,
                                         "لم يتم العثور على إجابة كافية في هذا المستند."))


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=HTML_PAGE)


# ───────────────────────── الواجهة ─────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f2a3d">
<title>المنصة التعليمية الذكية للمناهج</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📘</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&family=Reem+Kufi:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{--paper:#f3f6f4;--ink:#0f2a3d;--green:#0b6b50;--green-d:#084d3a;--amber:#f4b942;--amber-soft:#fff3d1;
--line:#d5ded9;--card:#fff;--muted:#5a6a72;--danger:#b42318;--r:14px}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;color:var(--ink);font-family:'IBM Plex Sans Arabic','Tajawal',system-ui,sans-serif;line-height:1.7;
background:var(--paper) repeating-linear-gradient(to bottom,transparent 0 33px,rgba(15,42,61,.055) 33px 34px)}
button,input{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:3px solid var(--amber);outline-offset:2px}
.top{background:var(--ink);color:#fff;padding:calc(14px + env(safe-area-inset-top,0px)) 16px 16px}
.top-in{max-width:760px;margin:0 auto;display:flex;align-items:center;gap:12px;justify-content:space-between}
.top h1{font-family:'Reem Kufi',sans-serif;font-weight:700;font-size:1.3rem;line-height:1.35;margin:0}
.top p{margin:2px 0 0;font-size:.85rem;color:#b9c8d1}
.lang{border:1.5px solid #5d7a8c;background:transparent;color:#fff;border-radius:999px;padding:6px 16px;font-weight:600;white-space:nowrap}
.lang:hover{background:#1b3f58}
.wrap{max-width:760px;margin:0 auto;padding:16px 16px calc(40px + env(safe-area-inset-bottom,0px))}
.crumbs{display:flex;gap:6px;align-items:center;overflow-x:auto;white-space:nowrap;padding:2px 0 14px;font-size:.88rem;scrollbar-width:none}
.crumbs::-webkit-scrollbar{display:none}
.crumbs button{background:#e4ece8;border:0;border-radius:999px;padding:4px 12px;color:var(--ink)}
.crumbs button:hover{background:#d3e0da}
.crumbs .cur{color:var(--muted);padding:4px 4px}
.crumbs .sep{color:#98a8a2}
body[dir=rtl] .crumbs .sep{transform:scaleX(-1)}
h2.h{font-family:'Reem Kufi',sans-serif;font-size:1.35rem;margin:6px 0 14px;font-weight:700}
.stack{display:grid;gap:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:12px}
.tile{display:flex;align-items:center;justify-content:space-between;gap:12px;text-align:start;background:var(--card);
border:1px solid var(--line);border-inline-start:7px solid var(--c,var(--green));border-radius:var(--r);padding:16px 18px;width:100%}
.tile b{font-size:1.1rem;font-weight:600}
.tile span{color:var(--muted);font-size:.85rem;white-space:nowrap}
.tile:hover{border-color:var(--c,var(--green));background:#fbfdfc}
.book{position:relative;display:flex;flex-direction:column;justify-content:space-between;gap:14px;min-height:112px;text-align:start;
background:var(--card);border:1px solid var(--line);border-inline-start:9px solid var(--c);border-radius:6px 14px 14px 6px;padding:14px 14px 12px;
box-shadow:0 1px 0 rgba(15,42,61,.06),0 6px 14px -10px rgba(15,42,61,.35)}
body[dir=rtl] .book{border-radius:14px 6px 6px 14px}
.book .bt{font-weight:600;line-height:1.45}
.book .bb{align-self:flex-start;font-size:.75rem;font-weight:600;padding:1px 10px;border-radius:999px;background:var(--c);color:#fff}
.book.off{background:#f6f8f7}
.book.off .bt{color:#6c7b83}
.book.off .bb{background:#e1e7e4;color:#5a6a72}
.book:hover{border-color:var(--c)}
.upload{display:block;width:100%;margin-top:22px;text-align:start;background:transparent;border:2px dashed #8aa89c;border-radius:var(--r);padding:16px 18px}
.upload b{display:block;font-size:1.05rem}
.upload span{color:var(--muted);font-size:.9rem}
.upload:hover{background:#eaf2ee;border-color:var(--green)}
.cover{background:var(--c);color:#fff;border-radius:6px 16px 16px 6px;padding:22px 20px;margin-bottom:16px;box-shadow:0 12px 22px -14px rgba(15,42,61,.6)}
body[dir=rtl] .cover{border-radius:16px 6px 6px 16px}
.cover h2{font-family:'Reem Kufi',sans-serif;margin:0 0 4px;font-size:1.6rem;line-height:1.35}
.cover p{margin:0;opacity:.9;font-size:.92rem}
.act{display:flex;align-items:center;gap:14px;width:100%;text-align:start;text-decoration:none;color:var(--ink);background:var(--card);
border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;margin-bottom:10px}
.act svg{flex:none;width:24px;height:24px;stroke:var(--green);fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.act b{display:block;font-weight:600}
.act small{display:block;color:var(--muted);font-size:.82rem;line-height:1.4}
.act:hover{border-color:var(--green);background:#fbfdfc}
.act.off{background:#f6f8f7;color:#7b8a91;cursor:default;pointer-events:none}
.act.off svg{stroke:#9aa9b0}
.act.hot{border-color:var(--green);border-width:2px}
.field{margin-bottom:14px}
.field label{display:block;font-weight:600;margin-bottom:6px}
.field input[type=text]{width:100%;padding:14px;border:1.5px solid #b9c7c1;border-radius:12px;background:#fff;font-size:1rem}
.field input[type=text]:focus{outline:none;border-color:var(--green);box-shadow:0 0 0 3px rgba(11,107,80,.18)}
.file{display:flex;align-items:center;gap:10px;padding:12px 14px;border:1.5px dashed #8aa89c;border-radius:12px;background:#fff;cursor:pointer}
.file input{position:absolute;opacity:0;width:1px;height:1px}
.file .fb{background:var(--green);color:#fff;border-radius:8px;padding:4px 12px;font-weight:600;font-size:.9rem}
.file .fn{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btn{width:100%;border:0;border-radius:12px;background:var(--green);color:#fff;font-weight:700;font-size:1.05rem;padding:14px;box-shadow:0 8px 16px -10px rgba(8,77,58,.8)}
.btn:hover{background:var(--green-d)}
.btn:disabled{opacity:.6;cursor:wait}
.btn.ghost{background:#fff;color:var(--green);border:1.5px solid var(--green);box-shadow:none;margin-top:12px}
.hint{color:var(--muted);font-size:.85rem;margin:8px 0 0}
#out{margin-top:18px}
.loading{display:flex;flex-direction:column;align-items:center;gap:10px;padding:26px 10px;color:var(--green);font-weight:600;text-align:center}
.spin{width:38px;height:38px;border:4px solid #d5e4dd;border-top-color:var(--green);border-radius:50%;animation:sp .9s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation-duration:3s}}
.answer{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:16px}
.meta{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.chip{background:var(--green);color:#fff;border-radius:999px;padding:2px 14px;font-weight:600;font-size:.9rem}
.chip.soft{background:#e4ece8;color:var(--ink);font-weight:500}
.atext{margin:0 0 12px;font-size:1.05rem;white-space:pre-wrap}
blockquote{margin:0 0 14px;padding:10px 14px;background:var(--amber-soft);border-inline-start:5px solid var(--amber);border-radius:8px;font-size:.95rem}
blockquote small{display:block;color:var(--muted);margin-bottom:2px}
figure{margin:0}
figure img{display:block;width:100%;border-radius:10px;border:1px solid var(--line);cursor:zoom-in}
figure figcaption{color:var(--muted);font-size:.8rem;margin-top:6px;text-align:center}
.error{background:#fef3f2;border:1px solid #f4b8b2;color:#7a1a12;border-radius:var(--r);padding:14px 16px}
.error p{margin:0}
#zoom{position:fixed;inset:0;background:rgba(10,20,30,.92);overflow:auto;display:none;z-index:50;padding:12px}
#zoom.on{display:block}
#zoom img{display:block;width:max(100%,900px);margin:0 auto;border-radius:6px;touch-action:pan-x pan-y pinch-zoom}
#zoom button{position:fixed;top:calc(10px + env(safe-area-inset-top,0px));inset-inline-start:12px;border:0;border-radius:999px;background:#fff;padding:6px 16px;font-weight:700;z-index:51}
</style>
</head>
<body>
<header class="top"><div class="top-in">
  <div><h1 id="ttl"></h1><p id="sub"></p></div>
  <button class="lang" id="lang" onclick="toggleLang()"></button>
</div></header>
<main class="wrap" id="app"></main>
<div id="zoom" onclick="this.classList.remove('on')"><button type="button" id="zclose"></button><img id="zimg" alt=""></div>

<script>
const T={
ar:{title:'المنصة التعليمية الذكية للمناهج',sub:'اسأل كتابك المدرسي واحصل على الإجابة مع رقم الصفحة',
home:'الرئيسية',pickStage:'اختر المرحلة الدراسية',pickYear:'اختر الصف',pickTerm:'اختر الفصل الدراسي',pickSubject:'اختر المادة',
yearsN:'سنوات دراسية',customTitle:'ارفع كتابك الخاص',customSub:'أي ملف PDF: اسأل عنه وسنظلل الإجابة في الصفحة',
browse:'تصفّح الكتاب (PDF)',browseHint:'افتح الكتاب كاملاً',solved:'الكتاب محلول',solvedHint:'الكتاب مع الحلول',
ask:'اسأل الكتاب بالذكاء الاصطناعي',askHint:'إجابة سريعة مع رقم الصفحة وتظليل الجواب',videos:'شرح الدروس (فيديو)',
soon:'قريباً',notAvail:'ملف هذا الكتاب لم يُضف بعد',qLabel:'اكتب سؤالك',qPh:'مثال: ما هو قانون كبلر الأول؟',
search:'ابحث في الكتاب',chooseFile:'اختر ملف PDF',noFile:'لم يتم اختيار ملف',analyze:'حلّل واستخرج الإجابة',
page:'الصفحة',pdfPage:'ترتيبها في الملف',fromBook:'من نص الكتاب',pageCap:'اضغط على الصفحة لتكبيرها',retry:'أعد المحاولة',
close:'إغلاق',needQ:'اكتب سؤالك أولاً',needFile:'اختر ملف PDF أولاً',
l1:'أبحث في صفحات الكتاب…',l2:'أقرأ الصفحات الأقرب لسؤالك…',l3:'أجهّز الإجابة…',
errServer:'تعذّر الاتصال بالخادم. أعد المحاولة بعد لحظات.',errSlow:'استغرق الرد وقتاً طويلاً. أعد المحاولة.',errNet:'تحقق من اتصالك بالإنترنت ثم أعد المحاولة.'},
en:{title:'Smart Curriculum Platform',sub:'Ask your textbook and get the answer with the page number',
home:'Home',pickStage:'Choose a stage',pickYear:'Choose a grade',pickTerm:'Choose a semester',pickSubject:'Choose a subject',
yearsN:'academic years',customTitle:'Upload your own book',customSub:'Any PDF: ask a question and we highlight the answer on the page',
browse:'Browse the book (PDF)',browseHint:'Open the full textbook',solved:'Solved book',solvedHint:'Textbook with solutions',
ask:'Ask the book with AI',askHint:'Quick answer with page number and highlight',videos:'Lesson videos',
soon:'Soon',notAvail:'This book file has not been added yet',qLabel:'Type your question',qPh:'e.g. What is Kepler’s first law?',
search:'Search the book',chooseFile:'Choose PDF',noFile:'No file selected',analyze:'Analyze and get the answer',
page:'Page',pdfPage:'File page',fromBook:'From the book',pageCap:'Tap the page to zoom',retry:'Try again',
close:'Close',needQ:'Type your question first',needFile:'Choose a PDF first',
l1:'Searching the book pages…',l2:'Reading the closest pages…',l3:'Preparing the answer…',
errServer:'Could not reach the server. Try again in a moment.',errSlow:'The reply took too long. Try again.',errNet:'Check your connection and try again.'}};

const state={lang:'ar',cat:null,view:'home',stage:null,year:null,term:null,subject:null};
const ADMIN=new URLSearchParams(location.search).has('admin');
const $=id=>document.getElementById(id);
const t=k=>T[state.lang][k]||k;
const nm=o=>o[state.lang]||o.ar;
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const STAGE_COL={primary:'#0b6b50',intermediate:'#2a6fb0',secondary:'#b9730a'};
const PAL=['#0b6b50','#2a6fb0','#7b4fa8','#b8501a','#a8285a','#17808f','#66761a','#9a6a14'];
const slugOf=c=>c.replace(/^[a-z]\d_t\d_/,'');
function colorOf(code){let h=0;for(const ch of slugOf(code))h=(h*31+ch.charCodeAt(0))%PAL.length;return PAL[h]}
const S=()=>state.cat.stages.find(s=>s.id===state.stage);
const Y=()=>S().years[state.year];
const Tm=()=>Y().terms[state.term];
const Sub=()=>Tm().subjects[state.subject];
const ICON={
 book:'<path d="M4 19.5V5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2zm0 0A2 2 0 0 0 6 21h14"/>',
 check:'<path d="M4 12.5l5 5L20 6.5"/>',
 spark:'<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 16v4M17 18h4"/>',
 play:'<circle cx="12" cy="12" r="9"/><path d="M10 8.5v7l6-3.5z"/>'};
const ico=n=>`<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[n]}</svg>`;

function go(view,patch){
  Object.assign(state,patch||{});state.view=view;
  history.pushState({view,stage:state.stage,year:state.year,term:state.term,subject:state.subject},'');
  render();window.scrollTo(0,0);
}
window.addEventListener('popstate',e=>{
  if(e.state){Object.assign(state,e.state)}else{state.view='home'}
  render();
});
function toggleLang(){state.lang=state.lang==='ar'?'en':'ar';render()}

const ORDER=['home','years','terms','subjects','subject','ask'];
function crumbs(){
  if(state.view==='home')return '';
  const items=[{l:t('home'),v:'home'}];
  if(state.view==='custom'){items.push({l:t('customTitle')})}
  else{
    const i=ORDER.indexOf(state.view);
    if(i>=1)items.push({l:nm(S()),v:'years'});
    if(i>=2)items.push({l:nm(Y()),v:'terms'});
    if(i>=3)items.push({l:nm(Tm()),v:'subjects'});
    if(i>=4)items.push({l:nm(Sub()),v:'subject'});
    if(i>=5)items.push({l:t('ask')});
  }
  const last=items.length-1;
  return `<nav class="crumbs" aria-label="breadcrumb">`+items.map((it,i)=>{
    const sep=i<last?'<span class="sep">‹</span>':'';
    if(i===last)return `<span class="cur">${esc(it.l)}</span>`;
    return `<button type="button" onclick="go('${it.v}')">${esc(it.l)}</button>${sep}`;
  }).join('')+`</nav>`;
}

const views={
 home(){
  return `<h2 class="h">${t('pickStage')}</h2><div class="stack">`+
   state.cat.stages.map(s=>`<button class="tile" style="--c:${STAGE_COL[s.id]}" onclick="go('years',{stage:'${s.id}'})"><b>${esc(nm(s))}</b><span>${s.years.length} ${t('yearsN')}</span></button>`).join('')+
   `</div><button class="upload" onclick="go('custom')"><b>${t('customTitle')}</b><span>${t('customSub')}</span></button>`;
 },
 years(){
  return `<h2 class="h">${t('pickYear')}</h2><div class="stack">`+
   S().years.map((y,i)=>`<button class="tile" style="--c:${STAGE_COL[state.stage]}" onclick="go('terms',{year:${i}})"><b>${esc(nm(y))}</b></button>`).join('')+`</div>`;
 },
 terms(){
  return `<h2 class="h">${t('pickTerm')}</h2><div class="stack">`+
   Y().terms.map((tm,i)=>`<button class="tile" style="--c:${STAGE_COL[state.stage]}" onclick="go('subjects',{term:${i}})"><b>${esc(nm(tm))}</b><span>${tm.subjects.length}</span></button>`).join('')+`</div>`;
 },
 subjects(){
  return `<h2 class="h">${t('pickSubject')}</h2><div class="grid">`+
   Tm().subjects.map((s,i)=>`<button class="book ${s.pdf?'':'off'}" style="--c:${colorOf(s.code)}" onclick="go('subject',{subject:${i}})"><span class="bt">${esc(nm(s))}</span><span class="bb">${s.pdf?'PDF':t('soon')}</span></button>`).join('')+`</div>`;
 },
 subject(){
  const s=Sub(),u=`/api/book/${encodeURIComponent(s.code)}/pdf`;
  const link=(on,href,icon,l,h,hot)=>on?`<a class="act ${hot?'hot':''}" href="${href}" target="_blank" rel="noopener">${ico(icon)}<span><b>${l}</b><small>${h}</small></span></a>`
    :`<div class="act off">${ico(icon)}<span><b>${l}</b><small>${t('soon')}</small></span></div>`;
  return `<div class="cover" style="--c:${colorOf(s.code)}"><h2>${esc(nm(s))}</h2><p>${esc(nm(Y()))} · ${esc(nm(Tm()))}</p></div>`+
   (s.pdf?'':`<p class="hint" style="margin:0 0 12px">${t('notAvail')}</p>`)+
   link(s.pdf,u,'book',t('browse'),t('browseHint'))+
   link(s.solved,u+'?solved=1','check',t('solved'),t('solvedHint'))+
   (s.pdf?`<button class="act hot" onclick="go('ask')">${ico('spark')}<span><b>${t('ask')}</b><small>${t('askHint')}</small></span></button>`
         :`<div class="act off">${ico('spark')}<span><b>${t('ask')}</b><small>${t('notAvail')}</small></span></div>`)+
   link(false,'','play',t('videos'),'')+
   (ADMIN?`<p class="hint">books/${esc(s.code)}.pdf · books/${esc(s.code)}_solved.pdf</p>`:'');
 },
 ask(){
  return `<h2 class="h">${esc(nm(Sub()))}</h2>
   <div class="field"><label for="q">${t('qLabel')}</label><input type="text" id="q" placeholder="${t('qPh')}" enterkeyhint="search" autocomplete="off" onkeydown="if(event.key==='Enter')doAsk()"></div>
   <button class="btn" id="go" onclick="doAsk()">${t('search')}</button><div id="out" aria-live="polite"></div>`;
 },
 custom(){
  return `<h2 class="h">${t('customTitle')}</h2>
   <div class="field"><label for="f">${t('chooseFile')}</label><label class="file"><input type="file" id="f" accept="application/pdf" onchange="$('fn').textContent=this.files[0]?this.files[0].name:'${t('noFile')}'"><span class="fb">${t('chooseFile')}</span><span class="fn" id="fn">${t('noFile')}</span></label></div>
   <div class="field"><label for="q">${t('qLabel')}</label><input type="text" id="q" placeholder="${t('qPh')}" autocomplete="off" onkeydown="if(event.key==='Enter')doCustom()"></div>
   <button class="btn" id="go" onclick="doCustom()">${t('analyze')}</button><div id="out" aria-live="polite"></div>`;
 }
};

function render(){
  document.documentElement.lang=state.lang;document.documentElement.dir=state.lang==='ar'?'rtl':'ltr';
  document.body.dir=document.documentElement.dir;
  $('ttl').textContent=t('title');$('sub').textContent=t('sub');
  $('lang').textContent=state.lang==='ar'?'English':'عربي';$('zclose').textContent=t('close');
  if(!state.cat){$('app').innerHTML='<div class="loading"><div class="spin"></div></div>';return}
  $('app').innerHTML=crumbs()+views[state.view]();
}

let timer=null,lastRun=null;
function loading(){
  const m=[t('l1'),t('l2'),t('l3')];let i=0;
  $('out').innerHTML=`<div class="loading"><div class="spin"></div><p id="lm" style="margin:0">${m[0]}</p></div>`;
  clearInterval(timer);timer=setInterval(()=>{i=Math.min(i+1,m.length-1);const e=$('lm');if(e)e.textContent=m[i]},2500);
}
function showError(msg,retry){
  $('out').innerHTML=`<div class="error"><p>${esc(msg)}</p></div>`+(retry?`<button class="btn ghost" onclick="run(lastRun)">${t('retry')}</button>`:'');
}
function showResult(d){
  const same=d.pdf_page===d.page_number;
  $('out').innerHTML=`<div class="answer"><div class="meta"><span class="chip">${t('page')} ${esc(d.page_number)}</span>${same?'':`<span class="chip soft">${t('pdfPage')} ${esc(d.pdf_page)}</span>`}</div>
   <p class="atext">${esc(d.answer)}</p>
   ${d.highlight?`<blockquote><small>${t('fromBook')}</small>${esc(d.highlight)}</blockquote>`:''}
   <figure><img src="${d.page_image}" alt="" onclick="zoomImg(this.src)"><figcaption>${t('pageCap')}</figcaption></figure></div>`;
}
function zoomImg(src){$('zimg').src=src;$('zoom').classList.add('on')}
async function run(doFetch){
  lastRun=doFetch;$('go').disabled=true;loading();
  const ctrl=new AbortController(),to=setTimeout(()=>ctrl.abort(),70000);
  try{
    const r=await doFetch(ctrl.signal);const txt=await r.text();let d;
    try{d=JSON.parse(txt)}catch(_){d={error_ar:t('errServer')}}
    clearInterval(timer);
    if(r.ok)showResult(d);else showError(d.error_ar||d.error||t('errServer'),d.retry!==false);
  }catch(e){clearInterval(timer);showError(e.name==='AbortError'?t('errSlow'):t('errNet'),true)}
  finally{clearTimeout(to);const b=$('go');if(b)b.disabled=false}
}
function doAsk(){
  const q=$('q').value.trim();if(!q){$('q').focus();return}
  const code=Sub().code;
  run(sig=>fetch(`/api/search?book_code=${encodeURIComponent(code)}&query=${encodeURIComponent(q)}`,{signal:sig}));
}
function doCustom(){
  const f=$('f').files[0],q=$('q').value.trim();
  if(!f){$('f').focus();return}
  if(!q){$('q').focus();return}
  run(sig=>{const fd=new FormData();fd.append('file',f);fd.append('query',q);return fetch('/api/custom-pdf-search',{method:'POST',body:fd,signal:sig})});
}

(async function init(){
  render();
  try{const r=await fetch('/api/catalog');state.cat=await r.json();}
  catch(e){$('app').innerHTML=`<div class="error"><p>${t('errServer')}</p></div><button class="btn ghost" onclick="location.reload()">${t('retry')}</button>`;return}
  history.replaceState({view:'home'},'');render();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
