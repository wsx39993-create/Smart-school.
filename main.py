import os
from pypdf import PdfReader
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse

app = FastAPI()

os.makedirs("books", exist_ok=True)

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>منصة المنهج الذكي | Smart Curriculum Platform</title>
    <style>
        body { font-family: Tahoma, sans-serif; background: #f8fafc; padding: 20px; color: #334155; margin: 0; }
        .container { max-width: 900px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }
        h2 { color: #0284c7; text-align: center; margin-bottom: 25px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
        .card { background: #f1f5f9; border: 2px solid #e2e8f0; border-radius: 12px; padding: 20px; text-align: center; cursor: pointer; transition: all 0.3s ease; font-weight: bold; color: #1e293b; }
        .card:hover { background: #0284c7; color: white; border-color: #0284c7; transform: translateY(-3px); }
        .section-title { font-size: 18px; font-weight: bold; margin: 20px 0 10px 0; color: #0f172a; border-right: 4px solid #0284c7; padding-right: 8px; }
        .hidden { display: none; }
        input[type="text"], input[type="file"], input[type="number"] { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 8px; box-sizing: border-box; margin-bottom: 15px; }
        button { width: 100%; padding: 12px; background-color: #10b981; color: white; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; font-size: 16px; }
        button:hover { background-color: #059669; }
        
        /* صندوق النتائج مع إطار أحمر وبارز */
        .result-box { 
            margin-top: 25px; 
            padding: 20px; 
            background: #fef2f2; 
            border-radius: 12px; 
            border: 3px solid #ef4444; 
            box-shadow: 0 4px 12px rgba(239, 68, 68, 0.15);
            display: none; 
        }
        .result-box h4 { color: #dc2626; margin-top: 0; }
        
        .lang-switch { text-align: left; margin-bottom: 15px; }
        .lang-btn { background: #64748b; padding: 5px 15px; font-size: 14px; width: auto; display: inline-block; }
    </style>
</head>
<body>
<div class="container">
    <div class="lang-switch">
        <button class="lang-btn" onclick="toggleLanguage()">English / عربي</button>
    </div>
    <h2 id="mainHeading">🎓 منصة المنهج الذكي التعليمية</h2>
    
    <!-- الخطوة 1: اختيار المرحلة -->
    <div id="step1">
        <div class="section-title" id="t1">اختر مرحلتك الدراسية:</div>
        <div class="grid">
            <div class="card" onclick="selectStage('اولى ابتدائي / Primary 1')">أولى ابتدائي / Primary 1</div>
            <div class="card" onclick="selectStage('ثاني ابتدائي / Primary 2')">ثاني ابتدائي / Primary 2</div>
            <div class="card" onclick="selectStage('ثالث متوسط / Intermediate 3')">ثالث متوسط / Intermediate 3</div>
            <div class="card" onclick="selectStage('ثاني ثانوي (مسارات) / Secondary 2')">ثاني ثانوي (مسارات) / Secondary 2</div>
        </div>
    </div>

    <!-- الخطوة 2: اختيار المادة -->
    <div id="step2" class="hidden">
        <div class="section-title" id="t2">اختر المادة الدراسية:</div>
        <div class="grid">
            <div class="card" onclick="selectSubject('رياضيات / Math')">📐 رياضيات / Math</div>
            <div class="card" onclick="selectSubject('علوم وفزياء / Science & Physics')">🧪 علوم وفزياء / Science & Physics</div>
            <div class="card" onclick="selectSubject('لغتي وإنجليزي / Languages')">📚 لغتي وإنجليزي / Languages</div>
        </div>
        <button onclick="goBack(1)" style="background: #64748b; margin-top: 10px;">⬅ رجوع / Back</button>
    </div>

    <!-- الخطوة 3: الخدمات الأربع -->
    <div id="step3" class="hidden">
        <div class="section-title" id="selectedTitle">خيارات الكتاب:</div>
        <div class="grid">
            <div class="card" onclick="alert('جاري فتح الكتاب بدون حل...')">📖 الكتاب بدون حل / Clean Book</div>
            <div class="card" onclick="alert('جاري فتح الكتاب محلول...')">📝 الكتاب محلول / Answered Book</div>
            <div class="card" onclick="alert('جاري فتح شرح الدروس...')">👨‍🏫 شرح الدروس / Lessons Explanations</div>
            <div class="card" onclick="showAiSearch()" style="background: #e0f2fe; border-color: #7dd3fc;">🤖 الكتاب بمساعدة الذكاء الاصطناعي / AI Textbook Assistant</div>
        </div>
        <button onclick="goBack(2)" style="background: #64748b; margin-top: 10px;">⬅ رجوع / Back</button>
    </div>

    <!-- الخطوة 4: قسم الذكاء الاصطناعي والبحث -->
    <div id="aiSection" class="hidden" style="margin-top: 30px; border-top: 2px dashed #cbd5e1; padding-top: 20px;">
        <h3 style="color: #0284c7;">🤖 البحث الذكي وتحديد الإجابة من الكتاب / AI Smart Search</h3>
        
        <div style="margin-bottom: 15px;">
            <label style="font-weight: bold; display: block; margin-bottom: 5px;">رفع ملف الـ PDF الخاص بالمادة:</label>
            <input type="file" id="pdfFile" accept=".pdf">
            <button onclick="uploadBook()">رفع ومعالجة الكتاب / Upload & Process Book</button>
        </div>

        <div style="margin-bottom: 15px;">
            <label style="font-weight: bold; display: block; margin-bottom: 5px;">اسم ملف الكتاب (مثال: physics.pdf):</label>
            <input type="text" id="bookName" placeholder="اكتب اسم ملف الكتاب هنا تماماً">
        </div>

        <div style="margin-bottom: 15px;">
            <label style="font-weight: bold; display: block; margin-bottom: 5px;">رقم صفحة الكتاب الفعلية (إذا كان هناك فارق عن ملف الـ PDF، وإلا اتركه 0):</label>
            <input type="number" id="pageOffset" value="0" placeholder="مثال: الفرق بين صفحة الملف وصفحة الكتاب">
        </div>

        <div style="margin-bottom: 15px;">
            <label style="font-weight: bold; display: block; margin-bottom: 5px;">اكتب سؤالك أو ما تريد البحث عنه:</label>
            <input type="text" id="query" placeholder="مثال: قانون الجذب الكوني...">
        </div>

        <button onclick="searchBook()" style="background: #0284c7;">بحث ذكي وتحديد الإجابة 🔍 / AI Search</button>

        <div id="resultBox" class="result-box">
            <h4>📌 الإجابة المحددة من الكتاب داخل الإطار الأحمر:</h4>
            <p id="answerText" style="font-size: 16px; font-weight: bold; line-height: 1.6;"></p>
            <p id="pageText" style="font-weight: bold; color: #dc2626; font-size: 17px; background: #fee2e2; padding: 8px; border-radius: 6px; display: inline-block;"></p>
        </div>
    </div>
</div>

<script>
let currentStage = "";
let currentSubject = "";

function selectStage(stage) {
    currentStage = stage;
    document.getElementById('step1').classList.add('hidden');
    document.getElementById('step2').classList.remove('hidden');
}

function selectSubject(subject) {
    currentSubject = subject;
    document.getElementById('selectedTitle').innerText = `المرحلة: ${currentStage} | المادة: ${currentSubject}`;
    document.getElementById('step2').classList.add('hidden');
    document.getElementById('step3').classList.remove('hidden');
}

function goBack(step) {
    if (step === 1) {
        document.getElementById('step2').classList.add('hidden');
        document.getElementById('step1').classList.remove('hidden');
    } else if (step === 2) {
        document.getElementById('step3').classList.add('hidden');
        document.getElementById('step2').classList.add('hidden');
        document.getElementById('step1').classList.remove('hidden');
        document.getElementById('aiSection').classList.add('hidden');
    }
}

function showAiSearch() {
    document.getElementById('aiSection').classList.remove('hidden');
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}

async function uploadBook() {
    const fileInput = document.getElementById('pdfFile');
    if (fileInput.files.length === 0) { alert('الرجاء اختيار ملف PDF أولاً'); return; }
    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    
    alert('جاري رفع ومعالجة الكتاب الكامل...');
    const res = await fetch('/upload-book/', { method: 'POST', body: formData });
    const data = await res.json();
    alert(data.message);
    document.getElementById('bookName').value = fileInput.files[0].name;
}

async function searchBook() {
    const bookName = document.getElementById('bookName').value;
    const query = document.getElementById('query').value;
    const offset = parseInt(document.getElementById('pageOffset').value) || 0;
    
    if (!bookName || !query) { alert('الرجاء إدخال اسم الكتاب والسؤال بدقة'); return; }

    const res = await fetch(`/search-book/?book_name=${encodeURIComponent(bookName)}&query=${encodeURIComponent(query)}&offset=${offset}`, { method: 'POST' });
    const data = await res.json();
    
    const resultBox = document.getElementById('resultBox');
    resultBox.style.display = 'block';
    
    if(data.error) {
        document.getElementById('answerText انعكاس').innerText = "";
        document.getElementById('answerText').innerText = data.error;
        document.getElementById('pageText').innerText = "";
    } else {
        document.getElementById('answerText').innerText = "النص المستخرج: " + data.extracted_text;
        document.getElementById('pageText').innerText = `📄 رقم الصفحة الفعلية في الكتاب: ${data.page_number}`;
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

@app.post("/upload-book/")
async def upload_book(file: UploadFile = File(...)):
    file_path = f"books/{file.filename}"
    with open(file_path, "wb") as f:
        f.write(await file.read())
    return {"status": "success", "message": f"تم رفع كتاب {file.filename} بنجاح!"}

@app.post("/search-book/")
async def search_book(book_name: str, query: str, offset: int = 0):
    pdf_path = f"books/{book_name}"
    if not os.path.exists(pdf_path):
        return {"error": "لم يتم العثور على ملف الكتاب، تأكد من رفعه بشكل صحيح."}
    
    reader = PdfReader(pdf_path)
    matched_text = ""
    found_page = 1
    
    keywords = query.split()
    for page_num, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        matches = sum(1 for kw in keywords if kw in text)
        if matches > 0:
            # حساب رقم الصفحة الفعلي مع الأخذ بعين الاعتبار الفارق (Offset)
            found_page = (page_num + 1) + offset
            matched_text = text[:450].replace("\n", " ")
            break
            
    if not matched_text:
        return {"error": "لم يتم العثور على إجابة مطابقة لهذا السؤال داخل الكتاب."}

    return {
        "page_number": found_page,
        "extracted_text": matched_text
    }
