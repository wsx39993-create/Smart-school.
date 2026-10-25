import os
from pypdf import PdfReader
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# التأكد من وجود مجلد الكتب
os.makedirs("books", exist_ok=True)

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>منصة المنهج الذكي | Smart Curriculum Platform</title>
    <style>
        :root {
            --primary: #2563eb;
            --primary-hover: #1d4ed8;
            --bg-color: #f8fafc;
            --card-bg: #ffffff;
            --text-main: #1e293b;
            --text-muted: #64748b;
            --border-color: #e2e8f0;
            --accent-red: #ef4444;
            --accent-green: #10b981;
        }

        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 20px;
            line-height: 1.6;
        }

        .container {
            max-width: 950px;
            margin: 0 auto;
            background: var(--card-bg);
            padding: 40px;
            border-radius: 20px;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.05), 0 8px 10px -6px rgba(0, 0, 0, 0.05);
            position: relative;
        }

        .header-flex {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid var(--border-color);
            padding-bottom: 20px;
            margin-bottom: 30px;
        }

        h2 {
            color: var(--primary);
            margin: 0;
            font-size: 26px;
        }

        .lang-btn {
            background: #f1f5f9;
            color: var(--text-muted);
            border: 1px solid var(--border-color);
            padding: 8px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            transition: all 0.2s;
        }

        .lang-btn:hover {
            background: var(--border-color);
            color: var(--text-main);
        }

        .section-title {
            font-size: 20px;
            font-weight: 700;
            margin: 30px 0 15px 0;
            color: var(--text-main);
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .section-title::before {
            content: '';
            width: 6px;
            height: 22px;
            background: var(--primary);
            border-radius: 4px;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 15px;
            margin-bottom: 25px;
        }

        .card {
            background: #ffffff;
            border: 2px solid var(--border-color);
            border-radius: 14px;
            padding: 18px;
            text-align: center;
            cursor: pointer;
            transition: all 0.25s ease;
            font-weight: 600;
            color: var(--text-main);
            box-shadow: 0 2px 4px rgba(0,0,0,0.02);
        }

        .card:hover {
            border-color: var(--primary);
            background: #eff6ff;
            color: var(--primary);
            transform: translateY(-3px);
            box-shadow: 0 6px 15px rgba(37, 99, 235, 0.1);
        }

        .hidden {
            display: none !important;
        }

        .form-group {
            margin-bottom: 20px;
        }

        label {
            font-weight: 600;
            display: block;
            margin-bottom: 8px;
            color: var(--text-main);
        }

        input[type="text"], input[type="number"] {
            width: 100%;
            padding: 14px;
            border: 2px solid var(--border-color);
            border-radius: 10px;
            box-sizing: border-box;
            font-size: 15px;
            background: #f8fafc;
        }

        button.action-btn {
            width: 100%;
            padding: 14px;
            background-color: var(--primary);
            color: white;
            border: none;
            border-radius: 10px;
            font-weight: bold;
            cursor: pointer;
            font-size: 16px;
            transition: background-color 0.2s;
        }

        button.action-btn:hover {
            background-color: var(--primary-hover);
        }

        .back-btn {
            background-color: #64748b !important;
            margin-top: 10px;
        }
        .back-btn:hover {
            background-color: #475569 !important;
        }

        /* شاشة التحميل العصرية */
        #loadingOverlay {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(255, 255, 255, 0.85);
            backdrop-filter: blur(4px);
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            z-index: 1000;
            border-radius: 20px;
            display: none;
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

        .loading-text {
            font-weight: 700;
            color: var(--primary);
            font-size: 18px;
        }

        /* صندوق النتائج مع الإطار الأحمر الاحترافي */
        .result-box {
            margin-top: 30px;
            padding: 30px;
            background: #fff;
            border-radius: 16px;
            border: 4px solid var(--accent-red);
            box-shadow: 0 10px 30px rgba(239, 68, 68, 0.15);
            display: none;
            position: relative;
        }

        .result-box::before {
            content: 'الإجابة الدقيقة محددة في الإطار الأحمر أدناه';
            position: absolute;
            top: -16px;
            right: 20px;
            background: var(--accent-red);
            color: white;
            padding: 4px 14px;
            font-size: 13px;
            font-weight: bold;
            border-radius: 6px;
        }

        .ai-section {
            margin-top: 35px;
            border-top: 2px dashed var(--border-color);
            padding-top: 30px;
        }
    </style>
</head>
<body>
<div class="container">
    <div id="loadingOverlay">
        <div class="spinner"></div>
        <div class="loading-text" id="loadingMessage">جاري التحليل الذكي...</div>
    </div>

    <div class="header-flex">
        <h2 id="mainHeading">🎓 منصة المنهج الذكي التعليمية</h2>
        <button class="lang-btn" onclick="toggleLanguage()">English / عربي</button>
    </div>
    
    <!-- الخطوة 1: اختيار الصف -->
    <div id="stepGrade">
        <div class="section-title">المرحلة الابتدائية:</div>
        <div class="grid">
            <div class="card" onclick="selectGrade('أولى ابتدائي')">🎒 أولى ابتدائي</div>
            <div class="card" onclick="selectGrade('ثاني ابتدائي')">🎒 ثاني ابتدائي</div>
            <div class="card" onclick="selectGrade('ثالث ابتدائي')">🎒 ثالث ابتدائي</div>
            <div class="card" onclick="selectGrade('رابع ابتدائي')">🎒 رابع ابتدائي</div>
            <div class="card" onclick="selectGrade('خامس ابتدائي')">🎒 خامس ابتدائي</div>
            <div class="card" onclick="selectGrade('سادس ابتدائي')">🎒 سادس ابتدائي</div>
        </div>

        <div class="section-title">المرحلة المتوسطة:</div>
        <div class="grid">
            <div class="card" onclick="selectGrade('أولى متوسط')">📚 أولى متوسط</div>
            <div class="card" onclick="selectGrade('ثاني متوسط')">📚 ثاني متوسط</div>
            <div class="card" onclick="selectGrade('ثالث متوسط')">📚 ثالث متوسط</div>
        </div>

        <div class="section-title">المرحلة الثانوية:</div>
        <div class="grid">
            <div class="card" onclick="selectGrade('أولى ثانوي')">🎓 أولى ثانوي</div>
            <div class="card" onclick="selectGrade('ثاني ثانوي (مسارات)')">🎓 ثاني ثانوي (مسارات)</div>
            <div class="card" onclick="select ثانوي')">🎓 ثالث ثانوي</div>
        </div>
    </div>

    <!-- الخطوة 2: اختيار الفصل -->
    <div id="stepSemester" class="hidden">
        <div class="section-title" id="semesterTitle">اختر الفصل الدراسي:</div>
        <div class="grid">
            <div class="card" onclick="selectSemester('الفصل الدراسي الأول')">📅 الفصل الدراسي الأول</div>
            <div class="card" onclick="selectSemester('الفصل الدراسي الثاني')">📅 الفصل الدراسي الثاني</div>
        </div>
        <button class="action-btn back-btn" onclick="goBackToGrade()">⬅ رجوع لاختيار الصف</button>
    </div>

    <!-- الخطوة 3: اختيار المادة -->
    <div id="stepSubject" class="hidden">
        <div class="section-title" id="selectedGradeTitle">مواد الفصل الدراسي:</div>
        <div class="grid" id="subjectsGrid"></div>
        <button class="action-btn back-btn" onclick="goBackToSemester()">⬅ رجوع لاختيار الفصل</button>
    </div>

    <!-- الخطوة 4: الخيارات الأربعة -->
    <div id="stepOptions" class="hidden">
        <div class="section-title" id="selectedTitle">خيارات الكتاب:</div>
        <div class="grid">
            <div class="card" onclick="openBook('clean')">📖 الكتاب بدون حل (النظيف)</div>
            <div class="card" onclick="openBook('solved')" style="background: #ecfdf5; border-color: #10b981; color: #047857;">📝 الكتاب محلول (كاملاً)</div>
            <div class="card" onclick="openAinLessons()" style="background: #eff6ff; border-color: #3b82f6; color: #1d4ed8;">👨‍🏫 شرح الدروس (قنوات عين)</div>
            <div class="card" onclick="showAiSearch()" style="background: #e0f2fe; border-color: #38bdf8; color: #0369a1;">🤖 المساعد الذكي للبحث الفوري</div>
        </div>
        <button class="action-btn back-btn" onclick="goBackToSubject()">⬅ رجوع لاختيار المادة</button>
    </div>

    <!-- الخطوة 5: البحث الذكي الفائق مع الإطار الأحمر -->
    <div id="aiSection" class="hidden ai-section">
        <h3 style="color: var(--primary); margin-top: 0;" id="aiSearchTitle">🤖 البحث الذكي الفائق في الكتاب</h3>
        
        <div class="form-group">
            <label style="color: #10b981; font-weight: bold;">✔ نظام الذكاء الاصطناعي متصل وجاهز للتحليل الدقيق</label>
        </div>

        <div class="form-group">
            <label>رقم صفحة الكتاب الفعلية (فارق الأصفست عن ملف PDF، وإلا اتركه 0):</label>
            <input type="number" id="pageOffset" value="0" placeholder="مثال: 0 أو 2">
        </div>

        <div class="form-group">
            <label>اكتب سؤالك بوضوح تام:</label>
            <input type="text" id="query" placeholder="مثال: ما هو قانون الجذب الكوني؟">
        </div>

        <button class="action-btn" onclick="searchBook()">بحث ذكي وتحليل دقيق 🔍</button>

        <div id="resultBox" class="result-box">
            <p id="answerText" style="font-size: 17px; font-weight: 600; line-height: 1.9; color: #1e293b; background: #f8fafc; padding: 18px; border-radius: 10px; border-right: 5px solid var(--accent-red);"></p>
            <div style="margin-top: 15px; display: flex; justify-content: space-between; align-items: center;">
                <span id="pageText" style="font-weight: bold; color: #dc2626; font-size: 15px;"></span>
                <a id="viewFullPageBtn" href="#" target="_blank" style="background: var(--primary); color: white; padding: 8px 16px; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 14px;">📄 فتح الصفحة كاملة في الكتاب</a>
            </div>
        </div>
    </div>
</div>

<script>
let currentGrade = "";
let currentSemester = "";
let currentSubject = "";
let currentCleanFile = "";
let currentSolvedFile = "";
let currentAinLink = "";

const curriculumData = {
    "ثاني ثانوي (مسارات)": {
        "الفصل الدراسي الأول": [
            { name: "الرياضيات 3", clean: "math3_t1.pdf", solved: "math3_t1_solved.pdf", ain: "https://ien.edu.sa/" },
            { name: "الفيزياء 2", clean: "physics2_t1.pdf", solved: "physics2_t1_solved.pdf", ain: "https://ien.edu.sa/" },
            { name: "الكيمياء 2", clean: "chemistry2_t1.pdf", solved: "chemistry2_t1_solved.pdf", ain: "https://ien.edu.sa/" },
            { name: "الأحياء 2", clean: "biology2_t1.pdf", solved: "biology2_t1_solved.pdf", ain: "https://ien.edu.sa/" }
        ],
        "الفصل الدراسي الثاني": [
            { name: "الرياضيات 4", clean: "math4_t2.pdf", solved: "math4_t2_solved.pdf", ain: "https://ien.edu.sa/" },
            { name: "الفيزياء 3", clean: "physics3_t2.pdf", solved: "physics3_t2_solved.pdf", ain: "https://ien.edu.sa/" }
        ]
    },
    "ثالث ثانوي": {
        "الفصل الدراسي الأول": [
            { name: "الرياضيات 5", clean: "math5_t1.pdf", solved: "math5_t1_solved.pdf", ain: "https://ien.edu.sa/" },
            { name: "الفيزياء 4", clean: "physics4_t1.pdf", solved: "physics4_t1_solved.pdf", ain: "https://ien.edu.sa/" }
        ],
        "الفصل الدراسي الثاني": [
            { name: "الرياضيات 6", clean: "math6_t2.pdf", solved: "math6_t2_solved.pdf", ain: "https://ien.edu.sa/" },
            { name: "الفيزياء 5", clean: "physics5_t2.pdf", solved: "physics5_t2_solved.pdf", ain: "https://ien.edu.sa/" }
        ]
    }
};

function showLoading(message) {
    document.getElementById('loadingMessage').innerText = message;
    document.getElementById('loadingOverlay').style.display = 'flex';
}

function hideLoading() {
    document.getElementById('loadingOverlay').style.display = 'none';
}

function selectGrade(grade) {
    currentGrade = grade;
    document.getElementById('semesterTitle').innerText = `اختر الفصل الدراسي لـ (${grade}):`;
    document.getElementById('stepGrade').classList.add('hidden');
    document.getElementById('stepSemester').classList.remove('hidden');
}

function selectSemester(semester) {
    currentSemester = semester;
    document.getElementById('selectedGradeTitle').innerText = `مقررات (${currentGrade} - ${semester}):`;
    
    const grid = document.getElementById('subjectsGrid');
    grid.innerHTML = "";
    
    const subjects = curriculumData[currentGrade]?.[semester] || [
        { name: "رياضيات عام", clean: "general_math.pdf", solved: "general_math_solved.pdf", ain: "https://ien.edu.sa/" }
    ];
    
    subjects.forEach(sub => {
        const card = document.createElement('div');
        card.className = "card";
        card.innerText = "📖 " + sub.name;
        card.onclick = () => selectSubject(sub.name, sub.clean, sub.solved, sub.ain);
        grid.appendChild(card);
    });

    document.getElementById('stepSemester').classList.add('hidden');
    document.getElementById('stepSubject').classList.remove('hidden');
}

function selectSubject(subjectName, cleanFile, solvedFile, ainLink) {
    currentSubject = subjectName;
    currentCleanFile = cleanFile;
    currentSolvedFile = solvedFile;
    currentAinLink = ainLink;
    document.getElementById('selectedTitle').innerText = `الصف: ${currentGrade} | ${currentSemester} | المادة: ${subjectName}`;
    document.getElementById('stepSubject').classList.add('hidden');
    document.getElementById('stepOptions').classList.remove('hidden');
}

function openBook(type) {
    let fileName = (type === 'solved') ? currentSolvedFile : currentCleanFile;
    showLoading(type === 'solved' ? 'جاري تجهيز الكتاب المحلول...' : 'جاري فتح الكتاب...');
    setTimeout(() => {
        hideLoading();
        window.open(`/view-book/${fileName}`, '_blank');
    }, 600);
}

function openAinLessons() {
    showLoading('جاري فتح شروحات قنوات عين...');
    setTimeout(() => {
        hideLoading();
        window.open(currentAinLink, '_blank');
    }, 500);
}

function goBackToGrade() {
    document.getElementById('stepSemester').classList.add('hidden');
    document.getElementById('stepGrade').classList.remove('hidden');
}

function goBackToSemester() {
    document.getElementById('stepSubject').classList.add('hidden');
    document.getElementById('stepSemester').classList.remove('hidden');
}

function goBackToSubject() {
    document.getElementById('stepOptions').classList.add('hidden');
    document.getElementById('stepSubject').classList.remove('hidden');
    document.getElementById('aiSection').classList.add('hidden');
}

function showAiSearch() {
    document.getElementById('aiSection').classList.remove('hidden');
    document.getElementById('aiSearchTitle').innerText = `🤖 البحث الذكي الفائق في كتاب: ${currentSubject}`;
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}

async function searchBook() {
    const query = document.getElementById('query').value;
    const offset = parseInt(document.getElementById('pageOffset').value) || 0;
    
    if (!query) { alert('الرجاء إدخال سؤالك بدقة'); return; }

    showLoading('جاري تحليل محتوى الكتاب واستخراج الإجابة الدقيقة...');

    try {
        const res = await fetch(`/search-book/?book_name=${encodeURIComponent(currentCleanFile)}&query=${encodeURIComponent(query)}&offset=${offset}`, { method: 'POST' });
        const data = await res.json();
        
        hideLoading();
        const resultBox = document.getElementById('resultBox');
        resultBox.style.display = 'block';
        
        if(data.error) {
            document.getElementById('answerText').innerText = data.error;
            document.getElementById('pageText').innerText = "";
            document.getElementById('viewFullPageBtn').style.display = 'none';
        } else {
            document.getElementById('answerText').innerText = "📌 " + data.extracted_text;
            document.getElementById('pageText').innerText = `📄 رقم الصفحة الفعلية في الكتاب: ${data.page_number}`;
            // توجيه الطالب مباشرة لفتح الصفحة المعنية داخل الكتاب
            document.getElementById('viewFullPageBtn.style.display = 'inline-block';
            document.getElementById('viewFullPageBtn').href = `/view-book/${currentCleanFile}#page=${data.page_number}`;
        }
    } catch (err) {
        hideLoading();
        alert('حدث خطأ أثناء الاتصال بالخادم.');
    }
}

function toggleLanguage() {
    const html = document.documentElement;
    if (html.getAttribute('dir') === 'rtl') {
        html.setAttribute('dir', 'ltr');
        html.setAttribute('lang', 'en');
        document.getElementById('mainHeading').innerText = "🎓 Smart Curriculum Educational Platform";
    } else {
        html.setAttribute('dir', 'rtl');
        html.setAttribute('lang', 'ar');
        document.getElementById('mainHeading').innerText = "🎓 منصة المنهج الذكي التعليمية";
    }
}
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_CONTENT

@app.get("/view-book/{book_name}")
async def view_book(book_name: str):
    file_path = f"books/{book_name}"
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="application/pdf", filename=book_name)
    return HTMLResponse("<h3>عذراً، ملف الكتاب غير متوفر في السيرفر حالياً.</h3>", status_code=404)

@app.post("/search-book/")
async def search_book(book_name: str, query: str, offset: int = 0):
    pdf_path = f"books/{book_name}"
    if not os.path.exists(pdf_path):
        return {"error": f"عذراً، كتاب ({book_name}) غير مرفوع حالياً في السيرفر."}
    
    reader = PdfReader(pdf_path)
    matched_text = ""
    found_page = 1
    
    # خوارزمية ذكية لتحليل الكلمات المفتاحية ومطابقتها بأعلى دقة
    keywords = [kw.strip() for kw in query.split() if len(kw.strip()) > 2]
    if not keywords:
        keywords = query.split()

    best_match_score = 0
    
    for page_num, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        score = sum(1 for kw in keywords if kw in text)
        if score > best_match_score:
            best_match_score = score
            found_page = (page_num + 1) + offset
            # استخراج النص الأقرب للإجابة بدقة
            lines = text.split('\n')
            relevant_lines = [line for line in lines if any(kw in line for kw in keywords)]
            if relevant_lines:
                matched_text = " ".join(relevant_lines[:5])
            else:
                matched_text = text[:500].replace("\n", " ")
            
    if best_match_score == 0 or not matched_text:
        return {"error": "لم يتم العثور على مطابقة دقيقة لهذا السؤال داخل صفحات الكتاب."}

    return {
        "page_number": found_page,
        "extracted_text": matched_text
    }
