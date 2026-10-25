import os
import io
import fitz  # PyMuPDF
import base64
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai

# إعداد مفتاح API الخاص بـ Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI(title="Smart Curriculum Educational Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BOOKS_DIR = os.path.join(os.path.dirname(__file__), "books")

def get_gemini_model():
    return genai.GenerativeModel('gemini-1.5-flash')

def search_pdf_text(pdf_path: str, query: str):
    doc = fitz.open(pdf_path)
    keywords = [k.strip() for k in query.split() if len(k.strip()) > 2]
    matched_pages = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        text = page.get_text("text")
        score = 0
        for kw in keywords:
            if kw.lower() in text.lower():
                score += 1
        if score > 0:
            matched_pages.append((page_idx, score))

    matched_pages.sort(key=lambda x: x[1], reverse=True)
    return [p[0] for p in matched_pages[:3]] if matched_pages else [0]

def render_page_with_highlight(doc: fitz.Document, page_num: int, highlight_text: str = "") -> str:
    page = doc[page_num]
    if highlight_text:
        text_instances = page.search_for(highlight_text)
        for inst in text_instances:
            page.draw_rect(inst, color=(1, 0, 0), fill=(1, 0.8, 0.8), overlay=True)

    pix = page.get_pixmap(dpi=150)
    img_bytes = pix.tobytes("png")
    encoded = base64.b64encode(img_bytes).decode('utf-8')
    return f"data:image/png;base64,{encoded}"

@app.get("/api/search")
def search_in_book(book_code: str = Query(...), query: str = Query(...)):
    filename = f"{book_code}.pdf"
    pdf_path = os.path.join(BOOKS_DIR, filename)

    if not os.path.exists(pdf_path):
        return JSONResponse(
            status_code=404,
            content={"error_ar": "عذراً، الكتاب غير متوفر حالياً في المكتبة الرقمية.",
                      "error_en": "Sorry, this book is currently not available in the library."}
        )

    try:
        doc = fitz.open(pdf_path)
        top_pages = search_pdf_text(pdf_path, query)
        best_page_idx = top_pages[0]
        page = doc[best_page_idx]

        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")

        model = get_gemini_model()
        prompt = (
            f"أنت معلم وخبير في المناهج الدراسية. بناءً على صفحة الكتاب المرفقة، أجب بدقة على السؤال التالي: '{query}'.\n"
            f"إذا وجدت الإجابة في الصفحة، حدد النص الدقيق المستخرج للإجابة في السطر الأخير مسبوقاً بـ 'EXACT_TEXT:'."
        )

        response = model.generate_content([
            prompt,
            {"mime_type": "image/png", "data": img_bytes}
        ])

        answer_text = response.text if response.text else "لم يتم العثور على إجابة واضحة."
        highlight_str = ""
        if "EXACT_TEXT:" in answer_text:
            parts = answer_text.split("EXACT_TEXT:")
            answer_text = parts[0].strip()
            highlight_str = parts[1].strip()

        img_b64 = render_page_with_highlight(doc, best_page_idx, highlight_str)

        return {
            "page_number": best_page_idx + 1,
            "answer": answer_text,
            "highlight": highlight_str,
            "page_image": img_b64
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/custom-pdf-search")
async def search_custom_pdf(file: UploadFile = File(...), query: str = Form(...)):
    if not file.filename.endswith('.pdf'):
        return JSONResponse(status_code=400, content={"error": "يجب أن يكون الملف بصيغة PDF فقط."})
    
    try:
        contents = await file.read()
        doc = fitz.open(stream=contents, filetype="pdf")
        
        keywords = [k.strip() for k in query.split() if len(k.strip()) > 2]
        best_page_idx = 0
        max_score = -1

        for page_idx in range(len(doc)):
            text = doc[page_idx].get_text("text")
            score = sum(1 for kw in keywords if kw.lower() in text.lower())
            if score > max_score:
                max_score = score
                best_page_idx = page_idx

        page = doc[best_page_idx]
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")

        model = get_gemini_model()
        prompt = (
            f"بناءً على صفحة المستند المرفقة، أجب على السؤال التالي: '{query}'.\n"
            f"في نهاية إجابتك أضف سطر جديد مكتوب فيه EXACT_TEXT: متبوعاً بالجملة أو النص الدقيق المحدد للإجابة من الصفحة."
        )

        response = model.generate_content([
            prompt,
            {"mime_type": "image/png", "data": img_bytes}
        ])

        answer_text = response.text if response.text else "تم تحليله بنجاح."
        highlight_str = ""
        if "EXACT_TEXT:" in answer_text:
            parts = answer_text.split("EXACT_TEXT:")
            answer_text = parts[0].strip()
            highlight_str = parts[1].strip()

        img_b64 = render_page_with_highlight(doc, best_page_idx, highlight_str)

        return {
            "page_number": best_page_idx + 1,
            "answer": answer_text,
            "page_image": img_b64
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

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
            --card-bg: rgba(255, 255, 255, 0.92);
            --primary: #4f46e5;
            --primary-hover: #4338ca;
            --secondary: #06b6d4;
            --accent-red: #ef4444;
            --text-dark: #1e293b;
        }

        body {
            font-family: 'Tajawal', sans-serif;
            background: var(--bg-gradient);
            min-height: 100vh;
            margin: 0;
            padding: 20px 10px;
            color: var(--text-dark);
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
            background: var(--card-bg);
            border-radius: 24px;
            padding: 30px 20px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            backdrop-filter: blur(10px);
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 15px;
            margin-bottom: 25px;
        }

        .title-area h1 {
            font-size: 1.8rem;
            color: #334155;
            margin: 0;
            font-weight: 900;
        }

        .lang-btn {
            background: #4f46e5;
            color: white;
            border: none;
            padding: 10px 18px;
            border-radius: 12px;
            cursor: pointer;
            font-weight: bold;
            font-family: 'Tajawal', sans-serif;
            transition: 0.3s;
        }
        .lang-btn:hover { background: #3730a3; }

        .custom-ai-banner {
            background: linear-gradient(135deg, #8b5cf6, #d946ef);
            color: white;
            padding: 20px;
            border-radius: 18px;
            text-align: center;
            margin-bottom: 25px;
            cursor: pointer;
            box-shadow: 0 10px 20px rgba(217, 70, 239, 0.3);
            transition: transform 0.2s;
        }
        .custom-ai-banner:hover { transform: scale(1.02); }
        .custom-ai-banner h2 { margin: 0 0 5px 0; font-size: 1.4rem; }

        .section-title {
            font-size: 1.3rem;
            font-weight: 700;
            color: #1e293b;
            margin-top: 20px;
            margin-bottom: 15px;
            border-right: 4px solid var(--primary);
            padding-right: 10px;
        }
        body[dir="ltr"] .section-title {
            border-right: none;
            border-left: 4px solid var(--primary);
            padding-right: 0;
            padding-left: 10px;
        }

        .grid-buttons {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 12px;
            margin-bottom: 20px;
        }

        .btn-card {
            background: #ffffff;
            border: 2px solid #e2e8f0;
            padding: 15px;
            border-radius: 14px;
            text-align: center;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.2s;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);
        }
        .btn-card:hover {
            border-color: var(--primary);
            color: var(--primary);
            transform: translateY(-2px);
        }

        .hidden { display: none !important; }

        .input-group {
            margin-bottom: 15px;
        }
        .input-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: bold;
        }
        .input-group input[type="text"], .input-group input[type="file"] {
            width: 100%;
            padding: 14px;
            border: 2px solid #cbd5e1;
            border-radius: 12px;
            font-family: inherit;
            box-sizing: border-box;
            font-size: 1rem;
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
            box-shadow: 0 10px 15px -3px rgba(79, 70, 229, 0.4);
        }
        .action-btn:hover { background: var(--primary-hover); }

        .result-box {
            margin-top: 25px;
            padding: 20px;
            background: #f8fafc;
            border-radius: 16px;
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
            border-radius: 12px;
            border: 2px solid #cbd5e1;
            margin-top: 15px;
        }

        /* تنسيق دائرة التحميل (Spinner) */
        .spinner-container {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .spinner {
            width: 45px;
            height: 45px;
            border: 5px solid #e2e8f0;
            border-top: 5px solid var(--primary);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-bottom: 12px;
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

    <!-- زر الذكاء الاصطناعي لكتابك الخاص -->
    <div class="custom-ai-banner" onclick="showCustomPDFScreen()">
        <h2 id="custom-pdf-title">🤖 الذكاء الاصطناعي لكتابك الخاص</h2>
        <span id="custom-pdf-desc">اضغط هنا لرفع أي كتاب PDF واسأل الذكاء الاصطناعي عنه مباشرة!</span>
    </div>

    <!-- شاشة الرفع الخاص -->
    <div id="custom-pdf-screen" class="hidden">
        <button class="btn-card" style="margin-bottom: 15px;" onclick="goBackToHome()" id="back-home-btn">← العودة للمراحل الدراسية</button>
        <h3 id="custom-upload-heading">ارفق كتابك الخاص (PDF) ووجّه سؤالك:</h3>
        <form id="custom-pdf-form" onsubmit="handleCustomPDFSubmit(event)">
            <div class="input-group">
                <label id="label-choose-file">اختر ملف الـ PDF:</label>
                <input type="file" id="custom-file-input" accept="application/pdf" required>
            </div>
            <div class="input-group">
                <label id="label-custom-query">اكتب سؤالك بوضوح تام:</label>
                <input type="text" id="custom-query-input" placeholder="مثال: ما هو تعريف الحركة الموجية؟" required>
            </div>
            <button type="submit" class="action-btn" id="custom-submit-btn">تحليل المستند واستخراج الإجابة 🔍</button>
        </form>
    </div>

    <!-- الشاشة الرئيسية للمراحل -->
    <div id="main-curriculum-screen">
        <div class="section-title" id="stages-heading">اختر المرحلة الدراسية:</div>
        <div class="grid-buttons">
            <div class="btn-card" onclick="selectStage('primary')" id="btn-primary">🎒 المرحلة الابتدائية</div>
            <div class="btn-card" onclick="selectStage('intermediate')" id="btn-intermediate">📚 المرحلة المتوسطة</div>
            <div class="btn-card" onclick="selectStage('secondary')" id="btn-secondary">🎓 المرحلة الثانوية (مسارات)</div>
        </div>

        <div id="years-section" class="hidden">
            <div class="section-title" id="years-heading">اختر السنة الدراسية:</div>
            <div class="grid-buttons" id="years-grid"></div>
        </div>

        <div id="terms-section" class="hidden">
            <div class="section-title" id="terms-heading">اختر الفصل الدراسي:</div>
            <div class="grid-buttons" id="terms-grid"></div>
        </div>

        <div id="subjects-section" class="hidden">
            <div class="section-title" id="subjects-heading">المواد الدراسية المتاحة:</div>
            <div class="grid-buttons" id="subjects-grid"></div>
        </div>

        <!-- واجهة البحث داخل كتاب المنهج -->
        <div id="search-section" class="hidden">
            <button class="btn-card" style="margin-bottom: 15px;" onclick="resetSelection()" id="back-select-btn">← رجوع لاختيار المادة</button>
            <div class="section-title" id="search-heading">🤖 البحث الذكي الفائق في كتاب: <span id="selected-book-name" style="color: var(--primary);"></span></div>
            
            <div class="input-group">
                <label id="label-query">اكتب سؤالك بوضوح تام:</label>
                <input type="text" id="search-query" placeholder="مثال: ما هو قانون الجذب الكوني؟" required>
            </div>
            <button class="action-btn" onclick="executeSearch()" id="search-btn">بحث ذكي وتحليل دقيق 🔍</button>
        </div>
    </div>

    <!-- عرض النتائج -->
    <div id="result-container" class="result-box hidden">
        <h3 id="result-heading" style="color: var(--primary); margin-top:0;">🎯 النتيجة والتحليل الذكي:</h3>
        <p><strong>رقم الصفحة:</strong> <span id="result-page"></span></p>
        <p><strong>الإجابة:</strong> <span id="result-answer"></span></p>
        <div id="image-container">
            <h4>الصفحة المحددة من الكتاب:</h4>
            <img id="result-image" class="page-preview" src="" alt="صفحة الكتاب">
        </div>
    </div>
</div>

<script>
    let currentLang = 'ar';
    let currentStage = '';
    let currentYear = '';
    let currentTerm = '';
    let selectedBookCode = '';

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
            searchHeading: "🤖 البحث الذكي الفائق في كتاب: ",
            labelQuery: "اكتب سؤالك بوضوح تام:",
            searchBtn: "بحث ذكي وتحليل دقيق 🔍",
            backSelectBtn: "← رجوع لاختيار المادة",
            backHomeBtn: "← العودة للمراحل الدراسية",
            customUploadHeading: "ارفق كتابك الخاص (PDF) ووجّه سؤالك:",
            labelChooseFile: "اختر ملف الـ PDF:",
            labelCustomQuery: "اكتب سؤالك بوضوح تام:",
            customSubmitBtn: "تحليل المستند واستخراج الإجابة 🔍",
            term1: "الفصل الدراسي الأول",
            term2: "الفصل الدراسي الثاني",
            term3: "الفصل الدراسي الثالث",
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
            searchHeading: "🤖 AI Search in Book: ",
            labelQuery: "Write your question clearly:",
            searchBtn: "Smart Search & Analyze 🔍",
            backSelectBtn: "← Back to Subject Selection",
            backHomeBtn: "← Back to Educational Stages",
            customUploadHeading: "Upload your custom PDF book & ask a question:",
            labelChooseFile: "Select PDF File:",
            labelCustomQuery: "Write your question clearly:",
            customSubmitBtn: "Analyze Document & Extract Answer 🔍",
            term1: "First Term",
            term2: "Second Term",
            term3: "Third Term",
            year1: "Grade 1", year2: "Grade 2", year3: "Grade 3",
            year4: "Grade 4", year5: "Grade 5", year6: "Grade 6",
            yearSec1: "First Common Year", yearSec2: "Second Year", yearSec3: "Third Year"
        }
    };

    const subjectsData = {
        primary: {
            years: [1, 2, 3, 4, 5, 6],
            subjects: {
                ar: ["القرآن الكريم", "التربية الإسلامية", "لغتي", "الرياضيات", "العلوم", "الدراسات الاجتماعية", "المهارات الرقمية", "التربية الفنية", "التربية البدنية"],
                en: ["Holy Quran", "Islamic Studies", "My Language (Arabic)", "Mathematics", "Science", "Social Studies", "Digital Skills", "Art Education", "Physical Education"]
            }
        },
        intermediate: {
            years: [1, 2, 3],
            subjects: {
                ar: ["القرآن والتفسير", "الحديث والسيرة", "التوحيد والفقه", "لغتي الخالدة", "اللغة الإنجليزية", "الرياضيات", "العلوم", "الدراسات الاجتماعية", "المهارات الرقمية", "التربية الفنية"],
                en: ["Quran & Tafseer", "Hadith & Seerah", "Islamic Faith & Fiqh", "Arabic Language", "English Language", "Mathematics", "Science", "Social Studies", "Digital Skills", "Art Education"]
            }
        },
        secondary: {
            years: [1, 2, 3],
            subjects: {
                ar: ["الفيزياء 2", "الكيمياء", "الأحياء", "الرياضيات 2", "التقنية الرقمية", "الدراسات الاجتماعية", "اللغة الإنجليزية", "التفكير النقدي", "اللياقة والثقافة الصحية"],
                en: ["Physics 2", "Chemistry", "Biology", "Mathematics 2", "Digital Technology", "Social Studies", "English Language", "Critical Thinking", "Fitness & Health Culture"]
            }
        }
    };

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
        document.getElementById('back-select-btn').innerText = t.backSelectBtn;
        document.getElementById('back-home-btn').innerText = t.backHomeBtn;
        document.getElementById('custom-upload-heading').innerText = t.customUploadHeading;
        document.getElementById('label-choose-file').innerText = t.labelChooseFile;
        document.getElementById('label-custom-query').innerText = t.labelCustomQuery;
        document.getElementById('custom-submit-btn').innerText = t.customSubmitBtn;

        if (currentStage) selectStage(currentStage);
    }

    function selectStage(stage) {
        currentStage = stage;
        document.getElementById('years-section').classList.remove('hidden');
        document.getElementById('terms-section').classList.add('hidden');
        document.getElementById('subjects-section').classList.add('hidden');
        document.getElementById('search-section').classList.add('hidden');

        const yearsGrid = document.getElementById('years-grid');
        yearsGrid.innerHTML = '';
        
        const yearsList = subjectsData[stage].years;
        yearsList.forEach(yr => {
            let label = "";
            if (stage === 'secondary') {
                label = translations[currentLang][`yearSec${yr}`];
            } else {
                label = translations[currentLang][`year${yr}`];
            }
            const btn = document.createElement('div');
            btn.className = 'btn-card';
            btn.innerText = label;
            btn.onclick = () => selectYear(yr);
            yearsGrid.appendChild(btn);
        });
    }

    function selectYear(yr) {
        currentYear = yr;
        document.getElementById('terms-section').classList.remove('hidden');
        document.getElementById('subjects-section').classList.add('hidden');
        
        const termsGrid = document.getElementById('terms-grid');
        termsGrid.innerHTML = '';
        [1, 2, 3].forEach(term => {
            const btn = document.createElement('div');
            btn.className = 'btn-card';
            btn.innerText = translations[currentLang][`term${term}`];
            btn.onclick = () => selectTerm(term);
            termsGrid.appendChild(btn);
        });
    }

    function selectTerm(term) {
        currentTerm = term;
        document.getElementById('subjects-section').classList.remove('hidden');
        
        const subjectsGrid = document.getElementById('subjects-grid');
        subjectsGrid.innerHTML = '';
        const list = subjectsData[currentStage].subjects[currentLang];

        list.forEach((sub, idx) => {
            const btn = document.createElement('div');
            btn.className = 'btn-card';
            btn.innerText = sub;
            btn.onclick = () => selectBook(sub, `physics2_t1`);
            subjectsGrid.appendChild(btn);
        });
    }

    function selectBook(bookName, code) {
        selectedBookCode = code;
        document.getElementById('selected-book-name').innerText = bookName;
        document.getElementById('search-section').classList.remove('hidden');
    }

    function resetSelection() {
        document.getElementById('search-section').classList.add('hidden');
        document.getElementById('result-container').classList.add('hidden');
    }

    function showCustomPDFScreen() {
        document.getElementById('main-curriculum-screen').classList.add('hidden');
        document.getElementById('custom-pdf-screen').classList.remove('hidden');
        document.getElementById('result-container').classList.add('hidden');
    }

    function goBackToHome() {
        document.getElementById('custom-pdf-screen').classList.add('hidden');
        document.getElementById('main-curriculum-screen').classList.remove('hidden');
        document.getElementById('result-container').classList.add('hidden');
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
                <div style="color: var(--primary); font-weight: bold;">${currentLang === 'ar' ? 'جاري البحث والتحليل الذكي...' : 'Analyzing...'}</div>
            </div>
        `;

        try {
            const resp = await fetch(`/api/search?book_code=${selectedBookCode}&query=${encodeURIComponent(q)}`);
            const data = await resp.json();
            if (resp.ok) {
                document.getElementById('image-container').classList.remove('hidden');
                document.getElementById('result-page').innerText = data.page_number;
                document.getElementById('result-answer').innerHTML = data.answer.replace(data.highlight, `<span class="answer-highlight">${data.highlight}</span>`);
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
        
        // عرض دائرة التحميل المتحركة مع رسالة نجاح الإرفاق
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
                document.getElementById('result-answer').innerText = data.answer;
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
