"""persist.py — حفظ الكتب والإعدادات خارج السيرفر (Backblaze B2 أو أي تخزين متوافق مع S3) حتى لا تُمسح عند إعادة التشغيل.

ماذا يفعل؟
  • عند التشغيل: يستعيد من التخزين الكتب (pdf) وفهارس البحث (.cache) وروابط الكتب (links.json) والمواد (catalog.json) وقاعدة الإحصائيات.
  • كل 60 ثانية: يرفع أي كتاب أو ملف جديد/معدّل، ويحذف من التخزين ما حذفته أنت من لوحة التحكم.
  • كل ~5 دقائق: يأخذ نسخة احتياطية من قاعدة الإحصائيات.

التركيب: أضف في أول main.py (قبل import control):   import persist
المتغيرات في Render (Environment):
  B2_KEY_ID  B2_APPLICATION_KEY  B2_BUCKET  B2_ENDPOINT   (مثال: https://s3.us-west-004.backblazeb2.com)
وأضف سطر  boto3  في requirements.txt
إذا لم تُضبط المتغيرات فالملف لا يفعل شيئاً (آمن).
"""
import os
import re
import sqlite3
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
BOOKS_DIR = os.environ.get("BOOKS_DIR") or os.path.join(BASE, "books")   # نفس منطق main.py
CACHE_DIR = os.path.join(BOOKS_DIR, ".cache")


def _env(*names):
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


KEY_ID = _env("B2_KEY_ID", "S3_KEY_ID")
SECRET = _env("B2_APPLICATION_KEY", "S3_SECRET")
BUCKET = _env("B2_BUCKET", "S3_BUCKET")
ENDPOINT = _env("B2_ENDPOINT", "S3_ENDPOINT")
if ENDPOINT and not ENDPOINT.startswith("http"):
    ENDPOINT = "https://" + ENDPOINT
PREFIX = (_env("B2_PREFIX", "S3_PREFIX") or "smart-school").strip("/") + "/"
SYNC_EVERY = 60
ENABLED = bool(KEY_ID and SECRET and BUCKET and ENDPOINT)

KNOWN = set()      # ملفات نعرف أنها موجودة محلياً (لتمييز ما حذفته أنت عمداً)
_MISSING = {}      # عدّاد غياب الملف (نحذف من التخزين بعد غيابه مرتين متتاليتين)
_META_SMALL = ("analytics.db", "links.json", "catalog.json")


# ───────────── الاتصال ─────────────
def _make_client(fast=False):
    import boto3
    from botocore.config import Config
    m = re.search(r"s3\.([a-z0-9\-]+)\.", ENDPOINT)
    cfg = Config(signature_version="s3v4", s3={"addressing_style": "path"},
                 retries={"max_attempts": 2 if fast else 5, "mode": "standard"},
                 connect_timeout=10, read_timeout=30 if fast else 120)
    return boto3.client("s3", endpoint_url=ENDPOINT, aws_access_key_id=KEY_ID, aws_secret_access_key=SECRET,
                        region_name=m.group(1) if m else None, config=cfg)


def _tcfg():
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(max_concurrency=4, multipart_threshold=16 * 1024 * 1024, multipart_chunksize=8 * 1024 * 1024)


# ───────────── أدوات ─────────────
def _path(rel):
    return os.path.join(BOOKS_DIR, *rel.split("/"))


def local_files():
    out = {}
    try:
        for f in os.listdir(BOOKS_DIR):
            p = os.path.join(BOOKS_DIR, f)
            if os.path.isfile(p) and (f.lower().endswith(".pdf") or f in ("links.json", "catalog.json")):
                out[f] = p
        if os.path.isdir(CACHE_DIR):
            for f in os.listdir(CACHE_DIR):
                if f.endswith(".json"):
                    out[".cache/" + f] = os.path.join(CACHE_DIR, f)
    except Exception as e:
        print("[persist] listing local failed:", type(e).__name__, str(e)[:100])
    return out


def remote_list(c):
    out = {}
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
        for o in page.get("Contents", []):
            out[o["Key"][len(PREFIX):]] = o["Size"]
    return out


def _pull(c, rel):
    key = PREFIX + rel
    local = _path(rel)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    tmp = local + ".dl"
    head = c.head_object(Bucket=BUCKET, Key=key)
    c.download_file(BUCKET, key, tmp, Config=_tcfg())
    os.replace(tmp, local)
    mt = (head.get("Metadata") or {}).get("mtime", "")
    if mt.isdigit():
        os.utime(local, (int(mt), int(mt)))   # نحافظ على وقت التعديل الأصلي حتى يبقى فهرس البحث (OCR) صالحاً


def _push(c, rel):
    p = _path(rel)
    c.upload_file(p, BUCKET, PREFIX + rel,
                  ExtraArgs={"Metadata": {"mtime": str(int(os.path.getmtime(p)))}}, Config=_tcfg())


# ───────────── الاستعادة ─────────────
def _restore_small():
    """عند الاستيراد (قبل تشغيل لوحة التحكم): نستعيد الملفات الصغيرة فقط إذا كانت مفقودة."""
    c = _make_client(fast=True)
    for rel in _META_SMALL:
        if os.path.exists(_path(rel)):
            continue
        try:
            _pull(c, rel)
            print("[persist] restored", rel)
        except Exception as e:
            if "404" not in str(e) and "Not Found" not in str(e):
                print("[persist] small restore:", rel, type(e).__name__, str(e)[:80])


def _restore_all(c):
    remote = remote_list(c)

    def prio(rel):
        return 0 if rel in ("links.json", "catalog.json") else 1 if rel.startswith(".cache/") else 2
    for rel in sorted(remote, key=lambda r: (prio(r), r)):
        if rel == "analytics.db":
            continue
        p = _path(rel)
        if os.path.exists(p) and os.path.getsize(p) == remote[rel]:
            continue
        try:
            _pull(c, rel)
            print(f"[persist] restored {rel} ({remote[rel]} bytes)")
        except Exception as e:
            print("[persist] restore failed:", rel, type(e).__name__, str(e)[:100])
    return remote


# ───────────── المزامنة ─────────────
def _sync(c):
    lf = local_files()
    rf = remote_list(c)
    now = time.time()
    for rel in list(rf):   # ما فشلت استعادته سابقاً
        if rel not in lf and rel not in KNOWN and rel != "analytics.db":
            try:
                _pull(c, rel)
                KNOWN.add(rel)
                lf[rel] = _path(rel)
                print("[persist] restored (retry)", rel)
            except Exception as e:
                print("[persist] retry restore failed:", rel, str(e)[:80])
    for rel, p in list(lf.items()):
        try:
            if now - os.path.getmtime(p) < 10:
                continue   # ما زال يُكتب
            if rf.get(rel) != os.path.getsize(p):
                _push(c, rel)
                print("[persist] uploaded", rel)
            KNOWN.add(rel)
        except Exception as e:
            print("[persist] upload failed:", rel, type(e).__name__, str(e)[:100])
    deleted = 0
    for rel in list(KNOWN):
        if rel in lf:
            _MISSING.pop(rel, None)
            continue
        if rel.startswith(".cache/") or rel not in rf:
            KNOWN.discard(rel)
            _MISSING.pop(rel, None)
            continue
        _MISSING[rel] = _MISSING.get(rel, 0) + 1
        if _MISSING[rel] >= 2 and deleted < 5 and os.path.isdir(BOOKS_DIR):   # حماية من الحذف الجماعي الخاطئ
            c.delete_object(Bucket=BUCKET, Key=PREFIX + rel)
            KNOWN.discard(rel)
            _MISSING.pop(rel, None)
            deleted += 1
            print("[persist] deleted from storage", rel)


def _backup_db(c):
    p = os.path.join(BOOKS_DIR, "analytics.db")
    if not os.path.exists(p):
        return
    snap = os.path.join(tempfile.gettempdir(), "analytics.snap")
    src = sqlite3.connect(p, timeout=5)
    dst = sqlite3.connect(snap)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    c.upload_file(snap, BUCKET, PREFIX + "analytics.db")


def _run():
    try:
        c = _make_client()
        _restore_all(c)
    except Exception as e:
        print("[persist] cannot reach storage, sync disabled:", type(e).__name__, str(e)[:150])
        return
    KNOWN.clear()
    KNOWN.update(local_files())
    n = 0
    while True:
        try:
            _sync(c)
            if n % 5 == 0:
                _backup_db(c)
            n += 1
        except Exception as e:
            print("[persist] sync error:", type(e).__name__, str(e)[:150])
        time.sleep(SYNC_EVERY)


def _start():
    if not ENABLED:
        print("[persist] off: B2_KEY_ID / B2_APPLICATION_KEY / B2_BUCKET / B2_ENDPOINT not all set")
        return
    try:
        import boto3  # noqa: F401
    except ImportError:
        print("[persist] boto3 missing: add the line  boto3  to requirements.txt")
        return
    os.makedirs(BOOKS_DIR, exist_ok=True)
    try:
        _restore_small()
    except Exception as e:
        print("[persist] small restore skipped:", type(e).__name__, str(e)[:120])
    threading.Thread(target=_run, daemon=True, name="persist").start()
    print("[persist] on, bucket:", BUCKET)


if not getattr(threading, "_persist_started", False):
    threading._persist_started = True
    _start()
