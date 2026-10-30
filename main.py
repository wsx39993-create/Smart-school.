import os
import re
import json
import base64
import fitz  # PyMuPDF
import anthropic
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# المفتاح يُقرأ من متغيرات البيئة (Render -> Environment -> ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# موديل Claude (للتوفير في التكلفة يمكنك استخدام claude-haiku-5-5)
MODEL_NAME = "claude-sonnet-5-5"

# دقة تحويل الصفحة إلى صورة قبل إرسالها للموديل (الإحداثيات اللي يرجعها الموديل تعتمد عليها)
ANALYSIS_DPI = 110

app = FastAPI(title="Smart Curriculum Educational Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BOOKS_DIR = os.path.join(os.path.dirname(__file__), "books")


def rank_pages(doc: fitz.Document, query: str, top_n: int = 3, scan_limit: int = 400):
    """يرتّب صفحات الكتاب حسب تطابق كلمات السؤال ويرجع أفضل الصفحات."""
    keywords = [k.strip().lower() for k in query.split() if len(k.strip()) > 2]
    scored = []
    for idx in range(min(len(doc), scan_limit)):
        text = doc[idx].get_text("text").lower()
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scored.append((idx, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    if scored:
        return [i for i, _ in scored[:top_n]]
    # إذا لم نجد تطابقاً نصياً (مثلاً كتاب ممسوح ضوئياً) نرسل أول الصفحات
    return list(range(min(len(doc), top_n)))


def render_page_with_highlight(doc: fitz.Document, page_num: int,
                               highlight_text: str = "", boxes_px=None) -> str:
    """يرسم الصفحة كاملة ويظلل الإجابة بالأحمر.
    1) إذا الملف فيه نص قابل للبحث نظلل النص نفسه.
    2) وإلا (ملف صور) نستخدم المستطيلات اللي حددها الموديل على الصورة."""
    page = doc[page_num]
    rects = []

    if highlight_text:
        try:
            rects = list(page.search_for(highlight_text))
        except Exception:
            rects = []

    if not rects and boxes_px:
        k = 72.0 / ANALYSIS_DPI  # من بكسل الصورة إلى نقاط الـ PDF
        for b in boxes_px:
            try:
                x0, y0, x1, y1 = [float(v) for v in b]
            except Exception:
                continue
            r = fitz.Rect(min(x0, x1) * k, min(y0, y1) * k, max(x0, x1) * k, max(y0, y1) * k)
            r = r & page.rect
            if not r.is_empty:
                rects.append(r)

    for r in rects:
        page.draw_rect(r, color=(1, 0, 0), fill=(1, 0, 0), fill_opacity=0.22,
                       width=1.5, overlay=True)

    pix = page.get_pixmap(dpi=120)
    encoded = base64.b64encode(pix.tobytes("png")).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def parse_reply(raw: str, page_indices):
    """يفصل إجابة الموديل عن أسطر البيانات الأربعة في آخرها."""
    first_marker = re.search(r"(?m)^\s*(PAGE|PRINTED_PAGE|BOXES|EXACT_TEXT)\s*:", raw)
    answer = raw[:first_marker.start()].strip() if first_marker else raw.strip()

    page_idx = page_indices[0]
    m = re.search(r"(?m)^\s*PAGE\s*:\s*(\d+)", raw)
    if m:
        n = int(m.group(1)) - 1
        if n in page_indices:
            page_idx = n

    printed = None
    m = re.search(r"(?m)^\s*PRINTED_PAGE\s*:\s*(\d+)", raw)
    if m and int(m.group(1)) > 0:
        printed = int(m.group(1))

    boxes = []
    m = re.search(r"(?m)^\s*BOXES\s*:\s*(\[.*\])\s*$", raw)
    if m:
        try:
            data = json.loads(m.group(1))
            boxes = [b for b in data if isinstance(b, list) and len(b) == 4]
        except Exception:
            boxes = []

    highlight = ""
    m = re.search(r"(?m)^\s*EXACT_TEXT\s*:\s*(.+)$", raw)
    if m:
        highlight = m.group(1).strip(" \"'«»")

    return answer, page_idx, printed, boxes, highlight


def ask_ai(query: str, doc: fitz.Document, page_indices):
    """يرسل صور الصفحات المرشحة إلى Claude ويرجع نتيجة جاهزة للعرض."""
    if client is None:
        raise RuntimeError("لم يتم ضبط ANTHROPIC_API_KEY في إعدادات Render.")

    content = []
    for idx in page_indices:
        pix = doc[idx].get_pixmap(dpi=ANALYSIS_DPI)
        png = pix.tobytes("png")
        content.append({"type": "text",
                        "text": f"صفحة رقم {idx + 1} (أبعاد الصورة: {pix.width}x{pix.height} بكسل):"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(png).decode("utf-8"),
            },
        })

    content.append({"type": "text", "text": (
        "أنت معلم وخبير في المناهج الدراسية. اعتماداً على صفحات الكتاب المرفقة فقط، "
        f"أجب بوضوح ودقة وباللغة العربية على السؤال التالي: '{query}'.\n"
        "إذا لم تجد الإجابة في الصفحات المرفقة فقل ذلك بصراحة ولا تخترع إجابة.\n"
        "بعد الإجابة أضف أربعة أسطر بهذا الشكل بالضبط، كل واحد في سطر مستقل:\n"
        "PAGE: رقم الصفحة المرفقة التي وجدت فيها الإجابة، كما كُتب في عنوان الصورة (أو 0 إذا لم تجدها)\n"
        "PRINTED_PAGE: الرقم المطبوع على الصفحة نفسها داخل الكتاب (يكون غالباً في أسفل الصفحة)، أو 0 إذا لا يوجد رقم مطبوع\n"
        "BOXES: مصفوفة JSON من المستطيلات [x0,y0,x1,y1] بالبكسل على صورة تلك الصفحة "
        "(الزاوية العليا اليسرى هي 0,0)، مستطيل واحد لكل سطر من سطور الجملة التي فيها الإجابة، "
        "تحيط بالجملة فقط، مثال: [[80,300,500,324],[300,330,500,352]]. إذا لم تجد الإجابة اكتب []\n"
        "EXACT_TEXT: الجملة الدقيقة من الكتاب التي تحتوي الإجابة، منسوخة كما هي"
    )})

    resp = client.messages.create(
        model=MODEL_NAME,
        max_tokens=1500,
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    return parse_reply(raw, page_indices)


def build_result(doc: fitz.Document, query: str, empty_msg: str):
    candidates = rank_pages(doc, query)
    answer, page_idx, printed, boxes, highlight = ask_ai(query, doc, candidates)
    if not answer:
        answer = empty_msg

    img_b64 = render_page_with_highlight(doc, page_idx, highlight, boxes)
    return {
        # رقم الصفحة المطبوع داخل الكتاب، وإذا ما انقرأ نرجع ترتيبها في الملف
        "page_number": printed if printed else page_idx + 1,
        "pdf_page": page_idx + 1,
        "answer": answer,
        "highlight": highlight,
        "page_image": img_b64
    }


@app.get("/api/search")
def search_in_book(book_code: str = Query(...), query: str = Query(...)):
    pdf_path = os.path.join(BOOKS_DIR, f"{book_code}.pdf")

    if not os.path.exists(pdf_path):
        return JSONResponse(
            status_code=404,
            content={"error_ar": "عذراً، الكتاب غير متوفر حالياً في المكتبة الرقمية.",
                      "error_en": "Sorry, this book is currently not available in the digital library."}
        )

    try:
        doc = fitz.open(pdf_path)
        return build_result(doc, query, "لم يتم العثور على إجابة واضحة.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/custom-pdf-search")
async def search_custom_pdf(file: UploadFile = File(...), query: str = Form(...)):
    if not file.filename.lower().endswith('.pdf'):
        return JSONResponse(status_code=400, content={"error": "يجب أن يكون الملف بصيغة PDF فقط."})

    try:
        contents = await file.read()
        doc = fitz.open(stream=contents, filetype="pdf")
        return build_result(doc, query, "لم يتم العثور على إجابة كافية في هذا المستند.")
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"خطأ في المعالجة: {str(e)}"})


@app.get("/", response_class=HTMLResponse)
def index():
    html_content = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>المنصة التعليمية الذكية - Smart Curriculum Platform</title>
    <link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;900&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-gradient: linear-gradient(135deg, #0f172a 0%, #1e1b4b 50%, #311042 100%);
            --card-bg: rgba(255, 255, 255, 0.95);
            --primary: #4f46e5;
            --primary-hover: #4338ca;
            --accent: #8b5cf6;
            --text-dark: #0f172a;
        }

        body {
            font-family: 'Tajawal', sans-serif;
            background: var(--bg-gradient);
            min-height: 100vh;
            margin: 0;
            padding: 25px 12px;
            color: var(--text-dark);
        }

        .container {
            max-width: 850px;
            margin: 0 auto;
            background: var(--card-bg);
            border-radius: 28px;
            padding: 30px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            backdrop-filter: blur(12px);
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 18px;
            margin-bottom: 25px;
        }

        .title-area h1 {
            font-size: 1.8rem;
            color: var(--text-dark);
            margin: 0;
            font-weight: 900;
        }

        .lang-btn {
            background: var(--primary);
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 12px;
            cursor: pointer;
            font-weight: bold;
            font-family: 'Tajawal', sans-serif;
            transition: 0.3s;
            box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
        }
        .lang-btn:hover { background: var(--primary-hover); }

        .custom-ai-banner {
            background: linear-gradient(135deg, #8b5cf6, #d946ef);
            color: white;
            padding: 22px;
            border-radius: 20px;
            text-align: center;
            margin-bottom: 25px;
            cursor: pointer;
            box-shadow: 0 10px 25px rgba(217, 70, 239, 0.35);
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .custom-ai-banner:hover { transform: translateY(-2px); box-shadow: 0 14px 30px rgba(217, 70, 239, 0.45); }
        .custom-ai-banner h2 { margin: 0 0 6px 0; font-size: 1.5rem; }

        .section-title {
            font-size: 1.4rem;
            font-weight: 800;
            color: var(--text-dark);
            margin-bottom: 20px;
            border-right: 5px solid var(--primary);
            padding-right: 12px;
        }
        body[dir="ltr"] .section-title {
            border-right: none;
            border-left: 5px solid var(--primary);
            padding-right: 0;
            padding-left: 12px;
        }

        .grid-buttons {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }

        .btn-card {
            background: #ffffff;
            border: 2px solid #e2e8f0;
            padding: 18px;
            border-radius: 16px;
            text-align: center;
            font-weight: bold;
            font-size: 1.05rem;
            cursor: pointer;
            transition: all 0.25s ease;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.04);
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .btn-card:hover {
            border-color: var(--primary);
            color: var(--primary);
            transform: translateY(-3px);
            box-shadow: 0 10px 20px -5px rgba(79, 70, 229, 0.2);
        }

        .back-btn {
            background: #f1f5f9;
            color: #475569;
            border: 1px solid #cbd5e1;
            padding: 10px 18px;
            border-radius: 12px;
            cursor: pointer;
            font-weight: bold;
            margin-bottom: 20px;
            display: inline-block;
            transition: 0.2s;
        }
        .back-btn:hover { background: #e2e8f0; color: #1e293b; }

        .hidden { display: none !important; }

        .input-group {
            margin-bottom: 18px;
        }
        .input-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: bold;
            font-size: 1.05rem;
        }
        .input-group input[type="text"], .input-group input[type="file"] {
            width: 100%;
            padding: 15px;
            border: 2px solid #cbd5e1;
            border-radius: 14px;
            font-family: inherit;
            box-sizing: border-box;
            font-size: 1rem;
            transition: border-color 0.2s;
        }
        .input-group input[type="text"]:focus {
            outline: none;
            border-color: var(--primary);
        }

        .action-btn {
            width: 100%;
            background: var(--primary);
            color: white;
            padding: 16px;
            border: none;
            border-radius: 14px;
            font-size: 1.1rem;
            font-weight: bold;
            cursor: pointer;
            box-shadow: 0 10px 20px -3px rgba(79, 70, 229, 0.4);
            transition: 0.2s;
        }
        .action-btn:hover { background: var(--primary-hover); }

        .result-box {
            margin-top: 25px;
            padding: 22px;
            background: #f8fafc;
            border-radius: 20px;
            border: 2px solid #e2e8f0;
        }
        .answer-highlight {
            color: #dc2626;
            font-weight: bold;
            background: #fee2e2;
            padding: 2px 6px;
            border-radius: 4px;
        }
        .page-preview {
            width: 100%;
            border-radius: 14px;
            border: 2px solid #cbd5e1;
            margin-top: 15px;
        }

        .spinner-container {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 25px 10px;
        }
        .spinner {
            width: 50px;
            height: 50px;
            border: 5px solid #e2e8f0;
            border-top: 5px solid var(--primary);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-bottom: 15px;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>

<div class="container">
    <div class="header">
        <div class="title-area">
            <h1 id="app-title">🎓 المنصة التعليمية الذكية للمناهج</h1>
        </div>
        <button class="lang-btn" onclick="toggleLanguage()" id="lang-toggle-btn">English / عربي</button>
    </div>

    <!-- بنر الذكاء الاصطناعي لكتابك الخاص -->
    <div class="custom-ai-banner" onclick="navigateTo('custom-pdf-screen')" id="banner-area">
        <h2 id="custom-pdf-title">🤖 الذكاء الاصطناعي لكتابك الخاص</h2>
        <span id="custom-pdf-desc">اضغط هنا لرفع أي كتاب PDF واسأل الذكاء الاصطناعي عنه مباشرة!</span>
    </div>

    <!-- شاشة الرفع الخاص -->
    <div id="custom-pdf-screen" class="hidden">
        <button class="back-btn" onclick="navigateTo('stages-screen')" id="back-stages-btn">← العودة للمراحل الدراسية</button>
        <h3 id="custom-upload-heading">ارفق كتابك الخاص (PDF) ووجّه سؤالك:</h3>
        <form id="custom-pdf-form" onsubmit="handleCustomPDFSubmit(event)">
            <div class="input-group">
                <label id="label-choose-file">اختر ملف الـ PDF:</label>
                <input type="file" id="custom-file-input" accept="application/pdf" required>
            </div>
            <div class="input-group">
                <label id="label-custom-query">اكتب سؤالك بوضوح تام:</label>
                <input type="text" id="custom-query-input" placeholder="مثال: ما هو قانون كبلر الاول؟" required>
            </div>
            <button type="submit" class="action-btn" id="custom-submit-btn">تحليل المستند واستخراج الإجابة 🔍</button>
        </form>
    </div>

    <!-- 1. شاشة المراحل الدراسية -->
    <div id="stages-screen">
        <div class="section-title" id="stages-heading">اختر المرحلة الدراسية:</div>
        <div class="grid-buttons">
            <div class="btn-card" onclick="selectStage('primary')" id="btn-primary">🎒 المرحلة الابتدائية</div>
            <div class="btn-card" onclick="selectStage('intermediate')" id="btn-intermediate">📚 المرحلة المتوسطة</div>
            <div class="btn-card" onclick="selectStage('secondary')" id="btn-secondary">🎓 المرحلة الثانوية (مسارات)</div>
        </div>
    </div>

    <!-- 2. شاشة السنوات الدراسية -->
    <div id="years-screen" class="hidden">
        <button class="back-btn" onclick="navigateTo('stages-screen')" id="back-to-stages">← اختيار مرحلة أخرى</button>
        <div class="section-title" id="years-heading">اختر السنة الدراسية:</div>
        <div class="grid-buttons" id="years-grid"></div>
    </div>

    <!-- 3. شاشة الفصول الدراسية -->
    <div id="terms-screen" class="hidden">
        <button class="back-btn" onclick="navigateTo('years-screen')" id="back-to-years">← اختيار سنة أخرى</button>
        <div class="section-title" id="terms-heading">اختر الفصل الدراسي:</div>
        <div class="grid-buttons" id="terms-grid"></div>
    </div>

    <!-- 4. شاشة المواد الدراسية -->
    <div id="subjects-screen" class="hidden">
        <button class="back-btn" onclick="navigateTo('terms-screen')" id="back-to-terms">← اختيار فصل آخر</button>
        <div class="section-title" id="subjects-heading">المواد الدراسية المتاحة:</div>
        <div class="grid-buttons" id="subjects-grid"></div>
    </div>

    <!-- 5. شاشة لوحة تحكم المادة المختارة -->
    <div id="subject-dashboard-screen" class="hidden">
        <button class="back-btn" onclick="navigateTo('subjects-screen')" id="back-to-subjects">← العودة لقائمة المواد</button>
        <div class="section-title" id="dashboard-heading">المادة: <span id="current-subject-name" style="color: var(--primary);"></span></div>
        <div class="grid-buttons">
            <div class="btn-card" onclick="openSubjectOption('solved')" id="opt-solved">📖 الكتاب محلول</div>
            <div class="btn-card" onclick="openSubjectOption('unsolved')" id="opt-unsolved">📘 الكتاب بدون حل</div>
            <div class="btn-card" onclick="openSubjectOption('videos')" id="opt-videos">🎥 شرح الدروس</div>
            <div class="btn-card" onclick="openSubjectOption('ai')" id="opt-ai" style="border-color: var(--accent); color: var(--accent);">🤖 المساعد الذكي لكتاب المادة</div>
        </div>
    </div>

    <!-- 6. واجهة البحث الذكي -->
    <div id="ai-search-screen" class="hidden">
        <button class="back-btn" onclick="navigateTo('subject-dashboard-screen')" id="back-to-dashboard">← الرجوع لخيارات المادة</button>
        <div class="section-title" id="ai-search-heading">🤖 المساعد الذكي لكتاب: <span id="ai-book-title" style="color: var(--primary);"></span></div>
        <div class="input-group">
            <label id="label-query">اكتب سؤالك بوضوح تام:</label>
            <input type="text" id="search-query" placeholder="مثال: ما هو قانون كبلر الاول؟" required>
        </div>
        <button class="action-btn" onclick="executeSearch()" id="search-btn">بحث ذكي وتحليل دقيق 🔍</button>
    </div>

    <!-- عرض نتائج البحث -->
    <div id="result-container" class="result-box hidden">
        <h3 id="result-heading" style="color: var(--primary); margin-top:0;">🎯 النتيجة والتحليل الذكي:</h3>
        <p><strong id="label-page-num">رقم الصفحة:</strong> <span id="result-page"></span></p>
        <p><strong id="label-answer-text">الإجابة:</strong> <span id="result-answer"></span></p>
        <div id="image-container" class="hidden">
            <h4 id="label-page-image">الصفحة المحددة من الكتاب:</h4>
            <img id="result-image" class="page-preview" src="" alt="صفحة الكتاب">
        </div>
    </div>
</div>

<script>
    let currentLang = 'ar';
    let currentStage = '';
    let currentYear = '';
    let currentTerm = '';
    let selectedSubject = '';
    let selectedBookCode = 'physics2_t1';

    const screens = [
        'stages-screen', 'years-screen', 'terms-screen', 
        'subjects-screen', 'subject-dashboard-screen', 
        'ai-search-screen', 'custom-pdf-screen'
    ];

    const translations = {
        ar: {
            appTitle: "🎓 المنصة التعليمية الذكية للمناهج",
            customPdfTitle: "🤖 الذكاء الاصطناعي لكتابك الخاص",
            customPdfDesc: "اضغط هنا لرفع أي كتاب PDF واسأل الذكاء الاصطناعي عنه مباشرة!",
            stagesHeading: "اختر المرحلة الدراسية:",
            btnPrimary: "🎒 المرحلة الابتدائية",
            btnIntermediate: "📚 المرحلة المتوسطة",
            btnSecondary: "🎓 المرحلة الثانوية (مسارات)",
            yearsHeading: "اختر السنة الدراسية:",
            termsHeading: "اختر الفصل الدراسي:",
            subjectsHeading: "المواد الدراسية المتاحة:",
            dashboardHeading: "المادة: ",
            aiSearchHeading: "🤖 المساعد الذكي لكتاب: ",
            labelQuery: "اكتب سؤالك بوضوح تام:",
            searchBtn: "بحث ذكي وتحليل دقيق 🔍",
            backStagesBtn: "← العودة للمراحل الدراسية",
            backToStages: "← اختيار مرحلة أخرى",
            backToYears: "← اختيار سنة أخرى",
            backToTerms: "← اختيار فصل آخر",
            backToSubjects: "← العودة لقائمة المواد",
            backToDashboard: "← الرجوع لخيارات المادة",
            customUploadHeading: "ارفق كتابك الخاص (PDF) ووجّه سؤالك:",
            labelChooseFile: "اختر ملف الـ PDF:",
            labelCustomQuery: "اكتب سؤالك بوضوح تام:",
            customSubmitBtn: "تحليل المستند واستخراج الإجابة 🔍",
            optSolved: "📖 الكتاب محلول",
            optUnsolved: "📘 الكتاب بدون حل",
            optVideos: "🎥 شرح الدروس",
            optAi: "🤖 المساعد الذكي لكتاب المادة",
            labelPageNum: "رقم الصفحة:",
            labelAnswerText: "الإجابة:",
            labelPageImage: "الصفحة المحددة من الكتاب:",
            term1: "الفصل الدراسي الأول",
            term2: "الفصل الدراسي الثاني",
            year1: "الصف الأول", year2: "الصف الثاني", year3: "الصف الثالث",
            year4: "الصف الرابع", year5: "الصف الخامس", year6: "الصف السادس",
            yearSec1: "السنة الأولى المشتركة", yearSec2: "السنة الثانية", yearSec3: "السنة الثالثة"
        },
        en: {
            appTitle: "🎓 Smart Curriculum Educational Platform",
            customPdfTitle: "🤖 AI for Your Custom PDF Book",
            customPdfDesc: "Click here to upload any PDF book and ask AI questions directly!",
            stagesHeading: "Select Educational Stage:",
            btnPrimary: "🎒 Primary School",
            btnIntermediate: "📚 Middle School",
            btnSecondary: "🎓 High School (Tracks)",
            yearsHeading: "Select Academic Year:",
            termsHeading: "Select Term:",
            subjectsHeading: "Available Subjects:",
            dashboardHeading: "Subject: ",
            aiSearchHeading: "🤖 AI Assistant for: ",
            labelQuery: "Write your question clearly:",
            searchBtn: "Smart Search & Analyze 🔍",
            backStagesBtn: "← Back to Educational Stages",
            backToStages: "← Select Another Stage",
            backToYears: "← Select Another Year",
            backToTerms: "← Select Another Term",
            backToSubjects: "← Back to Subjects List",
            backToDashboard: "← Back to Subject Options",
            customUploadHeading: "Upload your custom PDF book & ask a question:",
            labelChooseFile: "Select PDF File:",
            labelCustomQuery: "Write your question clearly:",
            customSubmitBtn: "Analyze Document & Extract Answer 🔍",
            optSolved: "📖 Solved Book",
            optUnsolved: "📘 Unsolved Book",
            optVideos: "🎥 Video Lessons",
            optAi: "🤖 AI-Powered Book Assistant",
            labelPageNum: "Page Number:",
            labelAnswerText: "Answer:",
            labelPageImage: "Selected Book Page:",
            term1: "First Semester",
            term2: "Second Semester",
            year1: "Grade 1", year2: "Grade 2", year3: "Grade 3",
            year4: "Grade 4", year5: "Grade 5", year6: "Grade 6",
            yearSec1: "First Common Year", yearSec2: "Second Year", yearSec3: "Third Year"
        }
    };

    const subjectsData = {
        primary: {
            years: [1, 2, 3, 4, 5, 6],
            subjects: {
                ar: ["القرآن الكريم", "التربية الإسلامية", "لغتي", "الرياضيات", "العلوم", "الدراسات الاجتماعية", "المهارات الرقمية", "التربية الفنية"],
                en: ["Holy Quran", "Islamic Studies", "My Language", "Mathematics", "Science", "Social Studies", "Digital Skills", "Art Education"]
            }
        },
        intermediate: {
            years: [1, 2, 3],
            subjects: {
                ar: ["القرآن والتفسير", "الحديث والسيرة", "التوحيد والفقه", "لغتي الخالدة", "اللغة الإنجليزية", "الرياضيات", "العلوم", "الدراسات الاجتماعية"],
                en: ["Quran & Tafseer", "Hadith & Seerah", "Islamic Faith & Fiqh", "Arabic Language", "English Language", "Mathematics", "Science", "Social Studies"]
            }
        },
        secondary: {
            years: [1, 2, 3],
            subjects: {
                ar: ["الفيزياء 2", "الكيمياء", "الأحياء", "الرياضيات 2", "التقنية الرقمية", "الدراسات الاجتماعية", "اللغة الإنجليزية", "التفكير النقدي"],
                en: ["Physics 2", "Chemistry", "Biology", "Mathematics 2", "Digital Technology", "Social Studies", "English Language", "Critical Thinking"]
            }
        }
    };

    function navigateTo(screenId) {
        screens.forEach(s => {
            const el = document.getElementById(s);
            if (el) el.classList.add('hidden');
        });
        document.getElementById(screenId).classList.remove('hidden');
        
        if (screenId === 'custom-pdf-screen') {
            document.getElementById('banner-area').classList.add('hidden');
        } else {
            document.getElementById('banner-area').classList.remove('hidden');
        }
        
        document.getElementById('result-container').classList.add('hidden');
    }

    function toggleLanguage() {
        currentLang = currentLang === 'ar' ? 'en' : 'ar';
        document.documentElement.dir = currentLang === 'ar' ? 'rtl' : 'ltr';
        document.documentElement.lang = currentLang;

        const t = translations[currentLang];
        document.getElementById('app-title').innerText = t.appTitle;
        document.getElementById('custom-pdf-title').innerText = t.customPdfTitle;
        document.getElementById('custom-pdf-desc').innerText = t.customPdfDesc;
        document.getElementById('stages-heading').innerText = t.stagesHeading;
        document.getElementById('btn-primary').innerText = t.btnPrimary;
        document.getElementById('btn-intermediate').innerText = t.btnIntermediate;
        document.getElementById('btn-secondary').innerText = t.btnSecondary;
        document.getElementById('years-heading').innerText = t.yearsHeading;
        document.getElementById('terms-heading').innerText = t.termsHeading;
        document.getElementById('subjects-heading').innerText = t.subjectsHeading;
        document.getElementById('label-query').innerText = t.labelQuery;
        document.getElementById('search-btn').innerText = t.searchBtn;
        document.getElementById('back-stages-btn').innerText = t.backStagesBtn;
        document.getElementById('back-to-stages').innerText = t.backToStages;
        document.getElementById('back-to-years').innerText = t.backToYears;
        document.getElementById('back-to-terms').innerText = t.backToTerms;
        document.getElementById('back-to-subjects').innerText = t.backToSubjects;
        document.getElementById('back-to-dashboard').innerText = t.backToDashboard;
        document.getElementById('custom-upload-heading').innerText = t.customUploadHeading;
        document.getElementById('label-choose-file').innerText = t.labelChooseFile;
        document.getElementById('label-custom-query').innerText = t.labelCustomQuery;
        document.getElementById('custom-submit-btn').innerText = t.customSubmitBtn;
        document.getElementById('opt-solved').innerText = t.optSolved;
        document.getElementById('opt-unsolved').innerText = t.optUnsolved;
        document.getElementById('opt-videos').innerText = t.optVideos;
        document.getElementById('opt-ai').innerText = t.optAi;
        document.getElementById('label-page-num').innerText = t.labelPageNum;
        document.getElementById('label-answer-text').innerText = t.labelAnswerText;
        document.getElementById('label-page-image').innerText = t.labelPageImage;

        if (currentStage) selectStage(currentStage);
    }

    function selectStage(stage) {
        currentStage = stage;
        const yearsGrid = document.getElementById('years-grid');
        yearsGrid.innerHTML = '';
        
        subjectsData[stage].years.forEach(yr => {
            const label = stage === 'secondary' ? translations[currentLang][`yearSec${yr}`] : translations[currentLang][`year${yr}`];
            const btn = document.createElement('div');
            btn.className = 'btn-card';
            btn.innerText = label;
            btn.onclick = () => selectYear(yr);
            yearsGrid.appendChild(btn);
        });

        navigateTo('years-screen');
    }

    function selectYear(yr) {
        currentYear = yr;
        const termsGrid = document.getElementById('terms-grid');
        termsGrid.innerHTML = '';
        
        [1, 2].forEach(term => {
            const btn = document.createElement('div');
            btn.className = 'btn-card';
            btn.innerText = translations[currentLang][`term${term}`];
            btn.onclick = () => selectTerm(term);
            termsGrid.appendChild(btn);
        });

        navigateTo('terms-screen');
    }

    function selectTerm(term) {
        currentTerm = term;
        const subjectsGrid = document.getElementById('subjects-grid');
        subjectsGrid.innerHTML = '';
        const list = subjectsData[currentStage].subjects[currentLang];

        list.forEach(sub => {
            const btn = document.createElement('div');
            btn.className = 'btn-card';
            btn.innerText = sub;
            btn.onclick = () => selectSubject(sub);
            subjectsGrid.appendChild(btn);
        });

        navigateTo('subjects-screen');
    }

    function selectSubject(subjectName) {
        selectedSubject = subjectName;
        document.getElementById('current-subject-name').innerText = subjectName;
        navigateTo('subject-dashboard-screen');
    }

    function openSubjectOption(optionType) {
        if (optionType === 'ai') {
            document.getElementById('ai-book-title').innerText = selectedSubject;
            navigateTo('ai-search-screen');
        } else {
            alert(currentLang === 'ar' ? `جاري فتح قسم (${optionType}) لمادة ${selectedSubject}...` : `Opening ${optionType} for ${selectedSubject}...`);
        }
    }

    async function executeSearch() {
        const q = document.getElementById('search-query').value;
        if (!q) return alert(currentLang === 'ar' ? "يرجى كتابة السؤال أولاً" : "Please enter a question");

        const resBox = document.getElementById('result-container');
        resBox.classList.remove('hidden');
        document.getElementById('image-container').classList.add('hidden');
        
        document.getElementById('result-answer').innerHTML = `
            <div class="spinner-container">
                <div class="spinner"></div>
                <div style="color: var(--primary); font-weight: bold;">${currentLang === 'ar' ? 'جاري البحث والتحليل الذكي بالسيرفر...' : 'Analyzing document...'}</div>
            </div>
        `;

        try {
            const resp = await fetch(`/api/search?book_code=${selectedBookCode}&query=${encodeURIComponent(q)}`);
            const data = await resp.json();
            if (resp.ok) {
                document.getElementById('image-container').classList.remove('hidden');
                document.getElementById('result-page').innerText = data.page_number;
                
                let answerHtml = data.answer;
                if (data.highlight) {
                    answerHtml = answerHtml.replace(data.highlight, `<span class="answer-highlight">${data.highlight}</span>`);
                }
                document.getElementById('result-answer').innerHTML = answerHtml;
                document.getElementById('result-image').src = data.page_image;
            } else {
                document.getElementById('result-answer').innerText = data.error_ar || data.error;
            }
        } catch(e) {
            document.getElementById('result-answer').innerText = "حدث خطأ أثناء الاتصال بالسيرفر.";
        }
    }

    async function handleCustomPDFSubmit(e) {
        e.preventDefault();
        const fileInput = document.getElementById('custom-file-input');
        const queryInput = document.getElementById('custom-query-input');

        if (!fileInput.files[0]) return;

        const fileName = fileInput.files[0].name;
        const formData = new FormData();
        formData.append('file', fileInput.files[0]);
        formData.append('query', queryInput.value);

        const resBox = document.getElementById('result-container');
        resBox.classList.remove('hidden');
        document.getElementById('image-container').classList.add('hidden');
        
        document.getElementById('result-answer').innerHTML = `
            <div style="color: #059669; font-weight: bold; margin-bottom: 8px; text-align: center;">
                ✅ تم إرفاق المستند (${fileName}) بنجاح!
            </div>
            <div class="spinner-container">
                <div class="spinner"></div>
                <div style="color: var(--primary); font-weight: bold;">${currentLang === 'ar' ? 'جاري قراءة وتحليل ملف الـ PDF بالذكاء الاصطناعي...' : 'Processing custom PDF...'}</div>
            </div>
        `;

        try {
            const resp = await fetch('/api/custom-pdf-search', {
                method: 'POST',
                body: formData
            });
            const data = await resp.json();
            if (resp.ok) {
                document.getElementById('image-container').classList.remove('hidden');
                document.getElementById('result-page').innerText = data.page_number;
                
                let answerHtml = data.answer;
                document.getElementById('result-answer').innerHTML = answerHtml;
                document.getElementById('result-image').src = data.page_image;
            } else {
                document.getElementById('result-answer').innerText = data.error || "تعذر تحليل الملف.";
            }
        } catch(e) {
            document.getElementById('result-answer').innerText = "حدث خطأ في عملية رفع أو تحليل الملف.";
        }
    }
</script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=10000)
