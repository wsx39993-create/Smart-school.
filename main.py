mkdir -p /home/claude/parts && cat > /home/claude/parts/backend.py << 'PYEOF'
import os
import re
import json
import math
import time
import base64
import hashlib
import hmac
import tempfile
import threading
import unicodedata
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List
from urllib.parse import quote

import fitz  # PyMuPDF
import anthropic
from google import genai
from google.genai import types
from fastapi import FastAPI, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

# ═════════════════════════ الإعدادات ═════════════════════════
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client = (anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=45.0, max_retries=0)
          if ANTHROPIC_API_KEY else None)
gemini_client = (genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=45000))
                 if GEMINI_API_KEY else None)

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
GEMINI_MODELS = [m.strip() for m in
                 os.environ.get("GEMINI_MODELS", "gemini-3.8-flash,gemini-2.5-flash").split(",") if m.strip()]

# مفتاح لوحة الإدارة /admin (اضبطه من Render -> Environment)
ADMIN_KEY = os.environ.get("ADMIN_KEY")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# على Render ضع Persistent Disk واضبط BOOKS_DIR=/var/data/books حتى لا تضيع الكتب عند إعادة التشغيل
BOOKS_DIR = os.environ.get("BOOKS_DIR", os.path.join(BASE_DIR, "books"))
CACHE_DIR = os.path.join(BOOKS_DIR, ".cache")
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "smart_uploads")
for _d in (BOOKS_DIR, CACHE_DIR, UPLOAD_DIR):
    os.makedirs(_d, exist_ok=True)

MAX_UPLOAD = 500 * 1024 * 1024   # 500 ميجابايت
ANALYSIS_DPI = 100               # دقة الصور المرسلة للموديل (الكتب الممسوحة)
OCR_DPI = 110
DISPLAY_DPI = 110
IEN_URL = "https://ien.edu.sa"                    # منصة عين التعليمية
EIN_CHANNEL = "https://www.youtube.com/@dorosien"  # قناة عين على يوتيوب

ALIASES = {}  # مثال: {"s2_t1_physics": "physics2_t1"}

app = FastAPI(title="Smart Curriculum Educational Platform")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


class ApiError(Exception):
    def __init__(self, msg, status=400, retry=False):
        super().__init__(msg)
        self.msg, self.status, self.retry = msg, status, retry


class AIUnavailable(ApiError):
    def __init__(self, msg, status=503, retry=True):
        super().__init__(msg, status, retry)


# ═════════════════════════ كتالوج المواد ═════════════════════════
# كود الكتاب = {حرف المرحلة}{الصف}_t{الفصل}_{المادة}   مثال: p3_t1_math -> p3_t1_math.pdf
# والمحلول: p3_t1_math_solved.pdf       (قائمة كل الأسماء المطلوبة: /api/missing)
STAGES = [
    {"id": "primary", "key": "p", "years": 6, "ar": "المرحلة الابتدائية", "en": "Primary school"},
    {"id": "intermediate", "key": "m", "years": 3, "ar": "المرحلة المتوسطة", "en": "Middle school"},
    {"id": "secondary", "key": "s", "years": 3, "ar": "المرحلة الثانوية (نظام المسارات)", "en": "High school (Tracks)"},
]
ORD_AR = ["الأول", "الثاني", "الثالث", "الرابع", "الخامس", "السادس"]
TERMS = [(1, "الفصل الدراسي الأول", "First semester"), (2, "الفصل الدراسي الثاني", "Second semester")]


def year_names(sk, y):
    if sk == "p":
        return f"الصف {ORD_AR[y - 1]} الابتدائي", f"Grade {y} (Primary)"
    if sk == "m":
        return f"الصف {ORD_AR[y - 1]} المتوسط", f"Grade {y} (Middle)"
    return ({1: "الأول الثانوي (السنة المشتركة)", 2: "الثاني الثانوي", 3: "الثالث الثانوي"}[y],
            {1: "Year 1 (Common)", 2: "Year 2", 3: "Year 3"}[y])


# الثانوية (المسار العام) حسب خطة الفصلين الدراسيين المعلنة من الوزارة.
# الفصل الأول لكل سنة مبني على الخطة المعلنة؛ الفصل الثاني للثاني والثالث ثانوي جزئي، عدّله من books/catalog.json
SEC = {
    (1, 1): [("islamic", "القرآن الكريم والتفسير", "Quran & Tafseer"),
             ("kafaayat", "الكفايات اللغوية 1-1", "Language Competencies 1-1"),
             ("english", "اللغة الإنجليزية 1-1", "English 1-1"),
             ("math", "الرياضيات 1-1", "Mathematics 1-1"),
             ("tech", "التقنية الرقمية 1-1", "Digital Technology 1-1"),
             ("bio", "الأحياء 1", "Biology 1"),
             ("chem", "الكيمياء 1", "Chemistry 1"),
             ("critical", "التفكير الناقد", "Critical Thinking"),
             ("health", "التربية الصحية والبدنية 1", "Health & Physical Education 1")],
    (1, 2): [("islamic", "الحديث", "Hadith"),
             ("kafaayat", "الكفايات اللغوية 1-2", "Language Competencies 1-2"),
             ("english", "اللغة الإنجليزية 1-2", "English 1-2"),
             ("math", "الرياضيات 1-2", "Mathematics 1-2"),
             ("physics", "الفيزياء 1", "Physics 1"),
             ("env", "علم البيئة 1", "Environmental Science 1"),
             ("social", "الدراسات الاجتماعية 1", "Social Studies 1"),
             ("health", "التربية الصحية والبدنية 2", "Health & Physical Education 2")],
    (2, 1): [("kafaayat", "الكفايات اللغوية 2-1", "Language Competencies 2-1"),
             ("english", "اللغة الإنجليزية 2-1", "English 2-1"),
             ("math", "الرياضيات 2-1", "Mathematics 2-1"),
             ("physics", "الفيزياء 2", "Physics 2"),
             ("chem", "الكيمياء 2", "Chemistry 2"),
             ("bio", "الأحياء 2", "Biology 2"),
             ("history", "التاريخ", "History")],
    (2, 2): [("kafaayat", "الكفايات اللغوية 2-2", "Language Competencies 2-2"),
             ("english", "اللغة الإنجليزية 2-2", "English 2-2"),
             ("math", "الرياضيات 2-2", "Mathematics 2-2")],
    (3, 1): [("math", "الرياضيات 3-1", "Mathematics 3-1"),
             ("english", "اللغة الإنجليزية 3-1", "English 3-1"),
             ("physics", "الفيزياء 3", "Physics 3"),
             ("chem", "الكيمياء 3", "Chemistry 3"),
             ("earth", "علوم الأرض والفضاء", "Earth & Space Science"),
             ("tech", "التقنية الرقمية 3", "Digital Technology 3"),
             ("health", "اللياقة الصحية والبدنية", "Health & Fitness"),
             ("research", "البحث ومصادر المعلومات", "Research & Information Sources"),
             ("elective", "المجال الاختياري", "Elective Field")],
    (3, 2): [("math", "الرياضيات 3-2", "Mathematics 3-2"),
             ("english", "اللغة الإنجليزية 3-2", "English 3-2"),
             ("elective", "المجال الاختياري", "Elective Field")],
}


def _catalog_override():
    try:
        with open(os.path.join(BOOKS_DIR, "catalog.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def subjects_for(sk, year, term, override=None):
    """يرجع [(slug, عربي, إنجليزي)] لكل مادة. للتعديل بدون لمس الكود: books/catalog.json
    مثال: {"p3_t1": [["math","الرياضيات","Mathematics"], ["science","العلوم","Science"]]}"""
    ov = (override if override is not None else _catalog_override()).get(f"{sk}{year}_t{term}")
    if ov:
        return [tuple(x) for x in ov]
    if sk == "p":
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
    if sk == "m":
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
    return SEC.get((year, term), [])


_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def book_path(code, solved=False):
    if not _CODE_RE.match(code or ""):
        return None
    real = ALIASES.get(code, code)
    path = os.path.join(BOOKS_DIR, f"{real}{'_solved' if solved else ''}.pdf")
    return path if os.path.exists(path) else None


def load_links():
    try:
        with open(os.path.join(BOOKS_DIR, "links.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_links(d):
    p = os.path.join(BOOKS_DIR, "links.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


def build_catalog():
    links, ov = load_links(), _catalog_override()
    stages = []
    for st in STAGES:
        years = []
        for y in range(1, st["years"] + 1):
            yar, yen = year_names(st["key"], y)
            terms = []
            for tn, tar, ten in TERMS:
                subs = []
                for slug, sar, sen in subjects_for(st["key"], y, tn, ov):
                    code = f"{st['key']}{y}_t{tn}_{slug}"
                    lk = links.get(code, {})
                    video = lk.get("video") or (EIN_CHANNEL + "/search?query=" + quote(f"{sar} {yar} {tar}"))
                    subs.append({"code": code, "ar": sar, "en": sen,
                                 "pdf": book_path(code) is not None, "solved": book_path(code, True) is not None,
                                 "pdf_url": lk.get("pdf"), "solved_url": lk.get("solved"), "video": video})
                terms.append({"n": tn, "ar": tar, "en": ten, "subjects": subs})
            years.append({"n": y, "ar": yar, "en": yen, "terms": terms})
        stages.append({"id": st["id"], "ar": st["ar"], "en": st["en"], "years": years})
    return {"stages": stages, "ien": IEN_URL}


def all_codes():
    ov = _catalog_override()
    return {f"{st['key']}{y}_t{tn}_{slug}"
            for st in STAGES for y in range(1, st["years"] + 1) for tn, _, _ in TERMS
            for slug, _, _ in subjects_for(st["key"], y, tn, ov)}


# ═════════════════════════ معالجة النص العربي ═════════════════════════
_DIAC = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = _DIAC.sub("", s)
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ٱ", "ا"), ("ى", "ي"), ("ة", "ه")):
        s = s.replace(a, b)
    s = _PUNCT.sub(" ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


_STOP = {norm(w) for w in
         "ما ماذا من هو هي هل كيف لماذا متى اين في على عن الى ذلك هذا هذه الذي التي اذكر عرف وضح اشرح علل فسر عدد".split()}


def _stem(w):
    if w.startswith("ال") and len(w) > 4:
        w = w[2:]
    for suf in ("ات", "ون", "ين", "ان"):
        if len(w) >= 6 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def query_tokens(q: str):
    out = []
    for w in norm(q).split():
        if w in _STOP:
            continue
        w = _stem(w)
        if len(w) >= 2 and w not in out:
            out.append(w)
    return out


# ═════════════════════════ الفهرس (نص الكتاب + OCR للممسوح) ═════════════════════════
class DocIndex:
    def __init__(self, norm_pages, lens, scanned, ocr_done):
        self.norm, self.len = norm_pages, lens
        self.scanned, self.ocr_done = set(scanned), set(ocr_done)

    def pending(self):
        return sorted(self.scanned - self.ocr_done)

    def needs_ocr(self):
        return len(self.scanned) > 0.3 * max(1, len(self.norm))

    def pct(self):
        if not self.needs_ocr():
            return 100
        return int(100 * len(self.ocr_done & self.scanned) / max(1, len(self.scanned)))


_IDX = OrderedDict()
_IDX_LOCK = threading.Lock()
_KEY_LOCKS = {}
_OCR_JOBS = {}
_ANS_CACHE = OrderedDict()


def book_key(path):
    return f"{os.path.basename(path)}:{int(os.path.getmtime(path))}:{os.path.getsize(path)}"


def _cache_file(key):
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json")


def _save_index(key, idx):
    try:
        p = _cache_file(key)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"norm": idx.norm, "len": idx.len, "scanned": sorted(idx.scanned),
                       "ocr": sorted(idx.ocr_done)}, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    except Exception as e:
        print("[index] save failed:", e)


def _load_index(key):
    try:
        with open(_cache_file(key), encoding="utf-8") as f:
            d = json.load(f)
        return DocIndex(d["norm"], d["len"], d["scanned"], d["ocr"])
    except Exception:
        return None


def get_index(path, key):
    with _IDX_LOCK:
        if key in _IDX:
            _IDX.move_to_end(key)
            return _IDX[key]
        lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with lock:
        with _IDX_LOCK:
            if key in _IDX:
                return _IDX[key]
        idx = _load_index(key)
        if idx is None:
            doc = fitz.open(path)
            try:
                norms, lens, scanned = [], [], []
                for i, page in enumerate(doc):
                    t = page.get_text("text")
                    n = len(t.strip())
                    norms.append(norm(t))
                    lens.append(n)
                    if n < 40:
                        scanned.append(i)
            finally:
                doc.close()
            idx = DocIndex(norms, lens, scanned, [])
            _save_index(key, idx)
        with _IDX_LOCK:
            _IDX[key] = idx
            while len(_IDX) > 12:
                _IDX.popitem(last=False)
        return idx


OCR_INSTR = ("انسخ كل النص الظاهر في كل صفحة من الصور المرفقة كما هو وبالترتيب (عربي وإنجليزي وأرقام ومعادلات). "
             "ابدأ نص كل صفحة بسطر === PAGE n === حيث n هو الرقم المكتوب في عنوان الصورة. لا تضف أي شرح أو تعليق.")


def _ocr_group(path, group):
    d = fitz.open(path)
    try:
        pages = []
        for i in group:
            pix = d[i].get_pixmap(dpi=OCR_DPI)
            pages.append({"label": f"=== PAGE {i + 1} ===", "jpeg": pix.tobytes("jpeg", jpg_quality=70)})
    finally:
        d.close()
    raw, _ = run_models(pages, lambda p: OCR_INSTR, prefer="gemini", max_tokens=3500)
    parts = re.split(r"===\s*PAGE\s*(\d+)\s*===", raw)
    out = {}
    for j in range(1, len(parts) - 1, 2):
        out[int(parts[j]) - 1] = parts[j + 1].strip()
    return out


def _ocr_worker(path, key, idx):
    pend = idx.pending()
    groups = [pend[i:i + 3] for i in range(0, len(pend), 3)]
    done_count = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_ocr_group, path, g): g for g in groups}
        for f in as_completed(futs):
            try:
                for pi, text in f.result().items():
                    if 0 <= pi < len(idx.norm) and text:
                        idx.norm[pi] = norm(text)
                        idx.len[pi] = len(text)
                        idx.ocr_done.add(pi)
            except Exception as e:
                print("[ocr] group failed:", type(e).__name__, str(e)[:150])
            done_count += 1
            if done_count % 8 == 0:
                _save_index(key, idx)
    _save_index(key, idx)


def ensure_ocr(path, key, idx, wait=45):
    if not idx.needs_ocr() or not idx.pending():
        return
    with _IDX_LOCK:
        th = _OCR_JOBS.get(key)
        if not th or not th.is_alive():
            th = threading.Thread(target=_ocr_worker, args=(path, key, idx), daemon=True)
            _OCR_JOBS[key] = th
            th.start()
    th.join(wait)


def rank_pages(norm_pages, query, top_n=3):
    toks = query_tokens(query)
    if not toks:
        return [], 0.0
    n = len(norm_pages)
    df = {t: sum(1 for p in norm_pages if t in p) for t in toks}
    scored = []
    for i, p in enumerate(norm_pages):
        if not p:
            continue
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
    words = page.get_text("words")
    words.sort(key=lambda w: (w[5], w[6], w[7]))
    return words


def words_to_text(words, limit=4000):
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


# ═════════════════════════ الذكاء الاصطناعي ═════════════════════════
def _is_transient(e) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code in (408, 429, 500, 502, 503, 504, 529):
        return True
    text = f"{type(e).__name__} {e}".lower()
    return any(k in text for k in ("timeout", "timed out", "unavailable", "overloaded", "connection", "deadline"))


def call_claude(pages, instructions, max_tokens):
    content = []
    for p in pages:
        if p.get("text") is not None:
            content.append({"type": "text", "text": f"{p['label']}\n{p['text']}"})
        else:
            content.append({"type": "text", "text": p["label"]})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(p["jpeg"]).decode()}})
    content.append({"type": "text", "text": instructions})
    resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=max_tokens,
                                  messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def call_gemini(model, pages, instructions, max_tokens):
    contents = []
    for p in pages:
        contents.append(p["label"])
        contents.append(p["text"] if p.get("text") is not None
                        else types.Part.from_bytes(data=p["jpeg"], mime_type="image/jpeg"))
    contents.append(instructions)
    think = (types.ThinkingConfig(thinking_budget=0) if model.startswith("gemini-2.5")
             else types.ThinkingConfig(thinking_level="low"))
    resp = gemini_client.models.generate_content(
        model=model, contents=contents,
        config=types.GenerateContentConfig(thinking_config=think, max_output_tokens=max_tokens, temperature=0.2))
    return (resp.text or "").strip()


def run_models(pages, make_instructions, prefer=None, max_tokens=700):
    """يجرّب الموديلات بالترتيب؛ الخطأ المؤقت (503/429/مهلة) يعيد المحاولة مرة ثم ينتقل للتالي."""
    attempts = []
    if client:
        attempts.append(("claude", CLAUDE_MODEL))
    if gemini_client:
        attempts += [("gemini", m) for m in GEMINI_MODELS]
    if prefer == "gemini":
        attempts.sort(key=lambda a: 0 if a[0] == "gemini" else 1)
    if not attempts:
        raise AIUnavailable("لم يتم ضبط مفتاح: أضف ANTHROPIC_API_KEY أو GEMINI_API_KEY في إعدادات Render.",
                            status=500, retry=False)
    for provider, model in attempts:
        instructions = make_instructions(provider)
        for i in range(2):
            try:
                raw = (call_claude(pages, instructions, max_tokens) if provider == "claude"
                       else call_gemini(model, pages, instructions, max_tokens))
                if raw:
                    return raw, provider
                break
            except Exception as e:
                print(f"[AI] {provider}/{model} attempt {i + 1}: {type(e).__name__}: {str(e)[:200]}")
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
        if provider == "gemini":
            x0, y0, x1, y1 = v[1] / 1000 * w, v[0] / 1000 * h, v[3] / 1000 * w, v[2] / 1000 * h
        else:
            x0, y0, x1, y1 = v
        out.append([x0 - pad, y0 - pad, x1 + pad, y1 + pad])
    return out


TAIL = ("اكتب الإجابة نصاً عادياً بدون Markdown وبإيجاز (من جملتين إلى أربع جمل). "
        "إذا لم تجد الإجابة في الصفحات فقل ذلك بصراحة ولا تخترع إجابة.\nبعد الإجابة أضف الأسطر التالية بالضبط، كل واحد في سطر مستقل:\n")


def text_instructions(query):
    return (f"أنت معلم خبير في المناهج الدراسية. اعتماداً على نصوص صفحات الكتاب المرفقة فقط أجب بالعربية عن السؤال: '{query}'.\n" + TAIL +
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
    return (f"أنت معلم خبير في المناهج الدراسية. اعتماداً على صور صفحات الكتاب المرفقة فقط أجب بالعربية عن السؤال: '{query}'.\n" + TAIL +
            "PAGE: رقم الصفحة المرفقة كما في عنوان الصورة (أو 0)\n"
            "PRINTED_PAGE: الرقم المطبوع على الصفحة نفسها (غالباً أسفلها) أو 0\n" + boxes +
            "EXACT_TEXT: الجملة الدقيقة من الكتاب التي تحتوي الإجابة")


def render_page(doc, page_num, rects):
    page = doc[page_num]
    for r in rects:
        r = fitz.Rect(r) & page.rect
        if r.is_empty:
            continue
        r = r + (-1.5, -1.5, 1.5, 1.5)
        page.draw_rect(r, color=(0.78, 0.55, 0.05), fill=(1, 0.82, 0.2), fill_opacity=0.42, width=1, overlay=True)
    pix = page.get_pixmap(dpi=DISPLAY_DPI)
    return "data:image/jpeg;base64," + base64.b64encode(pix.tobytes("jpeg", jpg_quality=82)).decode()


NO_MATCH = "لم أجد في الكتاب كلمات قريبة من سؤالك. جرّب صياغة أخرى بكلمات تظهر في الدرس نفسه."


def answer_question(path, key, query):
    idx = get_index(path, key)
    ensure_ocr(path, key, idx, wait=45)
    pct = idx.pct()
    note = None if pct >= 100 else f"جاري تجهيز الكتاب (تمت قراءة {pct}% من صفحاته). أعد السؤال بعد قليل لنتيجة أدق."
    cands, best = rank_pages(idx.norm, query, top_n=3)
    if not cands or best <= 0:
        return {"page_number": None, "pdf_page": None, "answer": NO_MATCH, "highlight": "",
                "page_image": None, "note": note, "_cache": False}

    doc = fitz.open(path)
    try:
        text_cands = [i for i in cands if i not in idx.ocr_done and idx.len[i] >= 120]
        pages, dims = [], {}
        if text_cands and cands[0] in text_cands:
            cands = text_cands  # كتاب نصي: نرسل النص فقط (الأسرع)
            for i in cands:
                pages.append({"label": f"[صفحة رقم {i + 1}]", "text": words_to_text(page_words(doc[i]))})
            raw, provider = run_models(pages, lambda p: text_instructions(query))
        else:
            cands = cands[:2]  # صفحات ممسوحة: صورتان فقط
            for i in cands:
                pix = doc[i].get_pixmap(dpi=ANALYSIS_DPI)
                dims[i] = (pix.width, pix.height)
                pages.append({"label": f"صفحة رقم {i + 1} (أبعاد الصورة: {pix.width}x{pix.height} بكسل):",
                              "jpeg": pix.tobytes("jpeg", jpg_quality=75)})
            raw, provider = run_models(pages, lambda p: image_instructions(query, p))

        answer, page_idx, printed, boxes, highlight = parse_reply(raw, cands)
        rects = []
        if highlight and page_idx not in dims:
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
        return {"page_number": printed if printed else page_idx + 1, "pdf_page": page_idx + 1,
                "answer": answer or "لم يتم العثور على إجابة واضحة.", "highlight": highlight,
                "page_image": render_page(doc, page_idx, rects), "note": note, "_cache": pct >= 100}
    finally:
        doc.close()


def cached_answer(path, key, query):
    ck = (key, " ".join(query_tokens(query)) or norm(query))
    if ck in _ANS_CACHE:
        _ANS_CACHE.move_to_end(ck)
        return _ANS_CACHE[ck]
    result = answer_question(path, key, query)
    cacheable = result.pop("_cache", False)
    if cacheable:
        _ANS_CACHE[ck] = result
        while len(_ANS_CACHE) > 60:
            _ANS_CACHE.popitem(last=False)
    return result


def respond(fn):
    try:
        return fn()
    except ApiError as e:
        return JSONResponse(status_code=e.status, content={"error": e.msg, "error_ar": e.msg, "retry": e.retry})
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        msg = "حدث خطأ غير متوقع أثناء المعالجة. حاول مرة أخرى."
        return JSONResponse(status_code=500, content={"error": msg, "error_ar": msg, "detail": str(e)[:200], "retry": True})


# ═════════════════════════ الملفات والرفع ═════════════════════════
def save_stream(up: UploadFile, dest_tmp: str, limit=MAX_UPLOAD):
    h, size = hashlib.sha1(), 0
    with open(dest_tmp, "wb") as out:
        while True:
            chunk = up.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                out.close()
                os.remove(dest_tmp)
                raise ApiError("حجم الملف أكبر من 500 ميجابايت.", 413)
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest(), size


def check_pdf(path):
    try:
        d = fitz.open(path)
        n = len(d)
        d.close()
        return n
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        raise ApiError("الملف ليس PDF صالحاً.", 400)


def warm(path, key):
    try:
        get_index(path, key)
    except Exception as e:
        print("[warm] failed:", e)


def cleanup_uploads(max_age=12 * 3600):
    now = time.time()
    for fn in os.listdir(UPLOAD_DIR):
        p = os.path.join(UPLOAD_DIR, fn)
        try:
            if now - os.path.getmtime(p) > max_age:
                os.remove(p)
        except Exception:
            pass


@app.on_event("startup")
def _startup():
    def run():
        for fn in sorted(os.listdir(BOOKS_DIR)):
            if fn.lower().endswith(".pdf"):
                p = os.path.join(BOOKS_DIR, fn)
                warm(p, book_key(p))
    threading.Thread(target=run, daemon=True).start()


# ═════════════════════════ واجهات API ═════════════════════════
@app.get("/api/catalog")
def api_catalog():
    return build_catalog()


@app.get("/api/missing")
def api_missing():
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
        msg = "عذراً، الكتاب غير متوفر حالياً في المكتبة الرقمية."
        return JSONResponse(status_code=404, content={"error": msg, "error_ar": msg, "retry": False})
    return respond(lambda: cached_answer(path, book_key(path), query))


@app.post("/api/custom/upload")
def custom_upload(file: UploadFile = File(...)):
    def go():
        if not (file.filename or "").lower().endswith(".pdf"):
            raise ApiError("يجب أن يكون الملف بصيغة PDF فقط.", 400)
        cleanup_uploads()
        tmp = os.path.join(UPLOAD_DIR, uuid.uuid4().hex + ".part")
        sha, size = save_stream(file, tmp)
        final = os.path.join(UPLOAD_DIR, sha + ".pdf")
        if os.path.exists(final):
            os.remove(tmp)
            os.utime(final, None)
        else:
            os.replace(tmp, final)
        pages = check_pdf(final)
        threading.Thread(target=warm, args=(final, sha), daemon=True).start()
        return {"file_id": sha, "pages": pages, "size": size}
    return respond(go)


@app.get("/api/custom/ask")
def custom_ask(file_id: str = Query(...), query: str = Query(..., min_length=2, max_length=300)):
    if not re.fullmatch(r"[0-9a-f]{40}", file_id):
        return JSONResponse(status_code=400, content={"error_ar": "معرّف الملف غير صحيح.", "retry": False})
    path = os.path.join(UPLOAD_DIR, file_id + ".pdf")
    if not os.path.exists(path):
        msg = "انتهت صلاحية الملف المرفوع. أعد رفعه من فضلك."
        return JSONResponse(status_code=410, content={"error": msg, "error_ar": msg, "retry": False})
    os.utime(path, None)
    return respond(lambda: cached_answer(path, file_id, query))


# ───────────── لوحة الإدارة ─────────────
def _admin(key):
    if not ADMIN_KEY:
        raise ApiError("اضبط المتغير ADMIN_KEY في إعدادات Render أولاً.", 403)
    if not hmac.compare_digest(key or "", ADMIN_KEY):
        raise ApiError("مفتاح الإدارة غير صحيح.", 403)


def _save_book(up, code, kind):
    if code not in all_codes():
        raise ApiError(f"الكود غير موجود في الكتالوج: {code}", 400)
    dest = os.path.join(BOOKS_DIR, f"{code}{'_solved' if kind == 'solved' else ''}.pdf")
    tmp = dest + ".part"
    save_stream(up, tmp)
    check_pdf(tmp)
    os.replace(tmp, dest)
    threading.Thread(target=warm, args=(dest, book_key(dest)), daemon=True).start()
    return dest


@app.post("/api/admin/upload")
def admin_upload(key: str = Form(...), code: str = Form(...), kind: str = Form("pdf"), file: UploadFile = File(...)):
    def go():
        _admin(key)
        _save_book(file, code, "solved" if kind == "solved" else "pdf")
        return {"ok": True, "code": code, "kind": kind}
    return respond(go)


@app.post("/api/admin/bulk")
def admin_bulk(key: str = Form(...), files: List[UploadFile] = File(...)):
    def go():
        _admin(key)
        results = []
        for up in files:
            base = re.sub(r"\.pdf$", "", up.filename or "", flags=re.I)
            kind = "solved" if base.endswith("_solved") else "pdf"
            code = base[:-7] if kind == "solved" else base
            try:
                _save_book(up, code, kind)
                results.append({"file": up.filename, "ok": True})
            except ApiError as e:
                results.append({"file": up.filename, "ok": False, "error": e.msg})
        return {"results": results}
    return respond(go)


@app.post("/api/admin/link")
def admin_link(key: str = Form(...), code: str = Form(...), kind: str = Form(...), url: str = Form("")):
    def go():
        _admin(key)
        if code not in all_codes() or kind not in ("pdf", "solved", "video"):
            raise ApiError("بيانات غير صحيحة.", 400)
        if url and not url.startswith(("http://", "https://")):
            raise ApiError("الرابط يجب أن يبدأ بـ https://", 400)
        links = load_links()
        entry = links.setdefault(code, {})
        if url:
            entry[kind] = url
        else:
            entry.pop(kind, None)
        save_links(links)
        return {"ok": True}
    return respond(go)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=HTML_PAGE)


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(content=ADMIN_PAGE)


PYEOF
echo done
