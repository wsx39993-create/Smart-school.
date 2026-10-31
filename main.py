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
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from typing import List
from urllib.parse import quote

import fitz  # PyMuPDF
import anthropic
from google import genai
from google.genai import types
from fastapi import FastAPI, Query, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

# ═════════════════════════ الإعدادات ═════════════════════════
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip() or None

client = (anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0, max_retries=0)
          if ANTHROPIC_API_KEY else None)
gemini_client = (genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=60000))
                 if GEMINI_API_KEY else None)

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_FALLBACKS = [m.strip() for m in os.environ.get("CLAUDE_FALLBACK", "claude-sonnet-5-5,claude-sonnet-4-6").split(",")
                    if m.strip() and m.strip() != CLAUDE_MODEL]
GEMINI_MODELS = [m.strip() for m in
                 os.environ.get("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]

ADMIN_KEY = (os.environ.get("ADMIN_KEY") or "").strip() or None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# على Render ضع Persistent Disk واضبط BOOKS_DIR=/var/data/books حتى لا تضيع الكتب عند إعادة التشغيل
BOOKS_DIR = os.environ.get("BOOKS_DIR", os.path.join(BASE_DIR, "books"))
CACHE_DIR = os.path.join(BOOKS_DIR, ".cache")
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "smart_uploads")
for _d in (BOOKS_DIR, CACHE_DIR, UPLOAD_DIR):
    os.makedirs(_d, exist_ok=True)

MAX_UPLOAD = 500 * 1024 * 1024   # 500 ميجابايت
ANALYSIS_DPI = 120
OCR_DPI = 110
DISPLAY_DPI = 110
HEDGE_SECONDS = float(os.environ.get("HEDGE_SECONDS", "4.0"))              # إذا تأخر الموديل الأول أكثر من هذا يبدأ الثاني بالتوازي (أسرع نتيجة تفوز)
WEB_SEARCH = os.environ.get("WEB_SEARCH", "1") == "1"   # بحث قوقل/الإنترنت للتحقق قبل الإجابة (اضبطه 0 لإيقافه)
WEB_TIMEOUT = float(os.environ.get("WEB_TIMEOUT", "12"))
CAND_PAGES = 5                   # عدد الصفحات المرشحة التي يقرؤها الموديل
MAX_IMG_PAGES = 3                # أقصى صفحات تُرسل كصور في الكتب الكبيرة
SMALL_DOC = 8                    # ملف بهذا الحجم أو أقل: يقرأ الموديل كل صفحاته مباشرة
TEXT_PAGES = 3                   # عدد الصفحات المرسلة للموديل في الكتب النصية
IEN_URL = "https://ien.edu.sa"
EIN_CHANNEL = "https://www.youtube.com/@dorosien"

ALIASES = {}

app = FastAPI(title="Smart Curriculum Educational Platform")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


class ApiError(Exception):
    def __init__(self, msg, status=400, retry=False, detail=None):
        super().__init__(msg)
        self.msg, self.status, self.retry, self.detail = msg, status, retry, detail


class AIUnavailable(ApiError):
    def __init__(self, msg, status=503, retry=True, detail=None):
        super().__init__(msg, status, retry, detail)


# ═════════════════════════ حماية من الاستهلاك الزائد ═════════════════════════
_RATE = {}
_RATE_LOCK = threading.Lock()


def client_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "?"))


def rate_ok(request: Request, bucket: str, limit: int, window: int = 60):
    now = time.time()
    k = (client_ip(request), bucket)
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(k, []) if now - t < window]
        if len(hits) >= limit:
            _RATE[k] = hits
            return False
        hits.append(now)
        _RATE[k] = hits
        if len(_RATE) > 5000:
            for kk in list(_RATE)[:2000]:
                _RATE.pop(kk, None)
    return True


def too_many():
    msg = "طلبات كثيرة خلال وقت قصير. انتظر دقيقة ثم أعد المحاولة."
    return JSONResponse(status_code=429, content={"error": msg, "error_ar": msg, "retry": True})


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


# الثانوية (المسار العام): الفصل الأول لكل سنة مبني على الخطة المعلنة. الفصل الثاني للسنة الثانية والثالثة
# غير مؤكد، عدّله من /admin (تعديل مواد الفصل) بدون لمس الكود.
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

_GROUP_RE = re.compile(r"^[pms][1-6]_t[12]$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9\-]+$")  # بدون شرطة سفلية حتى لا يتعارض مع صيغة الكود


def _catalog_override():
    try:
        with open(os.path.join(BOOKS_DIR, "catalog.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def subjects_for(sk, year, term, override=None):
    ov = (override if override is not None else _catalog_override()).get(f"{sk}{year}_t{term}")
    if ov:
        return [tuple(x) for x in ov]
    if sk == "p":
        s = [("quran", "القرآن الكريم", "Holy Quran"),
             ("islamic", "الدراسات الإسلامية", "Islamic Studies"),
             ("lugati", "اللغة العربية (لغتي)", "Arabic Language (Lughati)"),
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
        s = [("quran", "القرآن الكريم", "Holy Quran"),
             ("islamic", "الدراسات الإسلامية", "Islamic Studies"),
             ("arabic", "اللغة العربية (لغتي الخالدة)", "Arabic Language"),
             ("social", "الدراسات الاجتماعية", "Social Studies"),
             ("math", "الرياضيات", "Mathematics"),
             ("science", "العلوم", "Science"),
             ("english", "اللغة الإنجليزية", "English Language"),
             ("digital", "المهارات الرقمية", "Digital Skills")]
        if year == 3:
            s.append(("critical", "التفكير الناقد", "Critical Thinking"))
        s += [("pe", "التربية البدنية", "Physical Education"),
              ("art", "التربية الفنية", "Art Education"),
              ("life", "المهارات الحياتية والأسرية", "Life & Family Skills")]
        return s
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


def _write_json(name, data):
    p = os.path.join(BOOKS_DIR, name)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


def save_links(d):
    _write_json("links.json", d)


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
         ("ما ماذا ماهو ماهي من هو هي هل كيف لماذا ليش ليه متى اين وين في على عن الى ذلك هذا هذه الذي التي "
          "ايش وش شو ايه اذكر عرف وضح اشرح علل فسر عدد لي لنا معنى").split()}


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
        if len(w) >= 5 and w[0] in "لبو" and w[1:] not in out:   # لكبلر -> كبلر
            out.append(w[1:])
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
_OCR_SEM = threading.Semaphore(8)     # حد أقصى لطلبات OCR المتزامنة عبر كل الكتب
_ANS_CACHE = OrderedDict()
_ANS_LOCK = threading.Lock()


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
            pages.append({"label": f"=== PAGE {i + 1} ===", "jpeg": render_jpeg(d[i], OCR_DPI, quality=70)[0]})
    finally:
        d.close()
    with _OCR_SEM:
        raw, _ = run_models(pages, lambda p: OCR_INSTR, prefer="gemini", max_tokens=3500, hedge=25.0)
    parts = re.split(r"===\s*PAGE\s*(\d+)\s*===", raw)
    out = {}
    for j in range(1, len(parts) - 1, 2):
        out[int(parts[j]) - 1] = parts[j + 1].strip()
    return out


def _ocr_worker(path, key, idx):
    pend = idx.pending()
    groups = [pend[i:i + 3] for i in range(0, len(pend), 3)]
    done_count = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
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
    """يبدأ OCR في الخلفية للكتب الممسوحة. wait=0 يعني ابدأ ولا تنتظر (يُستخدم عند الرفع)."""
    if not idx.needs_ocr() or not idx.pending():
        return
    with _IDX_LOCK:
        th = _OCR_JOBS.get(key)
        if not th or not th.is_alive():
            th = threading.Thread(target=_ocr_worker, args=(path, key, idx), daemon=True)
            _OCR_JOBS[key] = th
            th.start()
    if wait:
        th.join(wait)


def _proximity(page_norm, toks):
    comp = page_norm.replace(" ", "")
    pos = []
    for t in toks:
        ps, start = [], 0
        while len(ps) < 25:
            j = comp.find(t, start)
            if j < 0:
                break
            ps.append(j)
            start = j + 1
        if ps:
            pos.append(ps)
    bonus = 0.0
    for x in range(len(pos)):
        for y in range(x + 1, len(pos)):
            d = min(abs(a - b) for a in pos[x] for b in pos[y])
            bonus += 2.0 / (1 + d / 40.0)
    return min(bonus, 5.0)


def rank_pages(norm_pages, query, top_n=5):
    toks = query_tokens(query)
    if not toks:
        return [], 0.0
    comp = [p.replace(" ", "") for p in norm_pages]   # بعض كتب الوزارة فيها مسافات خاطئة داخل الكلمات
    n = len(comp)
    df = {t: sum(1 for p in comp if t in p) for t in toks}
    scored = []
    for i, p in enumerate(comp):
        if not p:
            continue
        sc, hit = 0.0, 0
        for t in toks:
            c = p.count(t)
            if c:
                hit += 1
                sc += (1 + math.log(c)) * math.log(1 + n / (1 + df[t]))
        if not hit:
            continue
        sc *= 0.5 + 0.5 * hit / len(toks)
        for a, b in zip(toks, toks[1:]):
            if a + b in p or b + a in p:
                sc += 1.5
        sc /= (1 + len(p) / 6000.0) ** 0.3
        scored.append((sc, i))
    scored.sort(reverse=True)
    rer = sorted(((sc + _proximity(norm_pages[i], toks), i) for sc, i in scored[:12]), reverse=True)
    top = rer[:top_n]
    return [i for _, i in top], (top[0][0] if top else 0.0)


def page_words(page):
    words = page.get_text("words")
    words.sort(key=lambda w: (w[5], w[6], w[7]))
    return words


def words_to_text(words, limit=3000):
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


def call_claude(pages, instructions, max_tokens, model=None):
    content = []
    for p in pages:
        if p.get("text") is not None:
            content.append({"type": "text", "text": f"{p['label']}\n{p['text']}"})
        else:
            content.append({"type": "text", "text": p["label"]})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(p["jpeg"]).decode()}})
    content.append({"type": "text", "text": instructions})
    resp = client.messages.create(model=model or CLAUDE_MODEL, max_tokens=max_tokens,
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


LAST_AI_ERRORS = []


def _note_ai_error(provider, model, e):
    LAST_AI_ERRORS.append(f"{provider}/{model}: {type(e).__name__}: {str(e)[:160]}")
    del LAST_AI_ERRORS[:-10]


def run_models(pages, make_instructions, prefer=None, max_tokens=600, hedge=HEDGE_SECONDS):
    """يشغّل الموديل الأول؛ إذا تأخر أكثر من `hedge` ثانية يطلق الموديل التالي بالتوازي
    ويأخذ أول رد صالح. هذا يلغي انتظار المهلة الطويلة عند بطء مزوّد واحد."""
    attempts = []
    if client:
        attempts.append(("claude", CLAUDE_MODEL))
    if gemini_client:
        attempts += [("gemini", m) for m in GEMINI_MODELS]
    if client:
        attempts += [("claude", m) for m in CLAUDE_FALLBACKS]
    if prefer == "gemini":
        attempts.sort(key=lambda a: 0 if a[0] == "gemini" else 1)
    if not attempts:
        raise AIUnavailable("لم يتم ضبط مفتاح: أضف ANTHROPIC_API_KEY أو GEMINI_API_KEY في إعدادات Render.",
                            status=500, retry=False)

    def one(provider, model):
        instructions = make_instructions(provider)
        for i in range(2):
            try:
                raw = (call_claude(pages, instructions, max_tokens, model) if provider == "claude"
                       else call_gemini(model, pages, instructions, max_tokens))
                return (raw, provider) if raw else None
            except Exception as e:
                print(f"[AI] {provider}/{model} attempt {i + 1}: {type(e).__name__}: {str(e)[:200]}")
                _note_ai_error(provider, model, e)
                if i == 0 and _is_transient(e):
                    time.sleep(0.8)
                    continue
                return None
        return None

    ex = ThreadPoolExecutor(max_workers=len(attempts))
    pending, nxt, result = set(), 0, None
    try:
        while result is None and (pending or nxt < len(attempts)):
            if not pending or False:
                pending.add(ex.submit(one, *attempts[nxt]))
                nxt += 1
            done, _ = wait(pending, timeout=hedge if nxt < len(attempts) else None, return_when=FIRST_COMPLETED)
            if not done:                       # تأخر الأول: أطلق التالي بالتوازي
                pending.add(ex.submit(one, *attempts[nxt]))
                nxt += 1
                continue
            for f in done:
                pending.discard(f)
                r = f.result()
                if r:
                    result = r
                    break
    finally:
        ex.shutdown(wait=False)
    if result:
        return result
    raise AIUnavailable("تعذّر الوصول للذكاء الاصطناعي الآن. أعد المحاولة بعد لحظات.",
                        detail=" | ".join(LAST_AI_ERRORS[-3:]) or None)


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


TAIL = ("قواعد الإجابة:\n"
        "1) اقرأ كل الصفحات المرفقة أولاً، واختر الصفحة التي فيها الإجابة الفعلية (تعريف أو نص قانون أو شرح أو حل). "
        "تجاهل صفحات الأهداف والمفردات والفهارس إذا وُجد شرح أوضح في صفحة أخرى.\n"
        "2) ابدأ بالجواب المباشر الدقيق في جملة أو جملتين، ثم أضف شرحاً مبسطاً كأنك معلم (حتى خمس جمل). "
        "وإذا كان السؤال مسألة حسابية فاعرض خطوات الحل والناتج النهائي بوحدته.\n"
        "3) التزم بنص الكتاب ومصطلحاته العلمية عند الاقتباس.\n"
        "4) إذا لم تجد الإجابة في الصفحات المرفقة فاذكر ذلك في أول سطر، ثم أجب باختصار من معرفتك العامة مع عبارة (من خارج الكتاب)، واجعل PAGE: 0.\n"
        "5) اكتب نصاً عادياً بدون Markdown وبالعربية الفصحى المبسطة.\n"
        "بعد الإجابة أضف الأسطر التالية بالضبط، كل واحد في سطر مستقل:\n")


def _ref_block(ref):
    return (f"مرجع من بحث الإنترنت (للاسترشاد والتحقق فقط، والأولوية لما هو مكتوب في الكتاب): {ref}\n" if ref else "")


def text_instructions(query, ref=""):
    return (f"أنت معلم خبير ودقيق في المناهج الدراسية. سؤال الطالب: '{query}'.\n" + _ref_block(ref) +
            "المرفق نصوص صفحات من الكتاب، وكل صفحة بعنوان يبيّن رقمها.\n" + TAIL +
            "PAGE: رقم الصفحة كما في عنوانها (أو 0 إذا لم تجد الإجابة في الصفحات)\n"
            "PRINTED_PAGE: الرقم المطبوع على الصفحة إن ظهر في نصها، وإلا 0\n"
            "EXACT_TEXT: الجملة القصيرة (حتى 25 كلمة) من الكتاب التي تحتوي الإجابة، منسوخة حرفياً من النص (اتركه فارغاً إذا PAGE: 0)")


def image_instructions(query, provider, ref=""):
    if provider == "gemini":
        boxes = ("BOXES: مصفوفة JSON من المستطيلات [ymin,xmin,ymax,xmax] بإحداثيات من 0 إلى 1000 على صورة تلك الصفحة، "
                 "مستطيل لكل سطر من سطور الجملة التي فيها الإجابة، مثال: [[300,150,330,700]]. إذا لم تجد اكتب []\n")
    else:
        boxes = ("BOXES: مصفوفة JSON من المستطيلات [x0,y0,x1,y1] بالبكسل على صورة تلك الصفحة (0,0 أعلى اليسار)، "
                 "مستطيل لكل سطر من سطور الجملة التي فيها الإجابة، مثال: [[80,300,500,324]]. إذا لم تجد اكتب []\n")
    return (f"أنت معلم خبير ودقيق في المناهج الدراسية. سؤال الطالب: '{query}'.\n" + _ref_block(ref) +
            "المرفق صفحات من الكتاب (صور أو نصوص)، وكل صفحة بعنوان يبيّن رقمها.\n" + TAIL +
            "PAGE: رقم الصفحة المرفقة كما في عنوانها (أو 0)\n"
            "PRINTED_PAGE: الرقم المطبوع على الصفحة نفسها (غالباً أسفلها) أو 0\n" + boxes +
            "EXACT_TEXT: الجملة الدقيقة من الكتاب التي تحتوي الإجابة (اتركه فارغاً إذا PAGE: 0)")


def render_jpeg(page, dpi, max_side=1600, quality=75, max_bytes=3_500_000):
    """يرسم الصفحة JPEG بحجم آمن للموديل (أقصى ضلع 1600 بكسل وأقل من 3.5MB).
    الملفات المحوّلة من صور تكون صفحتها ضخمة، والموديل يرفض الصور الكبيرة."""
    r = page.rect
    side = max(r.width, r.height) or 1
    z = min(dpi / 72.0, max_side / side)
    q = quality
    while True:
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z))
        jpg = pix.tobytes("jpeg", jpg_quality=q)
        if len(jpg) <= max_bytes or z < 0.2:
            return jpg, pix.width, pix.height, z
        if q > 50:
            q -= 15
        else:
            z *= 0.75


def render_page(doc, page_num, rects):
    page = doc[page_num]
    for r in rects:
        r = fitz.Rect(r) & page.rect
        if r.is_empty:
            continue
        r = r + (-1.5, -1.5, 1.5, 1.5)
        page.draw_rect(r, color=(0.78, 0.55, 0.05), fill=(1, 0.82, 0.2), fill_opacity=0.42, width=1, overlay=True)
    jpg = render_jpeg(page, DISPLAY_DPI, max_side=1800, quality=80)[0]
    return "data:image/jpeg;base64," + base64.b64encode(jpg).decode()


NO_MATCH = "لم أجد في الكتاب كلمات قريبة من سؤالك. جرّب صياغة أخرى بكلمات تظهر في الدرس نفسه."


_AR_LETTER = re.compile(r"[\u0621-\u064A]")


def text_is_broken(text):
    """بعض كتب الوزارة تحمل طبقة نص مقطّعة ("ا لقانو ن")؛ نكتشفها ونستعمل الصورة بدلاً منها."""
    toks = (text or "").split()
    if len(toks) < 15:
        return False
    single = sum(1 for w in toks if len(w) == 1 and _AR_LETTER.match(w))
    return single / len(toks) > 0.12


def llm_page_text(page, toks, limit=2400):
    lines = [l.strip() for l in page.get_text("text").split("\n") if l.strip()]
    full = "\n".join(lines)
    if len(full) <= limit:
        return full
    comp = [norm(l).replace(" ", "") for l in lines]
    sc = [sum(1 for t in toks if t in c) for c in comp]
    center = max(range(len(lines)), key=lambda i: sc[i]) if any(sc) else 0
    lo = hi = center
    size = len(lines[center])
    while True:
        grew = False
        if lo > 0 and size + len(lines[lo - 1]) <= limit:
            lo -= 1
            size += len(lines[lo])
            grew = True
        if hi < len(lines) - 1 and size + len(lines[hi + 1]) <= limit:
            hi += 1
            size += len(lines[hi])
            grew = True
        if not grew:
            break
    keep = lines[lo:hi + 1]
    if hi < len(lines) - 2:
        keep = keep + lines[-2:]       # آخر سطرين غالباً فيهما رقم الصفحة المطبوع
    return "\n".join(keep)


def _line_groups(page):
    lines = OrderedDict()
    for w in page_words(page):
        lines.setdefault((w[5], w[6]), []).append(w)
    return list(lines.values())


def local_snippet(page, toks, span=2):
    """أفضل سطرين متتاليين فيهما كلمات السؤال: يرجع (النص، مستطيلات التظليل)."""
    groups = _line_groups(page)
    if not groups or not toks:
        return "", []
    texts = [norm(" ".join(w[4] for w in g)).replace(" ", "") for g in groups]
    sc = [sum(1 for t in toks if t in tx) for tx in texts]
    best, bi = 0.0, -1
    for i in range(len(groups)):
        v = sum(sc[i:i + span]) + (0.5 if sc[i] else 0)
        if v > best:
            best, bi = v, i
    if bi < 0:
        return "", []
    sel = groups[bi:bi + span]
    rects = []
    for g in sel:
        r = fitz.Rect(g[0][:4])
        for w in g[1:]:
            r = r | fitz.Rect(w[:4])
        rects.append(r)
    text = "\n".join(unicodedata.normalize("NFKC", " ".join(w[4] for w in g)) for g in sel)
    return text, rects


def locate_local(path, key, query):
    """بحث محلي فوري بدون ذكاء اصطناعي: رقم الصفحة المحتملة + تظليل (يُستبدل بإجابة الذكاء الاصطناعي)."""
    idx = get_index(path, key)
    cands, best = rank_pages(idx.norm, query, top_n=3)
    if not cands or best <= 0:
        return {"found": False}
    doc = fitz.open(path)
    try:
        pi = cands[0]
        text, rects = local_snippet(doc[pi], query_tokens(query))
        if text_is_broken(doc[pi].get_text("text")):
            text = ""
        return {"found": True, "page_number": pi + 1, "pdf_page": pi + 1, "snippet": text,
                "page_image": render_page(doc, pi, rects)}
    finally:
        doc.close()


def local_fallback(doc, pi, toks):
    text, rects = local_snippet(doc[pi], toks)
    if text_is_broken(doc[pi].get_text("text")):
        text = ""
    return {"page_number": pi + 1, "pdf_page": pi + 1,
            "answer": text or "هذه أقرب صفحة لسؤالك، وقد ظُلّل الجزء المحتمل فيها.",
            "highlight": "", "page_image": render_page(doc, pi, rects),
            "note": "الذكاء الاصطناعي لا يستجيب الآن، وهذه أقرب فقرة في الكتاب لسؤالك.",
            "detail": " | ".join(LAST_AI_ERRORS[-2:]) or None, "_cache": False}


_WEB_POOL = ThreadPoolExecutor(max_workers=4)
_WEB_CACHE = OrderedDict()
AI_STATUS = {"selftest": [], "at": None}
WEB_PROMPT = ("أنت باحث تعليمي. ابحث في الإنترنت (في مصادر موثوقة) عن الإجابة الدقيقة لسؤال طالب في المنهج السعودي، "
              "ثم أجب بهذا الشكل بالضبط:\n"
              "ANSWER: الإجابة الدقيقة بالعربية في جملتين إلى أربع جمل\n"
              "TERMS: من 8 إلى 12 مصطلحاً وعبارة بالعربية الفصحى كما قد تظهر في نص الكتاب المدرسي، مفصولة بفواصل\n"
              "سؤال الطالب: ")


def _think_cfg(model):
    return (types.ThinkingConfig(thinking_budget=0) if model.startswith("gemini-2.5")
            else types.ThinkingConfig(thinking_level="low"))


def _web_gemini(query):
    m = GEMINI_MODELS[0]
    r = gemini_client.models.generate_content(
        model=m, contents=WEB_PROMPT + query,
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())],
                                           thinking_config=_think_cfg(m), max_output_tokens=600, temperature=0.1))
    return (r.text or "").strip()


def _web_claude(query):
    r = client.messages.create(model=CLAUDE_MODEL, max_tokens=800,
                               tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
                               messages=[{"role": "user", "content": WEB_PROMPT + query}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()


def web_reference(query):
    """يبحث في الإنترنت عن الإجابة الصحيحة ويرجع (الإجابة، مصطلحات تُستعمل للبحث داخل الكتاب)."""
    if not WEB_SEARCH:
        return "", []
    ck = " ".join(query_tokens(query)) or norm(query)
    if ck in _WEB_CACHE:
        return _WEB_CACHE[ck]
    fns = []
    if gemini_client and GEMINI_MODELS:
        fns.append(_web_gemini)
    if client:
        fns.append(_web_claude)
    for fn in fns:
        try:
            raw = fn(query)
            a = re.search(r"ANSWER\s*:\s*(.+?)(?=\n\s*TERMS\s*:|$)", raw, re.S)
            t = re.search(r"TERMS\s*:\s*(.+)$", raw, re.S)
            ans = (a.group(1).strip() if a else raw.strip())[:900]
            terms = [x.strip() for x in re.split(r"[،,\n]", t.group(1))] if t else []
            terms = [x for x in terms if x][:14]
            if ans:
                _WEB_CACHE[ck] = (ans, terms)
                while len(_WEB_CACHE) > 80:
                    _WEB_CACHE.popitem(last=False)
                return ans, terms
        except Exception as e:
            print(f"[WEB] {fn.__name__}: {type(e).__name__}: {str(e)[:200]}")
            _note_ai_error("web", fn.__name__, e)
    return "", []


def run_ai_selftest():
    """يجرّب كل موديل (وبحث الإنترنت) بطلب صغير ويخزّن النتيجة؛ تظهر في /api/ai-status وفي سجل Render."""
    pages = [{"label": "[اختبار]", "text": "اكتب كلمة واحدة فقط: تم"}]
    jobs = []
    if client:
        jobs += [("claude", m) for m in [CLAUDE_MODEL] + CLAUDE_FALLBACKS]
    if gemini_client:
        jobs += [("gemini", m) for m in GEMINI_MODELS]
    if WEB_SEARCH and gemini_client and GEMINI_MODELS:
        jobs.append(("web-gemini", GEMINI_MODELS[0]))
    if WEB_SEARCH and client:
        jobs.append(("web-claude", CLAUDE_MODEL))

    def one(job):
        provider, model = job
        t0 = time.time()
        try:
            if provider == "claude":
                out = call_claude(pages, "أجب بكلمة واحدة.", 30, model)
            elif provider == "gemini":
                out = call_gemini(model, pages, "أجب بكلمة واحدة.", 30)
            elif provider == "web-gemini":
                out = _web_gemini("ما عاصمة المملكة العربية السعودية؟")
            else:
                out = _web_claude("ما عاصمة المملكة العربية السعودية؟")
            return {"provider": provider, "model": model, "ok": bool(out), "ms": int((time.time() - t0) * 1000),
                    "error": "" if out else "رد فارغ"}
        except Exception as e:
            return {"provider": provider, "model": model, "ok": False, "ms": int((time.time() - t0) * 1000),
                    "error": f"{type(e).__name__}: {str(e)[:220]}"}
    if not jobs:
        AI_STATUS["selftest"], AI_STATUS["at"] = [], time.strftime("%Y-%m-%d %H:%M:%S")
        return []
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        res = list(ex.map(one, jobs))
    AI_STATUS["selftest"], AI_STATUS["at"] = res, time.strftime("%Y-%m-%d %H:%M:%S")
    for r in res:
        print("[AI-SELFTEST]", r)
    return res


def rrf(lists, k=3):
    sc = {}
    for lst in lists:
        for r, p in enumerate(lst):
            sc[p] = sc.get(p, 0) + 1.0 / (k + r)
    return [p for p, _ in sorted(sc.items(), key=lambda x: -x[1])]


def expand_query(query):
    """إذا لم تُوجد كلمات السؤال في الكتاب (لهجة عامية أو مرادفات) نطلب من الموديل كلمات فصيحة مرادفة."""
    try:
        pages = [{"label": "[سؤال طالب]", "text": query}]
        raw, _ = run_models(
            pages, lambda p: ("هذا سؤال طالب عن كتاب مدرسي سعودي وقد يكون بلهجة عامية. اكتب من 6 إلى 10 كلمات مفتاحية فصيحة "
                              "ومصطلحات علمية ومرادفات قد ترد في نص الدرس، مفصولة بمسافات فقط وبدون أي شرح."),
            max_tokens=60, hedge=3.0)
        return raw.strip()[:200]
    except Exception:
        return ""


def answer_question(path, key, query):
    idx = get_index(path, key)
    n_pages = len(idx.norm)
    small = n_pages <= SMALL_DOC        # ملف صغير (حتى صفحة واحدة): الموديل يقرأ كل صفحاته مباشرة
    fut = _WEB_POOL.submit(web_reference, query) if (WEB_SEARCH and not small) else None
    if not small:
        ensure_ocr(path, key, idx, wait=25)
    pct = 100 if small else idx.pct()
    note = None if pct >= 100 else f"جاري تجهيز الكتاب (تمت قراءة {pct}% من صفحاته). أعد السؤال بعد قليل لنتيجة أدق."
    ref, terms = "", []
    if fut is not None:
        try:
            ref, terms = fut.result(timeout=WEB_TIMEOUT)
        except Exception:
            ref, terms = "", []
    toks = query_tokens(query + " " + " ".join(terms))
    if small:
        cands = list(range(n_pages))
    else:
        cands, best = rank_pages(idx.norm, query, top_n=CAND_PAGES)
        if terms:
            tc, tb = rank_pages(idx.norm, " ".join(terms), top_n=CAND_PAGES)
            cands, best = rrf([cands, tc])[:CAND_PAGES], max(best, tb)
        elif not cands:
            extra = expand_query(query)
            if extra:
                cands, best = rank_pages(idx.norm, query + " " + extra, top_n=CAND_PAGES)
                toks = query_tokens(query + " " + extra)
        if not cands or best <= 0:
            if ref:
                return {"page_number": None, "pdf_page": None, "highlight": "", "page_image": None, "note": note,
                        "answer": ref + "\n\n(هذه الإجابة من بحث الإنترنت، ولم أجد هذا الموضوع في الكتاب.)",
                        "verified": True, "_cache": False}
            return {"page_number": None, "pdf_page": None, "answer": NO_MATCH, "highlight": "",
                    "page_image": None, "note": note, "_cache": False}
        nxt = cands[0] + 1                # الصفحة التالية للأقرب غالباً فيها التتمة
        order = cands[:3] + ([nxt] if nxt < n_pages else []) + cands[3:]
        cands = list(OrderedDict.fromkeys(order))[:CAND_PAGES]

    doc = fitz.open(path)
    try:
        pages, dims, used, imgs = [], {}, [], 0
        limit = 3500 if small else 2400
        for i in cands:
            raw_text = doc[i].get_text("text")
            as_text = (i not in idx.ocr_done) and len(raw_text.strip()) >= 120 and not text_is_broken(raw_text)
            if as_text:
                pages.append({"label": f"[صفحة رقم {i + 1}]", "text": llm_page_text(doc[i], toks, limit)})
                used.append(i)
            elif imgs < (SMALL_DOC if small else MAX_IMG_PAGES):
                jpg, pw, ph, z = render_jpeg(doc[i], ANALYSIS_DPI)
                dims[i] = (pw, ph, z)
                pages.append({"label": f"صفحة رقم {i + 1} (أبعاد الصورة: {pw}x{ph} بكسل):", "jpeg": jpg})
                used.append(i)
                imgs += 1
        make = (lambda p: image_instructions(query, p, ref)) if dims else (lambda p: text_instructions(query, ref))
        try:
            raw, provider = run_models(pages, make, max_tokens=650 if dims else 500)
        except AIUnavailable:
            pi = 0 if small else cands[0]
            if doc[pi].get_text("text").strip():
                return local_fallback(doc, pi, toks)
            raise

        answer, page_idx, printed, boxes, highlight = parse_reply(raw, used)
        rects = []
        if highlight and page_idx not in dims:
            rects = locate_rects(doc[page_idx], highlight)
            if not rects:
                try:
                    rects = list(doc[page_idx].search_for(highlight))
                except Exception:
                    rects = []
        if not rects and boxes and page_idx in dims:
            w, h, z = dims[page_idx]
            k = 1.0 / z
            for b in to_pixel_boxes(boxes, provider, w, h):
                rects.append(fitz.Rect(min(b[0], b[2]) * k, min(b[1], b[3]) * k, max(b[0], b[2]) * k, max(b[1], b[3]) * k))
        return {"page_number": printed if printed else page_idx + 1, "pdf_page": page_idx + 1,
                "answer": answer or "لم يتم العثور على إجابة واضحة.", "highlight": highlight,
                "page_image": render_page(doc, page_idx, rects), "note": note, "verified": bool(ref),
                "_cache": pct >= 100}
    finally:
        doc.close()


def cached_answer(path, key, query):
    ck = (key, " ".join(query_tokens(query)) or norm(query))
    with _ANS_LOCK:
        if ck in _ANS_CACHE:
            _ANS_CACHE.move_to_end(ck)
            return _ANS_CACHE[ck]
    result = answer_question(path, key, query)
    cacheable = result.pop("_cache", False)
    if cacheable:
        with _ANS_LOCK:
            _ANS_CACHE[ck] = result
            while len(_ANS_CACHE) > 60:
                _ANS_CACHE.popitem(last=False)
    return result


def respond(fn):
    try:
        return fn()
    except ApiError as e:
        return JSONResponse(status_code=e.status, content={"error": e.msg, "error_ar": e.msg, "retry": e.retry,
                                                           "detail": getattr(e, "detail", None)})
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
    """يبني الفهرس ويبدأ OCR في الخلفية للكتب الممسوحة، فيكون الكتاب جاهزاً قبل أول سؤال."""
    try:
        idx = get_index(path, key)
        if len(idx.norm) > SMALL_DOC:
            ensure_ocr(path, key, idx, wait=0)
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
    threading.Thread(target=run_ai_selftest, daemon=True).start()


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
def search_in_book(request: Request, book_code: str = Query(...), query: str = Query(..., min_length=2, max_length=300)):
    if not rate_ok(request, "ask", 20):
        return too_many()
    path = book_path(book_code)
    if not path:
        msg = "عذراً، الكتاب غير متوفر حالياً في المكتبة الرقمية."
        return JSONResponse(status_code=404, content={"error": msg, "error_ar": msg, "retry": False})
    return respond(lambda: cached_answer(path, book_key(path), query))


@app.post("/api/custom/upload")
def custom_upload(request: Request, file: UploadFile = File(...)):
    if not rate_ok(request, "upload", 6):
        return too_many()

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
def custom_ask(request: Request, file_id: str = Query(...), query: str = Query(..., min_length=2, max_length=300)):
    if not rate_ok(request, "ask", 20):
        return too_many()
    if not re.fullmatch(r"[0-9a-f]{40}", file_id):
        return JSONResponse(status_code=400, content={"error_ar": "معرّف الملف غير صحيح.", "retry": False})
    path = os.path.join(UPLOAD_DIR, file_id + ".pdf")
    if not os.path.exists(path):
        msg = "انتهت صلاحية الملف المرفوع. أعد رفعه من فضلك."
        return JSONResponse(status_code=410, content={"error": msg, "error_ar": msg, "retry": False})
    os.utime(path, None)
    return respond(lambda: cached_answer(path, file_id, query))


@app.get("/api/locate")
def locate_in_book(request: Request, book_code: str = Query(...), query: str = Query(..., min_length=2, max_length=300)):
    if not rate_ok(request, "loc", 60):
        return too_many()
    path = book_path(book_code)
    if not path:
        return JSONResponse(status_code=404, content={"found": False})
    return respond(lambda: locate_local(path, book_key(path), query))


@app.get("/api/custom/locate")
def custom_locate(request: Request, file_id: str = Query(...), query: str = Query(..., min_length=2, max_length=300)):
    if not rate_ok(request, "loc", 60):
        return too_many()
    if not re.fullmatch(r"[0-9a-f]{40}", file_id):
        return JSONResponse(status_code=400, content={"found": False})
    path = os.path.join(UPLOAD_DIR, file_id + ".pdf")
    if not os.path.exists(path):
        return JSONResponse(status_code=410, content={"found": False})
    return respond(lambda: locate_local(path, file_id, query))


# ───────────── لوحة الإدارة ─────────────
_ADMIN_FAILS = {}


def _admin(key, request: Request = None):
    if not ADMIN_KEY:
        raise ApiError("اضبط المتغير ADMIN_KEY في إعدادات Render أولاً.", 403)
    ip = client_ip(request) if request else "?"
    now = time.time()
    fails = [t for t in _ADMIN_FAILS.get(ip, []) if now - t < 600]
    if len(fails) >= 8:
        raise ApiError("محاولات خاطئة كثيرة. انتظر 10 دقائق.", 429)
    if not hmac.compare_digest((key or "").encode(), ADMIN_KEY.encode()):
        fails.append(now)
        _ADMIN_FAILS[ip] = fails
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
def admin_upload(request: Request, key: str = Form(...), code: str = Form(...), kind: str = Form("pdf"), file: UploadFile = File(...)):
    def go():
        _admin(key, request)
        _save_book(file, code, "solved" if kind == "solved" else "pdf")
        return {"ok": True, "code": code, "kind": kind}
    return respond(go)


@app.post("/api/admin/bulk")
def admin_bulk(request: Request, key: str = Form(...), files: List[UploadFile] = File(...)):
    def go():
        _admin(key, request)
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
def admin_link(request: Request, key: str = Form(...), code: str = Form(...), kind: str = Form(...), url: str = Form("")):
    def go():
        _admin(key, request)
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


@app.post("/api/admin/catalog")
def admin_catalog(request: Request, key: str = Form(...), group: str = Form(...), text: str = Form("")):
    """تعديل مواد فصل دراسي: كل سطر  slug|الاسم العربي|English name   (فارغ = رجوع للقائمة الافتراضية)."""
    def go():
        _admin(key, request)
        if not _GROUP_RE.match(group):
            raise ApiError("مجموعة غير صحيحة.", 400)
        ov = _catalog_override()
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2 or not _SLUG_RE.match(parts[0]) or not parts[1]:
                raise ApiError(f"سطر غير صحيح: {line}", 400)
            rows.append([parts[0], parts[1], parts[2] if len(parts) > 2 and parts[2] else parts[1]])
        if rows:
            ov[group] = rows
        else:
            ov.pop(group, None)
        _write_json("catalog.json", ov)
        return {"ok": True, "count": len(rows)}
    return respond(go)


@app.get("/api/ai-status")
def ai_status(request: Request, run: int = 0):
    """افتحها في المتصفح: تعرض هل المفاتيح مضبوطة ونتيجة تجربة كل موديل وسبب الفشل. أضف ?run=1 لإعادة التجربة."""
    if run:
        if not rate_ok(request, "aitest", 3):
            return too_many()
        run_ai_selftest()
    return {"anthropic_key": bool(ANTHROPIC_API_KEY), "gemini_key": bool(GEMINI_API_KEY),
            "claude_model": CLAUDE_MODEL, "claude_fallbacks": CLAUDE_FALLBACKS, "gemini_models": GEMINI_MODELS,
            "web_search": WEB_SEARCH, "tested_at": AI_STATUS["at"], "selftest": AI_STATUS["selftest"],
            "recent_errors": LAST_AI_ERRORS[-6:]}


@app.post("/api/admin/ai-test")
def admin_ai_test(request: Request, key: str = Form(...)):
    """يجرّب كل موديل بطلب صغير ويعرض سبب الفشل بالضبط (مفتاح خاطئ، اسم موديل غير صحيح، رصيد...)."""
    def go():
        _admin(key, request)
        pages = [{"label": "[اختبار]", "text": "المطلوب: اكتب كلمة واحدة فقط وهي: تم"}]
        jobs = []
        if client:
            jobs.append(("claude", CLAUDE_MODEL))
            jobs += [("claude", m) for m in CLAUDE_FALLBACKS]
        if gemini_client:
            jobs += [("gemini", m) for m in GEMINI_MODELS]
        results = []
        for provider, model in jobs:
            t0 = time.time()
            try:
                out = (call_claude(pages, "أجب بكلمة واحدة.", 30, model) if provider == "claude"
                       else call_gemini(model, pages, "أجب بكلمة واحدة.", 30))
                results.append({"provider": provider, "model": model, "ok": bool(out),
                                "ms": int((time.time() - t0) * 1000), "error": "" if out else "رد فارغ"})
            except Exception as e:
                results.append({"provider": provider, "model": model, "ok": False,
                                "ms": int((time.time() - t0) * 1000), "error": f"{type(e).__name__}: {str(e)[:220]}"})
        return {"results": results, "anthropic_key": bool(ANTHROPIC_API_KEY), "gemini_key": bool(GEMINI_API_KEY),
                "recent_errors": LAST_AI_ERRORS[-5:]}
    return respond(go)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=HTML_PAGE)


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(content=ADMIN_PAGE)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0e5a44">
<title>المنصة التعليمية الذكية للمناهج</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📜</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&family=Reem+Kufi:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{--green:#0e5a44;--green-d:#06291f;--emerald:#14946d;--gold:#d4af5a;--gold-d:#9a7a2c;--gold-l:#f6e7b8;
--paper:#f5f2e9;--card:#fff;--card2:#faf6e8;--ink:#10201a;--muted:#5d6b64;--line:#e3dcc6;--sh:rgba(6,41,31,.38);
--frieze:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='28' height='14' viewBox='0 0 28 14'%3E%3Cpath d='M0 14L7 0l7 14zM14 14l7-14 7 14z' fill='%23d4af5a'/%3E%3C/svg%3E");
--lattice:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='36' height='36'%3E%3Cpath d='M18 2L34 18 18 34 2 18z' fill='none' stroke='%23fff' stroke-opacity='.13' stroke-width='1.4'/%3E%3Cpath d='M18 11L25 18 18 25 11 18z' fill='%23fff' fill-opacity='.06'/%3E%3C/svg%3E");
--star:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120' viewBox='0 0 60 60'%3E%3Cg fill='none' stroke='%23d4af5a' stroke-opacity='.42' stroke-width='.8'%3E%3Cpath d='M30 4l6.5 15.5L52 13l-6.5 15.5L60 30l-14.5 1.5L52 47l-15.5-6.5L30 56l-6.5-15.5L8 47l6.5-15.5L0 30l14.5-1.5L8 13l15.5 6.5z'/%3E%3Ccircle cx='30' cy='30' r='9'/%3E%3C/g%3E%3C/svg%3E")}
@media (prefers-color-scheme:dark){:root{--paper:#06110c;--card:#0e1f17;--card2:#13291f;--ink:#eef3ef;--muted:#9db3a8;--line:#1e3a2d;--sh:rgba(0,0,0,.7)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;min-height:100vh;color:var(--ink);font-family:'IBM Plex Sans Arabic','Tajawal',system-ui,sans-serif;line-height:1.7;
background:radial-gradient(900px 420px at 100% -8%,rgba(20,148,109,.22),transparent 60%),radial-gradient(700px 380px at 0 18%,rgba(212,175,90,.16),transparent 60%),var(--paper);background-attachment:fixed}
button,input{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none;padding:0;text-align:start}
a{color:inherit}
:focus-visible{outline:3px solid var(--gold);outline-offset:3px}
.k{font-family:'Reem Kufi','IBM Plex Sans Arabic',sans-serif}
.top{position:relative;overflow:hidden;background:linear-gradient(135deg,#06291f 0%,#0e5a44 58%,#14946d 100%);color:#fff;padding:calc(18px + env(safe-area-inset-top,0px)) 18px 28px;margin-bottom:18px}
.top::before{content:"";position:absolute;inset:0;background:var(--lattice);opacity:.9}
.top::after{content:"";position:absolute;inset-inline:0;bottom:0;height:12px;background:var(--frieze) repeat-x;background-size:24px 12px}
.top-in{position:relative;z-index:1;max-width:640px;margin:0 auto;display:flex;align-items:center;gap:12px;justify-content:space-between}
.top h1{margin:0;font-size:1.4rem;line-height:1.35;font-weight:700;background:linear-gradient(90deg,#fff3c9,#d4af5a);-webkit-background-clip:text;background-clip:text;color:transparent;display:flex;align-items:center;gap:10px}
.top h1::before{content:"📜";font-size:1.15rem;width:40px;height:40px;flex:none;display:grid;place-items:center;border-radius:50% 50% 12px 12px;background:rgba(255,255,255,.12);border:1.5px solid var(--gold);-webkit-text-fill-color:initial}
.top p{margin:2px 0 0;font-size:.8rem;color:#c3dacf}
.lang{border:1.5px solid var(--gold);color:var(--gold-l);border-radius:999px;padding:5px 16px;font-weight:600;white-space:nowrap;background:rgba(255,255,255,.08);backdrop-filter:blur(6px)}
.wrap{max-width:640px;margin:0 auto;padding:12px 16px calc(120px + env(safe-area-inset-bottom,0px))}
.wrap>*{animation:rise .45s cubic-bezier(.2,.8,.2,1) both}
.wrap>*:nth-child(2){animation-delay:.06s}.wrap>*:nth-child(3){animation-delay:.12s}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.pathbar{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.back{flex:none;width:44px;height:44px;display:grid;place-items:center;border-radius:50%;background:linear-gradient(145deg,var(--emerald),var(--green-d));border:2px solid var(--gold);box-shadow:0 6px 14px -6px var(--sh)}
.back svg{width:20px;height:20px;stroke:var(--gold-l);fill:none;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}
body[dir=rtl] .back svg{transform:scaleX(-1)}
.crumbs{display:flex;gap:6px;overflow-x:auto;white-space:nowrap;font-size:.78rem;scrollbar-width:none;flex:1}
.crumbs::-webkit-scrollbar{display:none}
.crumbs span{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:2px 13px}
.crumbs span:last-child{background:linear-gradient(90deg,var(--gold-l),var(--gold));color:#3a2b08;border-color:var(--gold);font-weight:700}
h2.h{font-size:1.55rem;margin:6px 0 16px;line-height:1.4}
.hero{position:relative;overflow:hidden;color:#fff;border-radius:30px;padding:28px 22px 24px;margin-bottom:26px;
background:radial-gradient(500px 260px at 0 0,rgba(20,148,109,.7),transparent 60%),linear-gradient(145deg,#06291f,#0b4634 55%,#0e5a44);
border:1px solid rgba(212,175,90,.55);box-shadow:0 22px 40px -22px var(--sh),inset 0 0 0 5px rgba(6,41,31,.5),inset 0 0 0 6px rgba(212,175,90,.35)}
.hero::before{content:"";position:absolute;inset:0;background:var(--lattice);opacity:.8}
.hero>*{position:relative}
.hero h2{margin:0 0 8px;font-size:2.25rem;line-height:1.25;max-width:72%;background:linear-gradient(90deg,#fff3c9,#d4af5a 70%);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{margin:0 0 18px;color:#d3e4db;font-size:.93rem;max-width:74%}
.arch{position:absolute!important;inset-inline-end:-46px;top:-40px;width:210px;height:210px;background:var(--star) center/contain no-repeat;animation:spin 60s linear infinite;pointer-events:none}
.arch svg{display:none}
@keyframes spin{to{transform:rotate(360deg)}}
.cta{display:inline-flex;align-items:center;gap:8px;background:linear-gradient(180deg,#f0cf7a,#d4af5a);color:#2b2108;font-weight:700;border-radius:16px;padding:12px 20px;box-shadow:0 5px 0 #8c6d27,0 14px 22px -10px rgba(0,0,0,.6)}
.cta:active{transform:translateY(4px);box-shadow:0 1px 0 #8c6d27}
.cta svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
.stats{display:flex;gap:10px;margin-top:20px}
.stat{flex:1;text-align:center;padding:9px 4px;border-radius:16px;background:rgba(255,255,255,.08);border:1px solid rgba(212,175,90,.35);backdrop-filter:blur(6px)}
.stat b{display:block;font-family:'Reem Kufi',sans-serif;font-size:1.5rem;line-height:1.2;color:var(--gold-l)}
.stat span{font-size:.72rem;color:#c3dacf}
.sec{display:flex;align-items:center;gap:10px;font-size:1.25rem;margin:0 0 14px}
.sec::after{content:"";flex:1;height:12px;background:var(--frieze) repeat-x;background-size:24px 12px;opacity:.65}
.stack{display:grid;gap:16px}
.stage{position:relative;overflow:hidden;display:flex;align-items:center;gap:16px;width:100%;padding:22px 18px 30px;border-radius:26px;color:#fff;
background:var(--lattice),linear-gradient(135deg,var(--c),color-mix(in srgb,var(--c) 45%,#000));box-shadow:0 16px 28px -16px var(--sh);border:1px solid rgba(255,255,255,.14)}
.stage::before{content:"";position:absolute;inset-inline-end:-30px;top:-30px;width:150px;height:150px;background:var(--star) center/contain no-repeat;opacity:.55}
.stage::after{content:"";position:absolute;inset-inline:0;bottom:0;height:10px;background:var(--frieze) repeat-x;background-size:20px 10px}
.stage>*{position:relative}
.stage .n{font-size:3.4rem;line-height:1;min-width:58px;text-align:center;color:transparent;-webkit-text-stroke:1.5px var(--gold);text-shadow:0 0 22px rgba(212,175,90,.5)}
.stage b{display:block;font-size:1.2rem;line-height:1.35}
.stage small{color:#d6e4dd}
.stage .go{margin-inline-start:auto;font-size:1.7rem;color:var(--gold-l)}
body[dir=rtl] .stage .go{transform:scaleX(-1)}
.stage:active,.cover:active,.archc:active,.row:active{transform:scale(.982)}
@media (hover:hover){.stage:hover,.cover:hover,.archc:hover,.row:hover{transform:translateY(-3px)}}
.stage,.cover,.archc,.row{transition:transform .2s,box-shadow .2s}
.duo{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.archc{display:flex;flex-direction:column;align-items:center;justify-content:space-between;text-align:center;min-height:160px;padding:24px 10px 14px;
background:linear-gradient(180deg,var(--card),var(--card2));border:2px solid var(--c,var(--green));border-radius:64px 64px 18px 18px;box-shadow:inset 0 0 0 4px var(--card),inset 0 0 0 5px var(--gold),0 12px 22px -16px var(--sh)}
.archc i{font-style:normal;font-size:3rem;line-height:1;color:var(--c,var(--green))}
.archc b{font-weight:600;font-size:.95rem;line-height:1.4}
.archc small{display:block;color:var(--muted);font-size:.78rem;font-weight:400}
.covers{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.cover{position:relative;overflow:hidden;display:flex;flex-direction:column;justify-content:space-between;gap:10px;min-height:184px;padding:24px 14px 12px;color:#fff;
background:var(--lattice),linear-gradient(150deg,var(--c),color-mix(in srgb,var(--c) 40%,#000));border-radius:16px 24px 24px 16px;border-inline-start:10px solid rgba(0,0,0,.3);box-shadow:0 14px 22px -14px var(--sh)}
body[dir=rtl] .cover{border-radius:24px 16px 16px 24px}
.cover::before{content:"";position:absolute;inset-inline:0;top:0;height:8px;background:var(--frieze) repeat-x;background-size:16px 8px;transform:scaleY(-1)}
.cover::after{content:"";position:absolute;inset-inline-end:-26px;bottom:-26px;width:110px;height:110px;background:var(--star) center/contain no-repeat;opacity:.5}
.cover>*{position:relative;z-index:1}
.cover .em{width:50px;height:50px;display:grid;place-items:center;font-size:1.65rem;background:rgba(255,255,255,.16);border:1.5px solid rgba(246,231,184,.75);border-radius:50% 50% 14px 14px;box-shadow:0 0 18px rgba(212,175,90,.35)}
.cover b{font-family:'Reem Kufi',sans-serif;font-size:1.08rem;line-height:1.45;font-weight:700}
.chips{display:flex;gap:6px}
.ch{width:28px;height:28px;display:grid;place-items:center;border-radius:9px;background:rgba(255,255,255,.13)}
.ch svg{width:16px;height:16px;stroke:rgba(255,255,255,.5);fill:none;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.ch.on{background:linear-gradient(180deg,#f0cf7a,var(--gold))}
.ch.on svg{stroke:#2b2108}
.cover.off{filter:saturate(.4) brightness(.92)}
.shero{position:relative;overflow:hidden;display:flex;align-items:center;gap:16px;padding:24px 18px 28px;margin-bottom:18px;color:#fff;
background:var(--lattice),linear-gradient(145deg,var(--c),color-mix(in srgb,var(--c) 40%,#000));border-radius:28px;box-shadow:0 18px 30px -18px var(--sh)}
.shero::before{content:"";position:absolute;inset-inline-end:-40px;top:-40px;width:180px;height:180px;background:var(--star) center/contain no-repeat;animation:spin 70s linear infinite}
.shero::after{content:"";position:absolute;inset-inline:0;bottom:0;height:10px;background:var(--frieze) repeat-x;background-size:20px 10px}
.shero>*{position:relative}
.shero .em{width:72px;height:72px;flex:none;display:grid;place-items:center;font-size:2.3rem;background:rgba(255,255,255,.15);border:2px solid var(--gold);border-radius:50% 50% 16px 16px;box-shadow:0 0 26px rgba(212,175,90,.4)}
.shero h2{margin:0;font-size:1.5rem;line-height:1.35;color:var(--gold-l)}
.shero p{margin:2px 0 0;font-size:.85rem;color:#e0ece6}
.row{position:relative;overflow:hidden;display:flex;align-items:center;gap:14px;width:100%;padding:15px 16px;margin-bottom:12px;text-decoration:none;background:var(--card);border:1px solid var(--line);border-radius:20px;box-shadow:0 10px 18px -14px var(--sh)}
.row .ic{flex:none;width:48px;height:48px;display:grid;place-items:center;border-radius:15px;background:linear-gradient(145deg,var(--gold-l),var(--gold))}
.row .ic svg{width:25px;height:25px;stroke:#1d2a14;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.row b{display:block;font-weight:700;line-height:1.4}
.row small{display:block;color:var(--muted);font-size:.8rem;line-height:1.4}
.row .ar{margin-inline-start:auto;color:var(--gold);font-size:1.4rem;font-weight:700}
body[dir=rtl] .row .ar{transform:scaleX(-1)}
.row.ai{color:#2b2108;background:linear-gradient(135deg,#f6dc8c,#d4af5a 60%,#b98f35);border-color:#8c6d27;box-shadow:0 6px 0 #8c6d27,0 18px 26px -14px rgba(0,0,0,.6)}
.row.ai::after{content:"";position:absolute;top:0;bottom:0;width:60px;inset-inline-start:-80px;background:linear-gradient(100deg,transparent,rgba(255,255,255,.6),transparent);animation:shine 3.2s ease-in-out infinite}
@keyframes shine{to{inset-inline-start:120%}}
.row.ai .ic{background:var(--green-d)}
.row.ai .ic svg{stroke:var(--gold-l)}
.row.ai small{color:#4a3a10}
.row.ai .ar{color:var(--green-d)}
.row.off{background:var(--card2);box-shadow:none;color:var(--muted);pointer-events:none;opacity:.7}
.row.off .ic{background:var(--line)}.row.off .ic svg{stroke:var(--muted)}
.hint{color:var(--muted);font-size:.82rem;margin:8px 2px 14px}
.field{margin-bottom:16px}
.field>label{display:block;font-weight:600;margin-bottom:8px}
.inp{width:100%;padding:16px 18px;font-size:1.02rem;background:var(--card);color:var(--ink);border:1.5px solid var(--line);border-radius:18px;box-shadow:0 8px 16px -14px var(--sh)}
.inp:focus{outline:none;border-color:var(--emerald);box-shadow:0 0 0 4px rgba(20,148,109,.2)}
.drop{display:flex;flex-direction:column;align-items:center;gap:4px;text-align:center;padding:26px 14px;cursor:pointer;background:var(--card);border:2px dashed var(--gold);border-radius:24px;transition:background .2s}
.drop:hover{background:var(--card2)}
.drop input{position:absolute;opacity:0;width:1px;height:1px}
.drop svg{width:38px;height:38px;stroke:var(--emerald);fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.drop b{font-size:1.05rem}.drop small{color:var(--muted)}
.upst{margin:12px 0 16px}
.prog{height:12px;border-radius:99px;background:var(--line);overflow:hidden}
.prog i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--emerald),var(--gold));transition:width .2s}
.okchip{display:flex;align-items:center;gap:8px;background:rgba(20,148,109,.14);border:1px solid var(--emerald);color:var(--emerald);border-radius:16px;padding:10px 14px;font-weight:600}
.errchip{background:rgba(200,60,40,.12);border:1px solid #d98a7e;color:#b23a2a;border-radius:16px;padding:10px 14px}
.btn{width:100%;display:block;text-align:center;background:linear-gradient(180deg,var(--emerald),var(--green));color:var(--gold-l);font-weight:700;font-size:1.06rem;padding:16px;border-radius:18px;box-shadow:0 6px 0 var(--green-d),0 16px 24px -12px var(--sh)}
.btn:active{transform:translateY(5px);box-shadow:0 1px 0 var(--green-d)}
.btn:disabled{opacity:.55;cursor:not-allowed}
.btn.alt{background:var(--card);color:var(--emerald);border:2px solid var(--emerald);box-shadow:none;margin-top:12px}
#out{margin-top:22px}
.loading{padding:18px;background:var(--card);border:1px solid var(--line);border-radius:24px}
.loading p{margin:0 0 14px;font-weight:600;text-align:center;color:var(--emerald)}
.sk{height:14px;border-radius:8px;margin-bottom:10px;background:linear-gradient(90deg,var(--line) 25%,var(--card2) 50%,var(--line) 75%);background-size:200% 100%;animation:sh 1.1s linear infinite}
.sk:nth-of-type(2){width:82%}.sk:nth-of-type(3){width:55%}
.sk.img{height:190px;border-radius:14px;margin-top:16px}
@keyframes sh{to{background-position:-200% 0}}
@media (prefers-reduced-motion:reduce){.sk,.arch,.shero::before,.row.ai::after{animation:none}.prog i{transition:none}.wrap>*{animation:none}}
.res{position:relative;background:var(--card);border:1px solid var(--line);border-radius:28px;padding:20px 18px 18px;box-shadow:0 20px 34px -22px var(--sh);overflow:hidden}
.res::before{content:"";position:absolute;inset-inline:0;top:0;height:5px;background:linear-gradient(90deg,var(--emerald),var(--gold),var(--emerald))}
.rtop{display:flex;align-items:center;gap:16px;margin:6px 0 14px}
.med{flex:none;width:88px;height:88px;display:grid;place-items:center;align-content:center;border-radius:50%;background:radial-gradient(circle at 30% 25%,var(--emerald),var(--green-d));color:var(--gold-l);outline:3px solid var(--gold);outline-offset:3px;line-height:1.1;animation:pulse 2.4s ease-in-out infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(212,175,90,.45)}50%{box-shadow:0 0 0 12px rgba(212,175,90,0)}}
.med small{font-size:.7rem}.med b{font-size:2rem;font-family:'Reem Kufi',sans-serif}
.rtop .fp{font-size:.82rem;color:var(--muted)}
.atext{margin:0 0 14px;font-size:1.1rem;line-height:2;white-space:pre-wrap;animation:rise .5s both}
blockquote{margin:0 0 16px;padding:12px 14px;background:rgba(212,175,90,.14);border-inline-start:5px solid var(--gold);border-radius:12px;font-size:.95rem}
blockquote small{display:block;color:var(--muted);font-weight:600;margin-bottom:2px}
.note{background:rgba(212,175,90,.16);border:1px solid var(--gold);border-radius:14px;padding:8px 12px;font-size:.85rem;margin:0 0 14px}
figure{margin:0}
figure img{display:block;width:100%;border:3px double var(--gold);border-radius:10px;cursor:zoom-in;background:#fff}
figcaption{text-align:center;color:var(--muted);font-size:.78rem;margin-top:8px}
.err{background:rgba(200,60,40,.1);border:1px solid #d98a7e;color:#b23a2a;border-radius:20px;padding:16px}
.err p{margin:0}
.nav{position:fixed;z-index:30;bottom:calc(14px + env(safe-area-inset-bottom,0px));left:50%;transform:translateX(-50%);width:min(92%,400px);
display:flex;padding:6px;gap:4px;background:rgba(6,41,31,.82);backdrop-filter:blur(14px) saturate(1.4);border:1.5px solid var(--gold);border-radius:28px;box-shadow:0 18px 30px -10px rgba(0,0,0,.6)}
.nav button{flex:1;display:flex;flex-direction:column;align-items:center;padding:7px 4px;border-radius:22px;font-size:.72rem;font-weight:600;text-align:center;color:#b8d0c5;transition:background .2s}
.nav button svg{width:24px;height:24px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.nav button.on{background:linear-gradient(180deg,#f0cf7a,var(--gold));color:#2b2108}
#zoom{position:fixed;inset:0;background:rgba(3,12,8,.96);overflow:auto;display:none;z-index:50;padding:12px}
#zoom.on{display:block}
#zoom img{display:block;width:max(100%,900px);margin:46px auto 0;border-radius:6px;touch-action:pan-x pan-y pinch-zoom}
#zoom button{position:fixed;top:calc(10px + env(safe-area-inset-top,0px));inset-inline-start:12px;background:var(--gold);color:#2b2108;border-radius:999px;padding:6px 18px;font-weight:700;z-index:51}
.aiwait{display:flex;align-items:center;gap:10px;margin:0 0 12px;padding:10px 14px;border-radius:14px;background:rgba(20,148,109,.12);border:1px solid var(--emerald);color:var(--emerald);font-weight:600;font-size:.92rem}
.aiwait .dot{width:10px;height:10px;border-radius:50%;background:var(--gold);animation:pulse 1.2s ease-in-out infinite;flex:none}
.okchip{flex-wrap:wrap;justify-content:space-between}
.okchip bdi{flex-basis:100%;font-size:.78rem;font-weight:400;opacity:.8;overflow-wrap:anywhere;direction:ltr;text-align:start}
.crumbs span{max-width:56vw;overflow:hidden;text-overflow:ellipsis}
.shero::before{opacity:.45}
.top h1{font-size:1.3rem}
.res blockquote{white-space:pre-line}
/* تخفيف الزخارف والخطوط */
.k,.cover b,.stat b,.med b,.stage .n,.top h1,.hero h2,.shero h2,h2.h,.crumbs span{font-family:'IBM Plex Sans Arabic',system-ui,sans-serif!important;letter-spacing:0}
.k,h2.h,.hero h2,.shero h2,.stage b,.cover b,.top h1{font-weight:700}
.top::before,.hero::before,.arch,.stage::before,.cover::after,.shero::before,.row.ai::after,.top h1::before{display:none!important}
.top h1{background:none;-webkit-text-fill-color:var(--gold-l);color:var(--gold-l);font-size:1.2rem}
.hero h2{background:none;-webkit-text-fill-color:var(--gold-l);color:var(--gold-l);font-size:2rem}
.hero{box-shadow:0 18px 32px -22px var(--sh);border:1px solid rgba(212,175,90,.35);background:linear-gradient(145deg,#06291f,#0e5a44)}
.stage{background:linear-gradient(135deg,var(--c),color-mix(in srgb,var(--c) 55%,#000))}
.cover{background:linear-gradient(150deg,var(--c),color-mix(in srgb,var(--c) 50%,#000))}
.shero{background:linear-gradient(145deg,var(--c),color-mix(in srgb,var(--c) 50%,#000))}
.top::after,.stage::after,.shero::after,.cover::before{opacity:.3;height:5px;background-size:12px 6px}
.sec::after{background:linear-gradient(90deg,var(--gold),transparent);height:2px;opacity:.5}
.archc{box-shadow:0 10px 20px -16px var(--sh);border-radius:52px 52px 18px 18px}
.stage .n{-webkit-text-stroke:0;color:var(--gold-l);text-shadow:none;font-size:2.8rem}
.cover .em,.shero .em{box-shadow:none;border-radius:50%}
.vchip{margin:0 0 10px;font-size:.82rem;font-weight:600;color:var(--emerald)}
.res .hint[dir=ltr]{font-size:.72rem;opacity:.8;overflow-wrap:anywhere}
</style>
</head>
<body>
<header class="top"><div class="top-in">
  <div><h1 class="k" id="ttl"></h1><p id="sub"></p></div>
  <button class="lang" id="lang" onclick="toggleLang()"></button>
</div></header>
<main class="wrap" id="app"></main>
<nav class="nav" id="nav" aria-label="main"></nav>
<div id="zoom" onclick="this.classList.remove('on')"><button type="button" id="zclose"></button><img id="zimg" alt=""></div>

<script>
const T={
ar:{title:'المنصة التعليمية الذكية للمناهج',sub:'اسأل كتابك المدرسي واحصل على الإجابة مع رقم الصفحة',
heroT:'اسأل كتابك<br>يجيبك بالصفحة',heroBtn:'ارفع كتابك واسأل',home:'الرئيسية',upload:'ارفع كتابك',lang:'English',
pickStage:'اختر مرحلتك',pickYear:'اختر الصف',pickTerm:'اختر الفصل الدراسي',pickSubject:'اختر المادة',yearsN:'سنوات دراسية',subjectsN:'مواد',
customTitle:'ارفع كتابك الخاص',customSub:'أي ملف PDF حتى 500 ميجابايت. ارفعه مرة واحدة واسأل عنه بقدر ما تريد.',
dropT:'اختر ملف PDF',dropS:'الحد الأعلى 500 ميجابايت',uploading:'جاري الرفع',ready:'الكتاب جاهز',pagesN:'صفحة',tooBig:'حجم الملف أكبر من 500 ميجابايت.',
ask:'اسأل الكتاب بالذكاء الاصطناعي',askHint:'إجابة سريعة مع رقم الصفحة وتظليل الجواب',
book:'الكتاب (بدون حل)',bookHint:'افتح الكتاب PDF كاملاً',bookIen:'الكتاب من منصة عين',bookIenHint:'الملف لم يُضف بعد، افتح المصدر الرسمي',
solved:'الكتاب محلول',solvedHint:'الكتاب مع الحلول',videos:'شرح الدروس',videosHint:'من قناة عين التعليمية على يوتيوب',
soon:'قريباً',notAvail:'ملف هذا الكتاب لم يُضف بعد',qLabel:'اكتب سؤالك',qPh:'مثال: ما هو قانون كبلر الأول؟',
search:'ابحث في الكتاب',page:'الصفحة',pdfPage:'ترتيبها في الملف',fromBook:'من نص الكتاب',pageCap:'اضغط على الصفحة لتكبيرها',retry:'أعد المحاولة',close:'إغلاق',
l1:'أبحث في صفحات الكتاب…',l2:'أقرأ الصفحات الأقرب لسؤالك…',l3:'أجهّز الإجابة…',l4:'الكتاب كبير، أجهّز صفحاته لأول مرة…',
errServer:'تعذّر الاتصال بالخادم. أعد المحاولة بعد لحظات.',errSlow:'استغرق الرد وقتاً طويلاً. أعد المحاولة.',errNet:'تحقق من اتصالك بالإنترنت ثم أعد المحاولة.',aiThinking:'الذكاء الاصطناعي يصيغ لك الإجابة…',aiFail:'تعذّر توليد الإجابة الآن، وهذه أقرب فقرة في الكتاب لسؤالك.',verified:'تم التحقق من الإجابة بالبحث في الإنترنت'},
en:{title:'Smart Curriculum Platform',sub:'Ask your textbook and get the answer with the page number',
heroT:'Ask your book<br>get the page',heroBtn:'Upload a book and ask',home:'Home',upload:'Upload',lang:'عربي',
pickStage:'Choose your stage',pickYear:'Choose a grade',pickTerm:'Choose a semester',pickSubject:'Choose a subject',yearsN:'academic years',subjectsN:'subjects',
customTitle:'Upload your own book',customSub:'Any PDF up to 500 MB. Upload once, ask as much as you like.',
dropT:'Choose a PDF',dropS:'Up to 500 MB',uploading:'Uploading',ready:'Book ready',pagesN:'pages',tooBig:'File is larger than 500 MB.',
ask:'Ask the book with AI',askHint:'Quick answer with page number and highlight',
book:'Textbook (without solutions)',bookHint:'Open the full PDF',bookIen:'Book on Ein platform',bookIenHint:'File not added yet, open the official source',
solved:'Solved book',solvedHint:'Textbook with solutions',videos:'Lesson videos',videosHint:'From the Ein channel on YouTube',
soon:'Soon',notAvail:'This book file has not been added yet',qLabel:'Type your question',qPh:'e.g. What is Kepler’s first law?',
search:'Search the book',page:'Page',pdfPage:'File page',fromBook:'From the book',pageCap:'Tap the page to zoom',retry:'Try again',close:'Close',
l1:'Searching the book pages…',l2:'Reading the closest pages…',l3:'Preparing the answer…',l4:'Big book, preparing its pages for the first time…',
errServer:'Could not reach the server. Try again in a moment.',errSlow:'The reply took too long. Try again.',errNet:'Check your connection and try again.',aiThinking:'AI is writing your answer…',aiFail:'Could not generate the answer now; this is the closest passage in the book.',verified:'Verified with an internet search'}};

const state={lang:'ar',cat:null,view:'home',stage:null,year:null,term:null,subject:null,custom:null,up:{mode:'idle'}};
const ADMIN=new URLSearchParams(location.search).has('admin');
const $=id=>document.getElementById(id);
const t=k=>T[state.lang][k]||k;
const nm=o=>o[state.lang]||o.ar;
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const STAGE={primary:'#0e5a44',intermediate:'#27396b',secondary:'#8f3a2a'};
const PAL=['#0e5a44','#27396b','#8f3a2a','#6b4a1f','#1f6f78','#5b2f6e','#3d5a1e','#7a2e4a'];
const EMO={quran:'📗',islamic:'🕋',lugati:'✍️',arabic:'✍️',kafaayat:'🗣️',math:'➗',science:'🔬',english:'🔤',social:'🌍',digital:'💻',tech:'💻',
art:'🎨',pep:'⚽',pe:'⚽',health:'💪',life:'🏡',physics:'⚛️',chem:'🧪',bio:'🧬',critical:'🧠',env:'🌿',history:'🏛️',earth:'🪐',research:'🔎',elective:'⭐'};
const slugOf=c=>c.replace(/^[a-z]\d_t\d_/,'');
const emo=c=>EMO[slugOf(c)]||'📘';
function colorOf(code){let h=0;for(const ch of slugOf(code))h=(h*31+ch.charCodeAt(0))%PAL.length;return PAL[h]}
const S=()=>state.cat.stages.find(s=>s.id===state.stage);
const Y=()=>S().years[state.year];
const Tm=()=>Y().terms[state.term];
const Sub=()=>Tm().subjects[state.subject];
const ICON={
 book:'<path d="M4 19.5V5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2zm0 0A2 2 0 0 0 6 21h14"/>',
 check:'<circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/>',
 spark:'<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 16v4M17 18h4"/>',
 play:'<circle cx="12" cy="12" r="9"/><path d="M10 8.5v7l6-3.5z"/>',
 home:'<path d="M4 11l8-7 8 7v9a1 1 0 0 1-1 1h-4v-6H9v6H5a1 1 0 0 1-1-1z"/>',
 up:'<path d="M12 16V4M7 9l5-5 5 5M5 20h14"/>',
 globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
 back:'<path d="M9 5l7 7-7 7"/>',
 ext:'<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'};
const ico=n=>`<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[n]}</svg>`;

const PARENT={years:'home',terms:'years',subjects:'terms',subject:'subjects',ask:'subject',custom:'home'};
function go(view,patch){
  Object.assign(state,patch||{});state.view=view;
  history.pushState({view,stage:state.stage,year:state.year,term:state.term,subject:state.subject},'');
  render();window.scrollTo(0,0);
}
function back(){go(PARENT[state.view]||'home')}
window.addEventListener('popstate',e=>{if(e.state){Object.assign(state,e.state)}else{state.view='home'}render()});
function toggleLang(){state.lang=state.lang==='ar'?'en':'ar';render()}

const ORDER=['home','years','terms','subjects','subject','ask'];
function crumbs(){
  const items=[];
  if(state.view==='custom'){items.push(t('customTitle'))}
  else{
    const i=ORDER.indexOf(state.view);
    if(i>=1)items.push(nm(S()));
    if(i>=2)items.push(nm(Y()));
    if(i>=3)items.push(nm(Tm()));
    if(i>=4)items.push(nm(Sub()));
    if(i>=5)items.push(t('ask'));
  }
  return `<div class="pathbar"><button class="back" onclick="back()" aria-label="back">${ico('back')}</button><div class="crumbs">${items.map(x=>`<span>${esc(x)}</span>`).join('')}</div></div>`;
}
const chip=(on,ic)=>`<span class="ch ${on?'on':''}">${ico(ic)}</span>`;
const rowA=(href,ic,title,hint,cls)=>`<a class="row ${cls||''}" href="${esc(href)}" target="_blank" rel="noopener"><span class="ic">${ico(ic)}</span><span><b>${title}</b><small>${hint}</small></span><span class="ar">‹</span></a>`;
const rowOff=(ic,title,hint)=>`<div class="row off"><span class="ic">${ico(ic)}</span><span><b>${title}</b><small>${hint}</small></span></div>`;

function heroStats(){
  const n=state.cat.stages.reduce((a,s)=>a+s.years.reduce((b,y)=>b+y.terms.reduce((c,tm)=>c+tm.subjects.length,0),0),0);
  const L=state.lang==='ar'?['مراحل','مادة','ذكاء اصطناعي']:['stages','subjects','AI'];
  return `<div class="stats"><div class="stat"><b>3</b><span>${L[0]}</span></div><div class="stat"><b>${n}</b><span>${L[1]}</span></div><div class="stat"><b>⚡</b><span>${L[2]}</span></div></div>`}
const views={
 home(){
  return `<section class="hero"><h2 class="k">${t('heroT')}</h2><p>${t('sub')}</p><button class="cta" onclick="go('custom')">${ico('up')}${t('heroBtn')}</button>${heroStats()}<div class="arch">${ico('spark')}</div></section>
  <h3 class="sec k">${t('pickStage')}</h3><div class="stack">`+
   state.cat.stages.map(s=>`<button class="stage" style="--c:${STAGE[s.id]}" onclick="go('years',{stage:'${s.id}'})"><span class="n k">${s.years.length}</span><span><b class="k">${esc(nm(s))}</b><small>${t('yearsN')}</small></span><span class="go">‹</span></button>`).join('')+`</div>`;
 },
 years(){
  return `<h2 class="h k">${t('pickYear')}</h2><div class="duo">`+
   S().years.map((y,i)=>`<button class="archc" style="--c:${STAGE[state.stage]}" onclick="go('terms',{year:${i}})"><i class="k">${y.n}</i><b>${esc(nm(y))}</b></button>`).join('')+`</div>`;
 },
 terms(){
  return `<h2 class="h k">${t('pickTerm')}</h2><div class="duo">`+
   Y().terms.map((tm,i)=>`<button class="archc" style="--c:${i?'#8f3a2a':'#0e5a44'};min-height:190px" onclick="go('subjects',{term:${i}})"><i class="k" style="font-size:3.6rem">${tm.n}</i><b>${esc(nm(tm))}<small>${tm.subjects.length} ${t('subjectsN')}</small></b></button>`).join('')+`</div>`;
 },
 subjects(){
  return `<h2 class="h k">${t('pickSubject')}</h2><div class="covers">`+
   Tm().subjects.map((s,i)=>`<button class="cover ${s.pdf||s.pdf_url?'':'off'}" style="--c:${colorOf(s.code)}" onclick="go('subject',{subject:${i}})"><span class="em">${emo(s.code)}</span><b>${esc(nm(s))}</b><span class="chips">${chip(s.pdf||s.pdf_url,'book')}${chip(s.solved||s.solved_url,'check')}${chip(true,'play')}${chip(s.pdf,'spark')}</span></button>`).join('')+`</div>`;
 },
 subject(){
  const s=Sub(),u=`/api/book/${encodeURIComponent(s.code)}/pdf`;
  const book=s.pdf?u:s.pdf_url, sol=s.solved?u+'?solved=1':s.solved_url;
  return `<div class="shero" style="--c:${colorOf(s.code)}"><span class="em">${emo(s.code)}</span><div><h2 class="k">${esc(nm(s))}</h2><p>${esc(nm(Y()))} · ${esc(nm(Tm()))}</p></div></div>`+
   (s.pdf?`<button class="row ai" onclick="go('ask')"><span class="ic">${ico('spark')}</span><span><b>${t('ask')}</b><small>${t('askHint')}</small></span><span class="ar">‹</span></button>`
         :rowOff('spark',t('ask'),t('notAvail')))+
   (book?rowA(book,'book',t('book'),t('bookHint')):rowOff('book',t('book'),t('soon')))+
   (sol?rowA(sol,'check',t('solved'),t('solvedHint')):rowOff('check',t('solved'),t('soon')))+
   rowA(s.video,'play',t('videos'),t('videosHint'))+
   (ADMIN?`<p class="hint">books/${esc(s.code)}.pdf · books/${esc(s.code)}_solved.pdf</p>`:'');
 },
 ask(){
  return `<h2 class="h k">${esc(nm(Sub()))}</h2>
   <div class="field"><label for="q">${t('qLabel')}</label><input class="inp" type="text" id="q" placeholder="${t('qPh')}" enterkeyhint="search" autocomplete="off" onkeydown="if(event.key==='Enter')doAsk()"></div>
   <button class="btn" id="go" onclick="doAsk()">${t('search')}</button><div id="out" aria-live="polite"></div>`;
 },
 custom(){
  return `<h2 class="h k">${t('customTitle')}</h2><p class="hint" style="margin-top:-6px">${t('customSub')}</p>
   <label class="drop">${ico('up')}<input type="file" id="f" accept="application/pdf" onchange="pickFile(this.files[0])"><b>${t('dropT')}</b><small>${t('dropS')}</small></label>
   <div class="upst" id="upst">${upHTML()}</div>
   <div class="field"><label for="q">${t('qLabel')}</label><input class="inp" type="text" id="q" placeholder="${t('qPh')}" autocomplete="off" onkeydown="if(event.key==='Enter')doCustom()"></div>
   <button class="btn" id="go" onclick="doCustom()">${t('search')}</button><div id="out" aria-live="polite"></div>`;
 }
};

function upHTML(){
  const u=state.up;
  if(u.mode==='prog')return `<div class="prog"><i style="width:${u.pct}%"></i></div><p class="hint" style="margin:6px 2px 0">${t('uploading')} ${u.pct}% · ${esc(u.name)}</p>`;
  if(u.mode==='ok'&&state.custom)return `<div class="okchip"><span>✓ ${t('ready')} · ${state.custom.pages} ${t('pagesN')}</span><bdi>${esc(state.custom.name)}</bdi></div>`;
  if(u.mode==='err')return `<div class="errchip">${esc(u.msg)}</div>`;
  return '';
}
function setUp(p){state.up=p;const e=$('upst');if(e)e.innerHTML=upHTML()}
function pickFile(f){
  if(!f)return;
  state.custom=null;
  if(f.size>500*1024*1024){setUp({mode:'err',msg:t('tooBig')});return}
  const xhr=new XMLHttpRequest();xhr.open('POST','/api/custom/upload');
  xhr.upload.onprogress=e=>{if(e.lengthComputable)setUp({mode:'prog',pct:Math.round(e.loaded/e.total*100),name:f.name})};
  xhr.onload=()=>{let d={};try{d=JSON.parse(xhr.responseText)}catch(_){}
    if(xhr.status===200&&d.file_id){state.custom={id:d.file_id,name:f.name,pages:d.pages};setUp({mode:'ok'})}
    else setUp({mode:'err',msg:d.error_ar||d.error||t('errServer')})};
  xhr.onerror=()=>setUp({mode:'err',msg:t('errNet')});
  const fd=new FormData();fd.append('file',f);setUp({mode:'prog',pct:0,name:f.name});xhr.send(fd);
}

function render(){
  document.documentElement.lang=state.lang;document.documentElement.dir=state.lang==='ar'?'rtl':'ltr';document.body.dir=document.documentElement.dir;
  $('ttl').textContent=t('title');$('sub').textContent=t('sub');$('lang').textContent=state.lang==='ar'?'English':'عربي';$('zclose').textContent=t('close');
  const nb=(on,fn,ic,l)=>`<button class="${on?'on':''}" onclick="${fn}">${ico(ic)}<span>${l}</span></button>`;
  $('nav').innerHTML=nb(state.view!=='custom','go(\'home\')','home',t('home'))+nb(state.view==='custom','go(\'custom\')','up',t('upload'))+nb(false,'toggleLang()','globe',t('lang'));
  if(!state.cat){$('app').innerHTML='<div class="loading"><div class="sk"></div><div class="sk"></div><div class="sk"></div></div>';return}
  $('app').innerHTML=(state.view==='home'?'':crumbs())+views[state.view]();
  const cr=document.querySelector('.crumbs');if(cr)cr.scrollLeft=state.lang==='ar'?-cr.scrollWidth:cr.scrollWidth;
}

let timer=null,lastRun=null;
function loading(){
  const m=[t('l1'),t('l2'),t('l3'),t('l4')];let i=0;
  $('out').innerHTML=`<div class="loading"><p id="lm">${m[0]}</p><div class="sk"></div><div class="sk"></div><div class="sk"></div><div class="sk img"></div></div>`;
  clearInterval(timer);timer=setInterval(()=>{i=Math.min(i+1,m.length-1);const e=$('lm');if(e)e.textContent=m[i===2&&Math.random()<.5?3:i]},3000);
}
function showError(msg,retry,detail){
  $('out').innerHTML=`<div class="err"><p>${esc(msg)}</p>${detail?`<p dir="ltr" style="margin-top:8px;font-size:.75rem;opacity:.85;overflow-wrap:anywhere">${esc(detail)}</p>`:''}</div>`+(retry?`<button class="btn alt" onclick="lastRun()">${t('retry')}</button>`:'');
}
function showResult(d,o){
  o=o||{};
  const same=d.pdf_page===d.page_number;
  $('out').innerHTML=`<div class="res"><div class="rtop">${d.page_number?`<div class="med"><small>${t('page')}</small><b>${esc(d.page_number)}</b></div>`:''}${same||!d.pdf_page||o.pending?'':`<span class="fp">${t('pdfPage')}: ${esc(d.pdf_page)}</span>`}</div>
   ${d.note?`<p class="note">${esc(d.note)}</p>`:''}
   ${d.detail?`<p dir="ltr" class="hint">${esc(d.detail)}</p>`:''}
   ${d.verified?`<p class="vchip">✓ ${t('verified')}</p>`:''}
   ${o.pending?`<p class="aiwait"><span class="dot"></span>${t('aiThinking')}</p>`:''}
   ${d.answer?`<p class="atext">${esc(d.answer)}</p>`:''}
   ${d.highlight?`<blockquote><small>${t('fromBook')}</small>${esc(d.highlight)}</blockquote>`:''}
   ${d.page_image?`<figure><img src="${d.page_image}" alt="" onclick="zoomImg(this.src)"><figcaption>${t('pageCap')}</figcaption></figure>`:''}</div>`;
  if(!o.noscroll)$('out').scrollIntoView({behavior:'smooth',block:'start'});
}
function zoomImg(src){$('zimg').src=src;$('zoom').classList.add('on')}
let runId=0;
function localView(d,note){return {page_number:d.page_number,pdf_page:d.pdf_page,highlight:d.snippet,page_image:d.page_image,note:note}}
async function runAsk(urlLocate,urlAsk){
  const my=++runId;lastRun=()=>runAsk(urlLocate,urlAsk);
  $('go').disabled=true;loading();
  const ctrl=new AbortController(),to=setTimeout(()=>ctrl.abort(),130000);
  let local=null,done=false;
  fetch(urlLocate,{signal:ctrl.signal}).then(r=>r.ok?r.json():null).then(d=>{
    if(d&&d.found&&!done&&my===runId){local=d;clearInterval(timer);showResult(localView(d),{pending:true})}
  }).catch(()=>{});
  try{
    const r=await fetch(urlAsk,{signal:ctrl.signal});const txt=await r.text();let d;
    try{d=JSON.parse(txt)}catch(_){d={error_ar:t('errServer')}}
    if(my!==runId)return;
    done=true;clearInterval(timer);
    if(r.ok)showResult(d,{noscroll:!!local});
    else if(local)showResult(localView(local,t('aiFail')),{noscroll:true});
    else showError(d.error_ar||d.error||t('errServer'),d.retry!==false,d.detail);
  }catch(e){
    if(my!==runId)return;
    done=true;clearInterval(timer);
    if(local)showResult(localView(local,t('aiFail')),{noscroll:true});
    else showError(e.name==='AbortError'?t('errSlow'):t('errNet'),true);
  }finally{clearTimeout(to);if(my===runId){const b=$('go');if(b)b.disabled=false}}
}
function doAsk(){
  const q=$('q').value.trim();if(!q){$('q').focus();return}
  const c=encodeURIComponent(Sub().code),qq=encodeURIComponent(q);
  runAsk(`/api/locate?book_code=${c}&query=${qq}`,`/api/search?book_code=${c}&query=${qq}`);
}
function doCustom(){
  if(!state.custom){$('f').click();return}
  const q=$('q').value.trim();if(!q){$('q').focus();return}
  const id=state.custom.id,qq=encodeURIComponent(q);
  runAsk(`/api/custom/locate?file_id=${id}&query=${qq}`,`/api/custom/ask?file_id=${id}&query=${qq}`);
}

(async function init(){
  render();
  try{const r=await fetch('/api/catalog');state.cat=await r.json();}
  catch(e){$('app').innerHTML=`<div class="err"><p>${t('errServer')}</p></div><button class="btn alt" onclick="location.reload()">${t('retry')}</button>`;return}
  history.replaceState({view:'home'},'');render();
})();
</script>
</body>
</html>
"""

ADMIN_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>لوحة الإدارة</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;600;700&family=Reem+Kufi:wght@700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f1ea;color:#13211c;font-family:'IBM Plex Sans Arabic',sans-serif;line-height:1.7;padding:16px}
.w{max-width:680px;margin:0 auto}h1{font-family:'Reem Kufi',sans-serif;color:#0e5a44;margin:0 0 4px}
.card{background:#fff;border:1px solid #d9d4c4;border-radius:18px;padding:16px;margin:14px 0}
h2{margin:0 0 10px;font-size:1.05rem}label{display:block;font-weight:600;margin:10px 0 4px;font-size:.9rem}
select,input[type=text],input[type=password],textarea{width:100%;padding:11px;border:1.5px solid #b9b39c;border-radius:12px;font:inherit;background:#fff}
textarea{min-height:170px;direction:rtl;font-size:.9rem}
button{width:100%;margin-top:12px;padding:13px;border:0;border-radius:12px;background:#0e5a44;color:#f1e2b8;font:inherit;font-weight:700;cursor:pointer}
.bar{height:10px;background:#e4dfcf;border-radius:9px;overflow:hidden;margin-top:10px}.bar i{display:block;height:100%;width:0;background:#c8a04a}
.msg{margin-top:10px;font-size:.9rem}.ok{color:#0e5a44}.bad{color:#a2301f}.hint{color:#5d6b64;font-size:.82rem}
table{width:100%;border-collapse:collapse;font-size:.8rem}td,th{border-bottom:1px solid #e4dfcf;padding:4px 6px;text-align:start}
</style></head><body><div class="w">
<h1>لوحة الإدارة</h1><p class="hint">ارفع الكتب بدل وضعها يدوياً في المجلد. تحتاج المتغير ADMIN_KEY في Render.</p>
<div class="card"><label>مفتاح الإدارة</label><input type="password" id="key" placeholder="ADMIN_KEY"></div>

<div class="card"><h2>1) رفع كتاب واحد</h2>
<label>المرحلة</label><select id="st"></select><label>الصف</label><select id="yr"></select>
<label>الفصل</label><select id="tm"></select><label>المادة</label><select id="sb"></select>
<label>النوع</label><select id="kind"><option value="pdf">الكتاب (غير محلول)</option><option value="solved">الكتاب محلول</option></select>
<label>ملف PDF (حتى 500 ميجابايت)</label><input type="file" id="file" accept="application/pdf">
<button onclick="one()">رفع</button><div class="bar"><i id="b1"></i></div><div class="msg" id="m1"></div></div>

<div class="card"><h2>2) رفع جماعي</h2><p class="hint">سمِّ كل ملف باسم الكود: p3_t1_math.pdf أو p3_t1_math_solved.pdf (القائمة الكاملة: <a href="/api/missing" target="_blank">/api/missing</a>)</p>
<input type="file" id="bulk" accept="application/pdf" multiple><button onclick="bulk()">رفع الملفات</button><div class="bar"><i id="b2"></i></div><div class="msg" id="m2"></div></div>

<div class="card"><h2>3) ربط رابط خارجي (كتاب، محلول، أو فيديو)</h2>
<label>الكود (يتبع الاختيارات أعلاه)</label><input type="text" id="code" readonly>
<label>النوع</label><select id="lk"><option value="pdf">رابط الكتاب</option><option value="solved">رابط المحلول</option><option value="video">رابط شرح الدروس (قائمة تشغيل أو فيديو)</option></select>
<label>الرابط (اتركه فارغاً للحذف)</label><input type="text" id="url" placeholder="https://..." dir="ltr">
<button onclick="link()">حفظ الرابط</button><div class="msg" id="m3"></div></div>

<div class="card"><h2>4) تعديل مواد الفصل المختار</h2>
<p class="hint">يتبع المرحلة والصف والفصل أعلاه. كل سطر: <b dir="ltr">slug|الاسم العربي|English</b> مثل <span dir="ltr">math|الرياضيات 2-2|Mathematics 2-2</span> (slug بالإنجليزية بدون شرطة سفلية). اتركه فارغاً للرجوع للقائمة الافتراضية. غيّر اسم الـ slug يغيّر اسم ملف الكتاب المطلوب.</p>
<textarea id="subs"></textarea>
<button onclick="saveSubs()">حفظ مواد هذا الفصل</button><div class="msg" id="m4"></div></div>

<div class="card"><h2>5) فحص الذكاء الاصطناعي</h2><p class="hint">يجرّب كل موديل ويعرض سبب الفشل (مفتاح خاطئ، اسم موديل غير صحيح، انتهاء الرصيد...).</p><button onclick="aiTest()">افحص الآن</button><div class="msg" id="m5"></div></div>
<div class="card"><h2>حالة الكتب</h2><div id="tbl" class="hint">...</div></div></div>
<script>
let cat;const $=i=>document.getElementById(i);
const key=()=>{const k=$('key').value;sessionStorage.setItem('ak',k);return k};
$('key').value=sessionStorage.getItem('ak')||'';
function fill(sel,arr,fn){sel.innerHTML=arr.map((x,i)=>`<option value="${i}">${fn(x)}</option>`).join('')}
function term(){const s=cat.stages[$('st').value],y=s.years[$('yr').value];return y.terms[$('tm').value]}
function cur(){return term().subjects[$('sb').value]}
function group(){const m=(term().subjects[0]||{code:''}).code.match(/^([a-z]\d_t\d)_/);return m?m[1]:('' + 'psm'[+$('st').value] + (+$('yr').value+1) + '_t' + (+$('tm').value+1))}
function onSt(){fill($('yr'),cat.stages[$('st').value].years,y=>y.ar);onYr()}
function onYr(){fill($('tm'),cat.stages[$('st').value].years[$('yr').value].terms,t=>t.ar);onTm()}
function onTm(){fill($('sb'),term().subjects,s=>s.ar);
 $('subs').value=term().subjects.map(s=>s.code.replace(/^[a-z]\d_t\d_/,'')+'|'+s.ar+'|'+s.en).join('\n');onSb()}
function onSb(){const s=cur();$('code').value=s?s.code:''}
function xhr(url,fd,bar,msg,done){const x=new XMLHttpRequest();x.open('POST',url);
 x.upload.onprogress=e=>{if(e.lengthComputable)$(bar).style.width=(e.loaded/e.total*100)+'%'};
 x.onload=()=>{let d={};try{d=JSON.parse(x.responseText)}catch(_){}
  if(x.status===200){$(msg).className='msg ok';$(msg).textContent=done(d);load()}else{$(msg).className='msg bad';$(msg).textContent=d.error_ar||d.error||'فشل الرفع'}};
 x.onerror=()=>{$(msg).className='msg bad';$(msg).textContent='انقطع الاتصال'};
 $(bar).style.width='0';$(msg).textContent='جاري الرفع...';x.send(fd)}
function one(){const f=$('file').files[0];if(!f)return;const fd=new FormData();fd.append('key',key());fd.append('code',cur().code);fd.append('kind',$('kind').value);fd.append('file',f);
 xhr('/api/admin/upload',fd,'b1','m1',()=>'تم رفع '+cur().code)}
function bulk(){const fs=$('bulk').files;if(!fs.length)return;const fd=new FormData();fd.append('key',key());for(const f of fs)fd.append('files',f);
 xhr('/api/admin/bulk',fd,'b2','m2',d=>{const ok=d.results.filter(r=>r.ok).length;const bad=d.results.filter(r=>!r.ok).map(r=>r.file+': '+r.error).join(' | ');return `تم رفع ${ok} من ${d.results.length}`+(bad?' — '+bad:'')})}
async function link(){const fd=new FormData();fd.append('key',key());fd.append('code',cur().code);fd.append('kind',$('lk').value);fd.append('url',$('url').value.trim());
 const r=await fetch('/api/admin/link',{method:'POST',body:fd});const d=await r.json();$('m3').className='msg '+(r.ok?'ok':'bad');$('m3').textContent=r.ok?'تم الحفظ':(d.error_ar||'فشل');load()}
async function saveSubs(){const fd=new FormData();fd.append('key',key());fd.append('group',group());fd.append('text',$('subs').value);
 const r=await fetch('/api/admin/catalog',{method:'POST',body:fd});const d=await r.json();$('m4').className='msg '+(r.ok?'ok':'bad');
 $('m4').textContent=r.ok?('تم الحفظ ('+d.count+' مادة)'):(d.error_ar||'فشل');if(r.ok)load(true)}
async function aiTest(){const fd=new FormData();fd.append('key',key());$('m5').className='msg';$('m5').textContent='جاري الفحص...';
 const r=await fetch('/api/admin/ai-test',{method:'POST',body:fd});const d=await r.json();
 if(!r.ok){$('m5').className='msg bad';$('m5').textContent=d.error_ar||'فشل';return}
 let h=d.results.map(x=>`<div class="${x.ok?'ok':'bad'}" dir="ltr">${x.provider} / ${x.model}: ${x.ok?('OK ('+x.ms+' ms)'):('ERROR '+x.error)}</div>`).join('');
 if(!d.results.length)h='<div class="bad">لا يوجد أي مفتاح مضبوط (ANTHROPIC_API_KEY / GEMINI_API_KEY)</div>';
 $('m5').innerHTML=h}
async function load(keepText){cat=await (await fetch('/api/catalog')).json();
 const keep=[$('st').value,$('yr').value,$('tm').value,$('sb').value];
 fill($('st'),cat.stages,s=>s.ar);if(keep[0])$('st').value=keep[0];fill($('yr'),cat.stages[$('st').value].years,y=>y.ar);if(keep[1])$('yr').value=keep[1];
 fill($('tm'),cat.stages[$('st').value].years[$('yr').value].terms,t=>t.ar);if(keep[2])$('tm').value=keep[2];
 fill($('sb'),term().subjects,s=>s.ar);if(keep[3])$('sb').value=keep[3];onSb();
 if(!$('subs').value||keepText)$('subs').value=term().subjects.map(s=>s.code.replace(/^[a-z]\d_t\d_/,'')+'|'+s.ar+'|'+s.en).join('\n');
 let n=0,h=0,rows='';for(const s of cat.stages)for(const y of s.years)for(const t of y.terms)for(const b of t.subjects){n++;if(b.pdf)h++;if(b.pdf||b.solved)rows+=`<tr><td>${b.code}</td><td>${b.ar}</td><td>${b.pdf?'✓':''}</td><td>${b.solved?'✓':''}</td></tr>`}
 $('tbl').innerHTML=`<p>${h} من ${n} كتاب مرفوع</p>`+(rows?`<table><tr><th>الكود</th><th>المادة</th><th>كتاب</th><th>محلول</th></tr>${rows}</table>`:'')}
$('st').onchange=onSt;$('yr').onchange=onYr;$('tm').onchange=onTm;$('sb').onchange=onSb;load();
</script></body></html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
