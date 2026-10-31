"""control.py — لوحة تحكم شاملة للمنصة: إحصائيات، إعلانات، أرباح، إدارة الكتب والملفات، فحص النظام.
ضعه بجانب main.py (يستبدل analytics.py القديم) وطبّق 3 أسطر فقط في main.py (راجع الشرح).
"""
import os
import re
import json
import shutil
import sqlite3
import hashlib
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import List
from urllib.parse import urlparse

from fastapi import Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse

TZ = timezone(timedelta(hours=3))   # توقيت السعودية
SALT = os.environ.get("ANALYTICS_SALT", "smart-curriculum")
_BOT = re.compile(r"bot|crawl|spider|slurp|uptimerobot|pingdom|curl|python-requests|headless|render/", re.I)
_lock = threading.Lock()
_db_path = None
M = {}   # مساحة أسماء main.py (تُمرَّر في setup)


# ───────────── قاعدة البيانات ─────────────
def _q(sql, args=(), write=False):
    with _lock:
        c = sqlite3.connect(_db_path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            cur = c.execute(sql, args)
            if write:
                c.commit()
                return []
            return cur.fetchall()
        finally:
            c.close()


def _init():
    c = sqlite3.connect(_db_path, timeout=10)
    try:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS events(ts INTEGER, day TEXT, type TEXT, visitor TEXT, meta TEXT);
            CREATE INDEX IF NOT EXISTS ev_day ON events(day, type);
            CREATE TABLE IF NOT EXISTS earnings(day TEXT PRIMARY KEY, amount REAL);
            CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        """)
        c.commit()
    finally:
        c.close()


def get_set(k, default=""):
    r = _q("SELECT v FROM settings WHERE k=?", (k,))
    return r[0]["v"] if r else default


def put_set(k, v):
    _q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)), True)


def ads_cfg():
    try:
        c = json.loads(get_set("ads", "{}"))
    except Exception:
        c = {}
    client = (c.get("client") if "client" in c else os.environ.get("ADSENSE_CLIENT", "")) or ""
    return {"client": client.strip(),
            "home": ((c.get("home") if "home" in c else os.environ.get("ADS_SLOT_HOME", "")) or "").strip(),
            "result": ((c.get("result") if "result" in c else os.environ.get("ADS_SLOT_RESULT", "")) or "").strip(),
            "on": bool(c.get("on", True))}


def _log(typ, request, meta=""):
    ua = request.headers.get("user-agent", "")
    if _BOT.search(ua):
        return
    now = datetime.now(TZ)
    day = now.strftime("%Y-%m-%d")
    vid = hashlib.sha256(f"{SALT}{day}{M['client_ip'](request)}{ua}".encode()).hexdigest()[:16]
    _q("INSERT INTO events(ts,day,type,visitor,meta) VALUES(?,?,?,?,?)",
       (int(now.timestamp()), day, typ, vid, (meta or "")[:200]), True)


# ───────────── الإحصائيات ─────────────
def _stats(days):
    days = max(1, min(int(days), 365))
    today = datetime.now(TZ).date()
    start = today - timedelta(days=days - 1)
    s = start.isoformat()
    labels = [(start + timedelta(d)).isoformat() for d in range(days)]
    ser = {l: dict(visitors=0, views=0, asks=0, uploads=0, pdf=0) for l in labels}
    kmap = {"view": "views", "ask": "asks", "ask_custom": "asks", "upload": "uploads", "pdf_open": "pdf"}
    for r in _q("SELECT day,type,COUNT(*) n FROM events WHERE day>=? GROUP BY day,type", (s,)):
        if r["day"] in ser and r["type"] in kmap:
            ser[r["day"]][kmap[r["type"]]] += r["n"]
    for r in _q("SELECT day,COUNT(DISTINCT visitor) u FROM events WHERE day>=? GROUP BY day", (s,)):
        if r["day"] in ser:
            ser[r["day"]]["visitors"] = r["u"]
    tot = {k: sum(v[k] for v in ser.values()) for k in ("visitors", "views", "asks", "uploads", "pdf")}

    books, qs = Counter(), Counter()
    for r in _q("SELECT meta FROM events WHERE type IN ('ask','ask_custom') AND day>=? ORDER BY ts DESC LIMIT 5000", (s,)):
        b, _, q = (r["meta"] or "").partition("\t")
        books[b or "custom"] += 1
        if q.strip():
            qs[q.strip()] += 1
    dev, ref = Counter(), Counter()
    for r in _q("SELECT meta FROM events WHERE type='view' AND day>=? ORDER BY ts DESC LIMIT 20000", (s,)):
        d_, _, h = (r["meta"] or "").partition("\t")
        dev[d_ or "?"] += 1
        if h:
            ref[h] += 1

    earn = {r["day"]: r["amount"] for r in _q("SELECT day,amount FROM earnings WHERE day>=? ORDER BY day", (s,))}
    earn_total = sum(earn.values())
    views_with = sum(ser[d]["views"] for d in earn if d in ser)
    actual_rpm = (earn_total / views_with * 1000) if views_with else 0.0
    rpm_set = float(get_set("rpm", "0") or 0)
    rpm = actual_rpm or rpm_set
    views_without = sum(v["views"] for d, v in ser.items() if d not in earn)
    return {
        "days": days, "labels": labels, "series": [ser[l] for l in labels],
        "today": ser[labels[-1]], "totals": tot,
        "top_books": books.most_common(10), "top_questions": qs.most_common(15),
        "devices": dev.most_common(), "referrers": ref.most_common(8),
        "earn": [{"day": d, "amount": a} for d, a in sorted(earn.items(), reverse=True)[:30]],
        "earn_total": round(earn_total, 2), "est_total": round(earn_total + views_without * rpm / 1000, 2),
        "rpm_actual": round(actual_rpm, 3), "rpm_set": rpm_set,
    }


# ───────────── الكتب والملفات ─────────────
def _books_list():
    links = M["load_links"]()
    out = []
    for st in M["build_catalog"]()["stages"]:
        for y in st["years"]:
            for t in y["terms"]:
                for s in t["subjects"]:
                    p, q = M["book_path"](s["code"]), M["book_path"](s["code"], True)
                    out.append({"code": s["code"], "name": s["ar"], "stage": st["ar"], "grade": y["ar"], "term": t["n"],
                                "pdf": os.path.getsize(p) if p else 0, "solved": os.path.getsize(q) if q else 0,
                                "lk": links.get(s["code"], {})})
    return out


def _dir_size(path, exts=None):
    total = 0
    try:
        for f in os.listdir(path):
            p = os.path.join(path, f)
            if os.path.isfile(p) and (not exts or f.lower().endswith(exts)):
                total += os.path.getsize(p)
    except Exception:
        pass
    return total


def _uploads():
    d, now, out = M["UPLOAD_DIR"], time.time(), []
    for f in os.listdir(d):
        if re.fullmatch(r"[0-9a-f]{40}\.pdf", f):
            st = os.stat(os.path.join(d, f))
            out.append({"id": f[:-4], "size": st.st_size, "age": int(now - st.st_mtime)})
    return sorted(out, key=lambda x: x["age"])


def _sysinfo():
    du = shutil.disk_usage(M["BOOKS_DIR"])
    return {"disk_total": du.total, "disk_free": du.free,
            "books_size": _dir_size(M["BOOKS_DIR"], (".pdf",)),
            "cache_size": _dir_size(M["CACHE_DIR"]), "db_size": os.path.getsize(_db_path) if os.path.exists(_db_path) else 0,
            "uploads_size": _dir_size(M["UPLOAD_DIR"]), "books_dir": M["BOOKS_DIR"],
            "anthropic_key": bool(M["ANTHROPIC_API_KEY"]), "gemini_key": bool(M["GEMINI_API_KEY"]),
            "claude_model": M["CLAUDE_MODEL"], "claude_fallbacks": M["CLAUDE_FALLBACKS"], "gemini_models": M["GEMINI_MODELS"],
            "web_search": M["WEB_SEARCH"], "selftest": M["AI_STATUS"]["selftest"], "tested_at": M["AI_STATUS"]["at"],
            "recent_errors": M["LAST_AI_ERRORS"][-6:]}


# ───────────── الإعلانات (تُحقن في صفحة الموقع بدون تعديل HTML_PAGE) ─────────────
CLIENT_JS = r"""
(function(){try{
const C=__CFG__;
const st=document.createElement('style');
st.textContent='.adbox{margin:22px 0;text-align:center;min-height:90px;clear:both}.adbox small{display:block;color:var(--muted);font-size:.7rem;margin-bottom:4px}'+
'.annc{margin:0 0 16px;padding:12px 16px;border-radius:16px;background:rgba(212,175,90,.18);border:1px solid var(--gold);font-weight:600}'+
'.plink{text-align:center;font-size:.8rem;margin-top:18px}';
document.head.appendChild(st);
function put(parent,slot){
  if(!parent||!C.client||!slot)return;
  const d=document.createElement('div');d.className='adbox';
  d.innerHTML='<small>إعلان</small><ins class="adsbygoogle" style="display:block" data-ad-client="'+C.client+'" data-ad-slot="'+slot+'" data-ad-format="auto" data-full-width-responsive="true"></ins>';
  parent.appendChild(d);
  try{(window.adsbygoogle=window.adsbygoogle||[]).push({})}catch(e){}
}
const r0=render;
render=function(){
  r0.apply(this,arguments);
  const app=document.getElementById('app');
  if(!app||typeof state==='undefined'||!state.cat)return;
  if(state.view==='home'){
    if(C.announce){const a=document.createElement('div');a.className='annc';a.textContent=C.announce;app.insertBefore(a,app.firstChild)}
    put(app,C.home);
    const p=document.createElement('p');p.className='plink';p.innerHTML='<a href="/privacy">سياسة الخصوصية</a>';app.appendChild(p);
  }else if(state.view==='subject'){put(app,C.home)}
};
const s0=showResult;
showResult=function(d,o){
  s0.apply(this,arguments);
  if(!(o&&o.pending))put(document.getElementById('out'),C.result);
};
}catch(e){console.log('ads init',e)}})();
"""


def inject(html):
    """يحقن إعلانات AdSense وإعلان الموقع حسب إعدادات لوحة التحكم (بدون إعادة نشر)."""
    c = ads_cfg()
    on = c["on"] and c["client"]
    pub = {"client": c["client"] if on else "", "home": c["home"], "result": c["result"],
           "announce": get_set("announce", "")}
    js = CLIENT_JS.replace("__CFG__", json.dumps(pub, ensure_ascii=False).replace("</", "<\\/"))
    if on:
        tag = ('<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client='
               + c["client"] + '" crossorigin="anonymous"></script>\n')
        html = html.replace("</head>", tag + "</head>", 1)
    return html.replace("</body>", "<script>" + js + "</script>\n</body>", 1)


PRIVACY_PAGE = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>سياسة الخصوصية</title>
<style>body{font-family:system-ui,'IBM Plex Sans Arabic',sans-serif;max-width:680px;margin:0 auto;padding:20px;line-height:2;color:#13211c;background:#f5f2e9}
h1{color:#0e5a44}a{color:#0e5a44}</style></head><body>
<h1>سياسة الخصوصية</h1>
<p>نحترم خصوصيتك. هذا الموقع يساعد الطلاب على البحث في كتبهم المدرسية بالذكاء الاصطناعي.</p>
<h3>ما الذي نجمعه؟</h3>
<p>نسجّل إحصائيات مجهولة الهوية فقط (عدد الزيارات، نوع الجهاز، الموقع المُحيل، عدد الأسئلة، والكتب الأكثر استخداماً) لتحسين الخدمة. لا نطلب اسمك ولا بريدك ولا أي بيانات شخصية، ولا نحفظ عنوان IP نفسه.</p>
<h3>الملفات والأسئلة</h3>
<p>الملفات التي ترفعها تُحفظ مؤقتاً لغرض الإجابة عن أسئلتك ثم تُحذف تلقائياً. ويُرسل نص السؤال والصفحات المطلوبة إلى مزوّدي الذكاء الاصطناعي لتوليد الإجابة.</p>
<h3>الإعلانات وملفات تعريف الارتباط</h3>
<p>قد يعرض الموقع إعلانات من Google AdSense. تستخدم Google وشركاؤها ملفات تعريف الارتباط (الكوكيز) لعرض إعلانات بناءً على زياراتك لهذا الموقع ومواقع أخرى. يمكنك إيقاف الإعلانات المخصّصة من
<a href="https://adssettings.google.com" rel="noopener">إعدادات الإعلانات في Google</a>.</p>
<p><a href="/">العودة للموقع</a></p></body></html>"""


# ───────────── واجهة لوحة التحكم ─────────────
DASH_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>لوحة التحكم</title>
<style>
:root{--g:#0e5a44;--gold:#d4af5a;--bg:#f3f1ea;--card:#fff;--ink:#13211c;--mut:#5d6b64;--line:#e4dfcf;--red:#b23a2a}
@media(prefers-color-scheme:dark){:root{--bg:#06110c;--card:#0e1f17;--ink:#eef3ef;--mut:#9db3a8;--line:#1e3a2d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,'IBM Plex Sans Arabic',sans-serif;line-height:1.7;padding:12px 12px 80px}
.w{max-width:780px;margin:0 auto}h1{margin:0;color:var(--g);font-size:1.3rem}@media(prefers-color-scheme:dark){h1{color:var(--gold)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;margin:12px 0}
h2{margin:0 0 10px;font-size:1rem}.hint{color:var(--mut);font-size:.82rem;margin:4px 0}
input,select,textarea{width:100%;padding:11px;border:1.5px solid var(--line);border-radius:12px;font:inherit;background:var(--card);color:var(--ink);margin-top:6px}
textarea{min-height:110px}
label.l{display:block;font-weight:600;font-size:.88rem;margin-top:10px}
.btn{display:inline-block;padding:11px 16px;border:0;border-radius:12px;background:var(--g);color:#f1e2b8;font:inherit;font-weight:700;cursor:pointer;text-align:center}
.btn.s{padding:7px 12px;font-size:.84rem}.btn.red{background:var(--red);color:#fff}.btn.alt{background:var(--card);color:var(--g);border:1.5px solid var(--g)}
.btn.full{width:100%;margin-top:12px}
.row{display:flex;gap:8px;align-items:center}.row>*{flex:1}.row>.btn{flex:none}
#tabs{display:flex;gap:6px;overflow-x:auto;margin:12px 0;position:sticky;top:0;background:var(--bg);padding:6px 0;z-index:5;scrollbar-width:none}
#tabs button{flex:none;padding:9px 14px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit;font-weight:600;cursor:pointer;white-space:nowrap}
#tabs button.on{background:var(--gold);color:#2b2108;border-color:var(--gold)}
.tabs2{display:flex;gap:6px;margin-bottom:8px}.tabs2 button{flex:1;padding:8px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit;cursor:pointer}
.tabs2 button.on{background:var(--g);color:#f1e2b8}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px}
.k{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:10px 12px;border-top:4px solid var(--gold)}
.k b{display:block;font-size:1.5rem;line-height:1.2;color:var(--g)}@media(prefers-color-scheme:dark){.k b{color:var(--gold)}}
.k span{font-size:.78rem;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:.86rem}td,th{border-bottom:1px solid var(--line);padding:6px;text-align:start}td.n{text-align:end;font-weight:700;width:70px}
.book{border:1px solid var(--line);border-radius:14px;padding:10px;margin:8px 0;background:var(--card)}
.bh b{display:block}.bh small{color:var(--mut)}
.badge{display:inline-block;font-size:.76rem;padding:2px 10px;border-radius:999px;background:var(--line);margin:6px 4px 0 0}.badge.ok{background:rgba(20,148,109,.18);color:#14946d;font-weight:700}
.acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
details{margin-top:8px}summary{cursor:pointer;font-size:.86rem;color:var(--g);font-weight:600}
#pb{display:none;position:fixed;bottom:0;inset-inline:0;background:var(--card);border-top:2px solid var(--gold);padding:8px 14px;z-index:20}
#pb i{display:block;height:10px;width:0;background:linear-gradient(90deg,#14946d,var(--gold));border-radius:9px}
.toast{position:fixed;top:12px;inset-inline:12px;max-width:500px;margin:auto;padding:12px;border-radius:12px;background:var(--g);color:#fff;text-align:center;z-index:30;display:none}
.toast.on{display:block}.toast.bad{background:var(--red)}
.ok{color:#14946d}.bad{color:var(--red)}
</style></head><body><div class="w">
<div id="toast" class="toast"></div>
<h1>🛠️ لوحة التحكم الشاملة</h1><p class="hint">خاصة بك فقط. تحتاج مفتاح الإدارة (ADMIN_KEY).</p>
<div class="card" id="login"><label class="l">مفتاح الإدارة</label><input type="password" id="key" placeholder="ADMIN_KEY" onkeydown="if(event.key==='Enter')login()"><button class="btn full" onclick="login()">دخول</button></div>
<div id="app" style="display:none"><div id="tabs"></div><div id="view"></div></div>
<div id="pb"><i></i><small id="pbt"></small></div>
</div>
<script>
const $=i=>document.getElementById(i);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>Number(n||0).toLocaleString('en-US',{maximumFractionDigits:2});
const mb=b=>b>=1073741824?(b/1073741824).toFixed(2)+' GB':b>=1048576?(b/1048576).toFixed(1)+' MB':Math.max(1,Math.round(b/1024))+' KB';
const ago=s=>s<3600?Math.round(s/60)+' د':s<86400?Math.round(s/3600)+' س':Math.round(s/86400)+' يوم';
let KEY=sessionStorage.getItem('dk')||'',DAYS=30,BOOKS=[],BQ='',BF='all';
const H=()=>({'X-Admin-Key':KEY});
function toast(m,bad){const t=$('toast');t.textContent=m;t.className='toast on'+(bad?' bad':'');clearTimeout(toast.t);toast.t=setTimeout(()=>t.className='toast',3500)}
async function api(url,opts){opts=opts||{};opts.headers=Object.assign(H(),opts.headers||{});
  const r=await fetch(url,opts);let d={};try{d=await r.json()}catch(_){}
  if(!r.ok){const e=new Error(d.error_ar||d.error||'فشل');e.status=r.status;throw e}return d}
const form=o=>{const f=new FormData();for(const k in o)f.append(k,o[k]);return f};
const post=(u,o)=>api(u,{method:'POST',body:form(o)});
function upload(url,fd,label){return new Promise((res,rej)=>{const x=new XMLHttpRequest();x.open('POST',url);x.setRequestHeader('X-Admin-Key',KEY);
  const pb=$('pb'),bar=pb.firstElementChild;pb.style.display='block';bar.style.width='0';$('pbt').textContent=label+' 0%';
  x.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);bar.style.width=p+'%';$('pbt').textContent=label+' '+p+'%'}};
  x.onload=()=>{pb.style.display='none';let d={};try{d=JSON.parse(x.responseText)}catch(_){}x.status===200?res(d):rej(new Error(d.error_ar||d.error||'فشل الرفع'))};
  x.onerror=()=>{pb.style.display='none';rej(new Error('انقطع الاتصال'))};x.send(fd)})}
async function login(){KEY=$('key').value||KEY;try{await api('/api/ctl/ping');sessionStorage.setItem('dk',KEY);$('login').style.display='none';$('app').style.display='block';show('ov')}catch(e){toast(e.message,1)}}
function logout(){sessionStorage.removeItem('dk');$('login').style.display='block';$('app').style.display='none'}
const TABS=[['ov','📊 نظرة عامة'],['money','💰 الأرباح والإعلانات'],['books','📚 الكتب'],['files','🗂️ ملفات الزوار'],['sys','⚙️ النظام']];
function show(t){$('tabs').innerHTML=TABS.map(x=>`<button class="${x[0]===t?'on':''}" onclick="show('${x[0]}')">${x[1]}</button>`).join('');
  $('view').innerHTML='<p class="hint">جاري التحميل...</p>';
  ({ov,money,books,files,sys})[t]().catch(e=>{toast(e.message,1);if(e.status===403||e.status===429)logout()})}
function bars(id,labels,vals,color){const W=640,Hh=140,p=8,n=vals.length,mx=Math.max(1,...vals),bw=(W-2*p)/n;
  let s=`<svg viewBox="0 0 ${W} ${Hh+26}" style="width:100%;direction:ltr;color:var(--mut)">`;
  vals.forEach((v,i)=>{const h=v/mx*Hh;s+=`<rect x="${p+i*bw+1}" y="${Hh-h+14}" width="${Math.max(1,bw-2)}" height="${h}" rx="2" fill="${color}"><title>${labels[i]}: ${v}</title></rect>`});
  s+=`<text x="${p}" y="${Hh+26}" font-size="11" fill="currentColor">${labels[0].slice(5)}</text><text x="${W-p}" y="${Hh+26}" text-anchor="end" font-size="11" fill="currentColor">${labels[n-1].slice(5)}</text><text x="${W-p}" y="10" text-anchor="end" font-size="11" fill="currentColor">max ${mx}</text></svg>`;
  $(id).innerHTML=s}
const table=(rows,empty)=>rows.length?'<table>'+rows.map(r=>`<tr><td>${esc(r[0])}</td><td class="n">${esc(r[1])}</td></tr>`).join('')+'</table>':`<p class="hint">${empty||'لا توجد بيانات بعد.'}</p>`;

/* ── نظرة عامة ── */
async function ov(){const d=await api('/api/ctl/stats?days='+DAYS),t=d.today,k=d.totals;
  $('view').innerHTML=`<div class="tabs2">${[7,30,90].map(x=>`<button class="${x===DAYS?'on':''}" onclick="DAYS=${x};show('ov')">آخر ${x} يوم</button>`).join('')}</div>
  <div class="kpis">${[['زوار اليوم',t.visitors],['فتح الموقع اليوم',t.views],['أسئلة اليوم',t.asks],['زوار الفترة',k.visitors],['أسئلة الفترة',k.asks],['كتب فُتحت',k.pdf],['ملفات مرفوعة',k.uploads],['أرباح مسجلة',d.earn_total],['إجمالي متوقع',d.est_total]].map(x=>`<div class="k"><b>${fmt(x[1])}</b><span>${x[0]}</span></div>`).join('')}</div>
  <div class="card"><h2>الزوار يومياً</h2><div id="c1"></div></div>
  <div class="card"><h2>الأسئلة يومياً</h2><div id="c2"></div></div>
  <div class="card"><h2>📱 الأجهزة</h2>${table(d.devices.map(x=>[x[0]==='mobile'?'جوال':x[0]==='desktop'?'كمبيوتر':x[0],x[1]]))}</div>
  <div class="card"><h2>🔗 مصادر الزيارات (المواقع المُحيلة)</h2>${table(d.referrers,'لا توجد مصادر (زيارات مباشرة غالباً).')}</div>
  <div class="card"><h2>📚 أكثر الكتب سؤالاً</h2>${table(d.top_books)}</div>
  <div class="card"><h2>❓ أكثر الأسئلة تكراراً</h2>${table(d.top_questions)}</div>
  <p class="hint">الزوار = أجهزة مختلفة في اليوم (مجهولة الهوية)، ومجموع الفترة هو مجموع زوار كل يوم.</p>`;
  bars('c1',d.labels,d.series.map(s=>s.visitors),'#14946d');bars('c2',d.labels,d.series.map(s=>s.asks),'#d4af5a')}

/* ── الأرباح والإعلانات ── */
async function money(){const [a,d]=await Promise.all([api('/api/ctl/ads'),api('/api/ctl/stats?days=90')]);
  const today=new Date(Date.now()+3*3600e3).toISOString().slice(0,10);
  $('view').innerHTML=`<div class="kpis"><div class="k"><b>${fmt(d.earn_total)}</b><span>أرباح مسجلة (90 يوم)</span></div><div class="k"><b>${fmt(d.est_total)}</b><span>إجمالي متوقع</span></div><div class="k"><b>${fmt(d.rpm_actual)}</b><span>RPM الفعلي</span></div></div>
  <div class="card"><h2>📢 إعدادات الإعلانات (تعمل فوراً بدون إعادة نشر)</h2>
   <label class="l">معرّف AdSense (Publisher ID)</label><input id="a_c" dir="ltr" placeholder="ca-pub-1234567890123456" value="${esc(a.client)}">
   <label class="l">رقم وحدة إعلان الصفحة الرئيسية (Slot)</label><input id="a_h" dir="ltr" placeholder="1234567890" value="${esc(a.home)}">
   <label class="l">رقم وحدة إعلان نتيجة الإجابة (Slot)</label><input id="a_r" dir="ltr" placeholder="1234567890" value="${esc(a.result)}">
   <label class="l"><input type="checkbox" id="a_on" style="width:auto" ${a.on?'checked':''}> تفعيل الإعلانات</label>
   <label class="l">إعلان/رسالة تظهر أعلى الصفحة الرئيسية (اختياري)</label><textarea id="a_n" placeholder="مثال: بالتوفيق للجميع في الاختبارات">${esc(a.announce)}</textarea>
   <button class="btn full" onclick="saveAds()">حفظ</button>
   <p class="hint">اتركه فارغاً لإيقاف الإعلانات. ملف ads.txt يُنشأ تلقائياً في <a href="/ads.txt" target="_blank">/ads.txt</a>.</p></div>
  <div class="card"><h2>💰 تسجيل أرباح يوم</h2>
   <p class="hint">أرباح AdSense لا تُسحب تلقائياً (يتطلب ربط API). انسخ ربح اليوم من لوحة AdSense وسجّله هنا؛ يحسب النظام RPM الفعلي ويقدّر باقي الأيام. اكتب 0 لحذف يوم.</p>
   <div class="row"><input type="date" id="e_d" value="${today}"><input type="number" step="0.01" id="e_a" placeholder="الربح"><button class="btn" onclick="saveEarn()">حفظ</button></div>
   <div class="row" style="margin-top:8px"><input type="number" step="0.01" id="e_r" placeholder="RPM تقديري لكل 1000 زيارة" value="${d.rpm_set||''}"><button class="btn" onclick="saveRpm()">حفظ</button></div>
   <div style="margin-top:10px">${table(d.earn.map(e=>[e.day,fmt(e.amount)]),'لا توجد أرباح مسجلة بعد.')}</div></div>`}
async function saveAds(){await post('/api/ctl/ads',{client:$('a_c').value.trim(),home:$('a_h').value.trim(),result:$('a_r').value.trim(),on:$('a_on').checked?1:0,announce:$('a_n').value.trim()});toast('تم الحفظ، ويظهر التغيير عند تحديث الموقع')}
async function saveEarn(){if(!$('e_a').value)return;await post('/api/ctl/earning',{day:$('e_d').value,amount:$('e_a').value});toast('تم الحفظ');show('money')}
async function saveRpm(){await post('/api/ctl/rpm',{rpm:$('e_r').value||0});toast('تم الحفظ');show('money')}

/* ── الكتب ── */
async function books(){BOOKS=(await api('/api/ctl/books')).books;
  const groups={};BOOKS.forEach(b=>{const g=b.code.match(/^([a-z]\d_t\d)_/);if(g&&!groups[g[1]])groups[g[1]]=`${b.stage} · ${b.grade} · الفصل ${b.term}`});
  const have=BOOKS.filter(b=>b.pdf).length;
  $('view').innerHTML=`<div class="kpis"><div class="k"><b>${have}/${BOOKS.length}</b><span>كتب مرفوعة</span></div><div class="k"><b>${BOOKS.filter(b=>b.solved).length}</b><span>كتب محلولة</span></div><div class="k"><b>${BOOKS.length-have}</b><span>ناقصة</span></div></div>
  <div class="card"><h2>⬆ رفع جماعي</h2><p class="hint">سمِّ كل ملف باسم الكود: <b dir="ltr">p3_t1_math.pdf</b> أو <b dir="ltr">p3_t1_math_solved.pdf</b>. الكود يظهر تحت اسم كل مادة أدناه.</p>
   <input type="file" id="bulk" accept="application/pdf" multiple><button class="btn full" onclick="bulkUp()">رفع الملفات</button></div>
  <div class="card"><h2>📋 قائمة الكتب</h2><input id="bq" placeholder="ابحث باسم المادة أو الصف أو الكود" oninput="BQ=this.value;drawList()">
   <select id="bf" onchange="BF=this.value;drawList()"><option value="all">الكل</option><option value="missing">الناقصة فقط</option><option value="has">المرفوعة فقط</option></select><div id="blist"></div></div>
  <div class="card"><h2>✏️ تعديل مواد فصل دراسي</h2><p class="hint">كل سطر: <b dir="ltr">slug|الاسم العربي|English</b> (slug إنجليزي بدون شرطة سفلية). اتركه فارغاً للرجوع للقائمة الافتراضية. تغيير الـ slug يغيّر كود الكتاب المطلوب.</p>
   <select id="cg" onchange="loadCat()">${Object.entries(groups).map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('')}</select>
   <textarea id="ct" dir="ltr" style="min-height:150px"></textarea><button class="btn full" onclick="saveCat()">حفظ المواد</button></div>`;
  $('bq').value=BQ;$('bf').value=BF;drawList();loadCat()}
function drawList(){const q=BQ.trim().toLowerCase();
  const L=BOOKS.filter(b=>(BF==='all'||(BF==='missing'?!b.pdf:b.pdf))&&(!q||(b.name+' '+b.grade+' '+b.stage+' '+b.code).toLowerCase().includes(q))).slice(0,60);
  $('blist').innerHTML=(L.map(bookCard).join('')||'<p class="hint">لا نتائج.</p>')+(L.length===60?'<p class="hint">يعرض أول 60 نتيجة، استخدم البحث.</p>':'')}
function bookCard(b){const lk=b.lk||{},c=b.code;
  return `<div class="book"><div class="bh"><b>${esc(b.name)}</b><small>${esc(b.stage)} · ${esc(b.grade)} · الفصل ${b.term} · <bdi dir="ltr">${c}</bdi></small></div>
  <span class="badge ${b.pdf?'ok':''}">📘 ${b.pdf?mb(b.pdf):'لا يوجد'}</span><span class="badge ${b.solved?'ok':''}">✅ محلول: ${b.solved?mb(b.solved):'لا يوجد'}</span>
  <div class="acts"><label class="btn s">⬆ ${b.pdf?'استبدال':'رفع'} الكتاب<input type="file" accept="application/pdf" hidden onchange="upBook('${c}','pdf',this)"></label>
  <label class="btn s alt">⬆ ${b.solved?'استبدال':'رفع'} المحلول<input type="file" accept="application/pdf" hidden onchange="upBook('${c}','solved',this)"></label>
  ${b.pdf?`<button class="btn s red" onclick="delBook('${c}','pdf')">🗑 الكتاب</button>`:''}${b.solved?`<button class="btn s red" onclick="delBook('${c}','solved')">🗑 المحلول</button>`:''}</div>
  <details><summary>🔗 روابط خارجية وفيديو</summary>
   <input data-c="${c}" data-k="pdf" dir="ltr" placeholder="رابط الكتاب (https://...)" value="${esc(lk.pdf||'')}">
   <input data-c="${c}" data-k="solved" dir="ltr" placeholder="رابط المحلول" value="${esc(lk.solved||'')}">
   <input data-c="${c}" data-k="video" dir="ltr" placeholder="رابط شرح الدروس (يوتيوب)" value="${esc(lk.video||'')}">
   <button class="btn s full" onclick="saveLinks('${c}',this)">حفظ الروابط</button></details></div>`}
async function refreshBooks(){BOOKS=(await api('/api/ctl/books')).books;drawList()}
async function upBook(code,kind,inp){const f=inp.files[0];if(!f)return;
  try{await upload('/api/ctl/book/upload',form({code,kind,file:f}),'رفع '+code);toast('تم رفع '+code);await refreshBooks()}catch(e){toast(e.message,1)}}
async function delBook(code,kind){if(!confirm('حذف الملف نهائياً؟ '+code))return;try{await post('/api/ctl/book/delete',{code,kind});toast('تم الحذف');await refreshBooks()}catch(e){toast(e.message,1)}}
async function bulkUp(){const fs=$('bulk').files;if(!fs.length)return;const fd=new FormData();for(const f of fs)fd.append('files',f);
  try{const d=await upload('/api/ctl/book/bulk',fd,'رفع '+fs.length+' ملف');const ok=d.results.filter(r=>r.ok).length;const bad=d.results.filter(r=>!r.ok);
    toast(`تم رفع ${ok} من ${d.results.length}`+(bad.length?' — '+bad.map(r=>r.file+': '+r.error).join(' | '):''),bad.length>0);await refreshBooks()}catch(e){toast(e.message,1)}}
async function saveLinks(code,btn){const inps=btn.parentElement.querySelectorAll('input[data-k]');
  try{for(const i of inps)await post('/api/ctl/link',{code,kind:i.dataset.k,url:i.value.trim()});toast('تم حفظ الروابط');await refreshBooks()}catch(e){toast(e.message,1)}}
async function loadCat(){const g=$('cg').value;if(!g)return;$('ct').value=(await api('/api/ctl/catalog?group='+g)).text}
async function saveCat(){try{const d=await post('/api/ctl/catalog',{group:$('cg').value,text:$('ct').value});toast('تم الحفظ ('+d.count+' مادة)');await refreshBooks()}catch(e){toast(e.message,1)}}

/* ── ملفات الزوار ── */
async function files(){const d=await api('/api/ctl/uploads');
  $('view').innerHTML=`<div class="card"><h2>🗂️ الملفات التي رفعها الزوار</h2><p class="hint">تُحذف تلقائياً بعد 12 ساعة من آخر استخدام. يمكنك حذفها الآن لتوفير المساحة.</p>
  <div class="kpis"><div class="k"><b>${d.files.length}</b><span>ملف</span></div><div class="k"><b>${mb(d.files.reduce((a,x)=>a+x.size,0))}</b><span>الحجم</span></div></div>
  ${d.files.length?'<table>'+d.files.map(x=>`<tr><td dir="ltr">${x.id.slice(0,10)}…</td><td>${mb(x.size)}</td><td>${ago(x.age)}</td><td><button class="btn s red" onclick="delUp('${x.id}')">حذف</button></td></tr>`).join('')+'</table>':'<p class="hint">لا توجد ملفات.</p>'}
  ${d.files.length?'<button class="btn red full" onclick="delUp(\'all\')">حذف الكل</button>':''}</div>`}
async function delUp(id){if(!confirm(id==='all'?'حذف كل ملفات الزوار؟':'حذف الملف؟'))return;try{await post('/api/ctl/uploads/delete',{id});toast('تم الحذف');show('files')}catch(e){toast(e.message,1)}}

/* ── النظام ── */
async function sys(){const s=await api('/api/ctl/system');
  const used=s.disk_total-s.disk_free;
  $('view').innerHTML=`<div class="kpis"><div class="k"><b>${mb(s.books_size)}</b><span>حجم الكتب</span></div><div class="k"><b>${mb(s.cache_size)}</b><span>فهارس البحث</span></div><div class="k"><b>${mb(s.db_size)}</b><span>قاعدة الإحصائيات</span></div><div class="k"><b>${mb(s.disk_free)}</b><span>مساحة متبقية</span></div></div>
  <div class="card"><h2>💾 التخزين</h2><p class="hint" dir="ltr">${esc(s.books_dir)}</p><p class="hint">استخدام القرص: ${mb(used)} من ${mb(s.disk_total)}. إذا لم يكن المجلد على Persistent Disk في Render فستضيع الكتب والإحصائيات عند إعادة التشغيل.</p></div>
  <div class="card"><h2>🤖 الذكاء الاصطناعي</h2>
   <p>${s.anthropic_key?'<span class="ok">✓</span>':'<span class="bad">✗</span>'} مفتاح Claude &nbsp; ${s.gemini_key?'<span class="ok">✓</span>':'<span class="bad">✗</span>'} مفتاح Gemini &nbsp; ${s.web_search?'<span class="ok">✓</span>':'<span class="bad">✗</span>'} بحث الإنترنت</p>
   <p class="hint" dir="ltr">${esc(s.claude_model)} → ${esc(s.claude_fallbacks.join(', '))}<br>${esc(s.gemini_models.join(', '))}</p>
   <button class="btn full" onclick="aiTest(this)">فحص كل الموديلات الآن</button><div id="aires">${aiRows(s.selftest)}${s.tested_at?`<p class="hint">آخر فحص: ${esc(s.tested_at)}</p>`:''}</div>
   ${s.recent_errors.length?`<details><summary>آخر الأخطاء</summary><p class="hint bad" dir="ltr">${s.recent_errors.map(esc).join('<br>')}</p></details>`:''}</div>
  <div class="card"><h2>🔗 روابط مفيدة</h2><p><a href="/" target="_blank">الموقع</a> · <a href="/privacy" target="_blank">الخصوصية</a> · <a href="/ads.txt" target="_blank">ads.txt</a> · <a href="/api/missing" target="_blank">الكتب الناقصة</a></p></div>
  <button class="btn alt full" onclick="logout()">تسجيل الخروج</button>`}
const aiRows=a=>(a||[]).map(x=>`<div class="${x.ok?'ok':'bad'}" dir="ltr" style="font-size:.82rem">${esc(x.provider)} / ${esc(x.model)}: ${x.ok?'OK ('+x.ms+' ms)':'ERROR '+esc(x.error)}</div>`).join('');
async function aiTest(b){b.disabled=true;b.textContent='جاري الفحص...';try{const d=await api('/api/ctl/ai-test',{method:'POST'});$('aires').innerHTML=aiRows(d.results)||'<p class="bad">لا يوجد أي مفتاح مضبوط.</p>'}catch(e){toast(e.message,1)}b.disabled=false;b.textContent='فحص كل الموديلات الآن'}
if(KEY)login();
</script></body></html>"""


# ───────────── التركيب ─────────────
def setup(app, ns):
    """ns = globals() الخاصة بـ main.py"""
    global _db_path, M
    M = ns   # مرجع حي لمساحة أسماء main.py (يُستدعى setup مبكراً قبل تعريف باقي الدوال)
    _db_path = os.path.join(M["BOOKS_DIR"], "analytics.db")
    _init()

    def err(msg, code=400):
        return JSONResponse(status_code=code, content={"error_ar": msg})

    def guard(request, fn):
        def go():
            M["_admin"](request.headers.get("x-admin-key", ""), request)
            return fn()
        return M["respond"](go)

    # تتبع الزيارات
    @app.middleware("http")
    async def _track(request: Request, call_next):
        resp = await call_next(request)
        try:
            if request.method == "GET" and request.url.path == "/" and resp.status_code == 200:
                body = b"".join([chunk async for chunk in resp.body_iterator])
                hdrs = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-type")}
                resp = HTMLResponse(inject(body.decode("utf-8")), status_code=200, headers=hdrs)
        except Exception as e:
            print("[control] inject failed:", type(e).__name__, str(e)[:120])
        try:
            p, m, ok = request.url.path, request.method, resp.status_code < 400
            qp = request.query_params
            if m == "GET" and p == "/":
                ua = request.headers.get("user-agent", "")
                host = urlparse(request.headers.get("referer", "")).netloc
                if host == request.headers.get("host", ""):
                    host = ""
                _log("view", request, ("mobile" if "mobi" in ua.lower() else "desktop") + "\t" + host)
            elif m == "GET" and p == "/api/search" and ok:
                _log("ask", request, f"{qp.get('book_code', 'custom')}\t{qp.get('query', '')}")
            elif m == "GET" and p == "/api/custom/ask" and ok:
                _log("ask_custom", request, f"custom\t{qp.get('query', '')}")
            elif m == "POST" and p == "/api/custom/upload" and ok:
                _log("upload", request)
            elif m == "GET" and p.startswith("/api/book/") and p.endswith("/pdf") and ok:
                _log("pdf_open", request, p.split("/")[3])
        except Exception as e:
            print("[control]", type(e).__name__, str(e)[:120])
        return resp

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse(DASH_PAGE)

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy():
        return HTMLResponse(PRIVACY_PAGE)

    @app.get("/ads.txt")
    def ads_txt():
        pub = ads_cfg()["client"].replace("ca-pub-", "pub-")
        return PlainTextResponse(f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n" if pub else "")

    @app.get("/api/ctl/ping")
    def ctl_ping(request: Request):
        return guard(request, lambda: {"ok": True})

    @app.get("/api/ctl/stats")
    def ctl_stats(request: Request, days: int = 30):
        return guard(request, lambda: _stats(days))

    @app.post("/api/ctl/earning")
    def ctl_earning(request: Request, day: str = Form(...), amount: float = Form(...)):
        def go():
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or amount < 0:
                return err("بيانات غير صحيحة.")
            if amount == 0:
                _q("DELETE FROM earnings WHERE day=?", (day,), True)
            else:
                _q("INSERT INTO earnings(day,amount) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET amount=excluded.amount",
                   (day, amount), True)
            return {"ok": True}
        return guard(request, go)

    @app.post("/api/ctl/rpm")
    def ctl_rpm(request: Request, rpm: float = Form(0)):
        def go():
            put_set("rpm", max(0.0, rpm))
            return {"ok": True}
        return guard(request, go)

    @app.get("/api/ctl/ads")
    def ctl_ads_get(request: Request):
        def go():
            c = ads_cfg()
            c["announce"] = get_set("announce", "")
            return c
        return guard(request, go)

    @app.post("/api/ctl/ads")
    def ctl_ads_set(request: Request, client: str = Form(""), home: str = Form(""), result: str = Form(""),
                    on: int = Form(1), announce: str = Form("")):
        def go():
            if client and not re.fullmatch(r"ca-pub-\d{8,20}", client):
                return err("معرّف AdSense يجب أن يكون بصيغة ca-pub-1234567890123456")
            if (home and not home.isdigit()) or (result and not result.isdigit()):
                return err("رقم وحدة الإعلان (Slot) أرقام فقط.")
            put_set("ads", json.dumps({"client": client, "home": home, "result": result, "on": bool(on)}))
            put_set("announce", announce[:300])
            return {"ok": True}
        return guard(request, go)

    @app.get("/api/ctl/books")
    def ctl_books(request: Request):
        return guard(request, lambda: {"books": _books_list()})

    @app.post("/api/ctl/book/upload")
    def ctl_book_upload(request: Request, code: str = Form(...), kind: str = Form("pdf"), file: UploadFile = File(...)):
        def go():
            M["_save_book"](file, code, "solved" if kind == "solved" else "pdf")
            return {"ok": True}
        return guard(request, go)

    @app.post("/api/ctl/book/bulk")
    def ctl_book_bulk(request: Request, files: List[UploadFile] = File(...)):
        def go():
            res = []
            for up in files:
                base = re.sub(r"\.pdf$", "", up.filename or "", flags=re.I)
                kind = "solved" if base.endswith("_solved") else "pdf"
                code = base[:-7] if kind == "solved" else base
                try:
                    M["_save_book"](up, code, kind)
                    res.append({"file": up.filename, "ok": True})
                except M["ApiError"] as e:
                    res.append({"file": up.filename, "ok": False, "error": e.msg})
            return {"results": res}
        return guard(request, go)

    @app.post("/api/ctl/book/delete")
    def ctl_book_delete(request: Request, code: str = Form(...), kind: str = Form("pdf")):
        def go():
            if code not in M["all_codes"]():
                return err("الكود غير موجود في الكتالوج.")
            p = M["book_path"](code, kind == "solved")
            if p:
                os.remove(p)
            return {"ok": True}
        return guard(request, go)

    @app.post("/api/ctl/link")
    def ctl_link(request: Request, code: str = Form(...), kind: str = Form(...), url: str = Form("")):
        def go():
            if code not in M["all_codes"]() or kind not in ("pdf", "solved", "video"):
                return err("بيانات غير صحيحة.")
            if url and not url.startswith(("http://", "https://")):
                return err("الرابط يجب أن يبدأ بـ https://")
            links = M["load_links"]()
            entry = links.setdefault(code, {})
            if url:
                entry[kind] = url
            else:
                entry.pop(kind, None)
            M["save_links"](links)
            return {"ok": True}
        return guard(request, go)

    @app.get("/api/ctl/catalog")
    def ctl_catalog_get(request: Request, group: str):
        def go():
            if not M["_GROUP_RE"].match(group):
                return err("مجموعة غير صحيحة.")
            rows = M["subjects_for"](group[0], int(group[1]), int(group[-1]))
            return {"text": "\n".join("|".join(r) for r in rows)}
        return guard(request, go)

    @app.post("/api/ctl/catalog")
    def ctl_catalog_set(request: Request, group: str = Form(...), text: str = Form("")):
        def go():
            if not M["_GROUP_RE"].match(group):
                return err("مجموعة غير صحيحة.")
            ov = M["_catalog_override"]()
            rows = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 2 or not M["_SLUG_RE"].match(parts[0]) or not parts[1]:
                    return err(f"سطر غير صحيح: {line}")
                rows.append([parts[0], parts[1], parts[2] if len(parts) > 2 and parts[2] else parts[1]])
            if rows:
                ov[group] = rows
            else:
                ov.pop(group, None)
            M["_write_json"]("catalog.json", ov)
            return {"ok": True, "count": len(rows)}
        return guard(request, go)

    @app.get("/api/ctl/uploads")
    def ctl_uploads(request: Request):
        return guard(request, lambda: {"files": _uploads()})

    @app.post("/api/ctl/uploads/delete")
    def ctl_uploads_delete(request: Request, id: str = Form(...)):
        def go():
            ids = [f["id"] for f in _uploads()] if id == "all" else ([id] if re.fullmatch(r"[0-9a-f]{40}", id) else [])
            for i in ids:
                try:
                    os.remove(os.path.join(M["UPLOAD_DIR"], i + ".pdf"))
                except Exception:
                    pass
            return {"ok": True, "deleted": len(ids)}
        return guard(request, go)

    @app.get("/api/ctl/system")
    def ctl_system(request: Request):
        return guard(request, _sysinfo)

    @app.post("/api/ctl/ai-test")
    def ctl_ai_test(request: Request):
        return guard(request, lambda: {"results": M["run_ai_selftest"]()})
