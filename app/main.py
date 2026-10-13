"""RuangNode API — website + gerbang Access Token (fase website).

Beranda bersifat publik (landing page + tombol Login). Dashboard, dokumentasi,
dan semua request API hanya bisa diakses dengan Access Token yang benar.
Satu-satunya endpoint API publik adalah /api/health (cek liveness).
"""

import hashlib
import hmac
import os
import re
import time

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates

ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
SESSION_DAYS = 7
COOKIE_NAME = "rn_session"

if not ACCESS_TOKEN or not SESSION_SECRET:
    raise SystemExit(
        "ACCESS_TOKEN dan SESSION_SECRET wajib di-set via environment "
        "(lihat /etc/ruangnode-api/env di VPS)."
    )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

app = FastAPI(
    title="RuangNode API",
    docs_url=None,      # dokumentasi bawaan FastAPI dimatikan
    redoc_url=None,
    openapi_url=None,
)

# ----------------------------------------------------------------------------
# Data katalog (fase website: statis; modul dibangun menyusul per kategori)
# ----------------------------------------------------------------------------
CATEGORIES = [
    {"name": "Download", "icon": "download", "planned": 29, "status": "segera",
     "desc": "Unduh video, audio, dan foto dari berbagai platform — diawali TikTok: tanpa watermark, MP3, carousel selektif, GIF."},
    {"name": "AI", "icon": "ai", "planned": 21, "status": "segera",
     "desc": "Chat AI, generator dan editor gambar, text-to-speech, text-to-video."},
    {"name": "Tools", "icon": "tools", "planned": 43, "status": "segera",
     "desc": "OCR, konverter, QR, shortener, screenshot web, temp mail, olah PDF."},
    {"name": "Stalk", "icon": "stalk", "planned": 26, "status": "segera",
     "desc": "Lihat profil publik dari puluhan platform sosial dan game."},
    {"name": "Pterodactyl", "icon": "ptero", "planned": 18, "status": "segera",
     "desc": "Kelola panel game server: server, user, suspend, restart."},
    {"name": "RE", "icon": "re", "planned": 12, "status": "segera",
     "desc": "Reverse engineering: analisis APK/IPA, deobfuscator, inspeksi SSL pinning."},
    {"name": "Info", "icon": "info", "planned": 8, "status": "segera",
     "desc": "IP lookup, DNS, WHOIS, cek SSL, status website."},
    {"name": "Maker", "icon": "maker", "planned": 6, "status": "segera",
     "desc": "Generator teks dan gambar: brat, carbon snippet, quote canvas."},
]
PLANNED_TOTAL = sum(c["planned"] for c in CATEGORIES)
ACTIVE_ENDPOINTS = [
    {"method": "GET", "path": "/api/health", "desc": "Cek liveness (publik, tanpa token)"},
    {"method": "GET", "path": "/api", "desc": "Katalog modul (butuh token)"},
]

# ----------------------------------------------------------------------------
# Helper keamanan
# ----------------------------------------------------------------------------
def clean_token(value: str | None) -> str:
    """Buang SEMUA whitespace (spasi, baris baru, tab) dari token.

    Token yang disalin dari chat sering membawa baris baru di tengah atau
    spasi tak terlihat; membersihkan semuanya membuat tempelan apa adanya
    dari WhatsApp tetap terbaca benar.
    """
    return re.sub(r"\s+", "", value or "")


def _sign(ts: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts)}"


def valid_session(cookie_val: str | None) -> bool:
    if not cookie_val or "." not in cookie_val:
        return False
    ts, sig = cookie_val.split(".", 1)
    if not ts.isdigit():
        return False
    if not hmac.compare_digest(_sign(ts), sig):
        return False
    if int(time.time()) - int(ts) > SESSION_DAYS * 24 * 3600:
        return False
    return True


def page_allowed(request: Request) -> bool:
    return valid_session(request.cookies.get(COOKIE_NAME))


def token_from_request(request: Request) -> str | None:
    token = request.headers.get("X-Access-Token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        token = request.query_params.get("token")
    return clean_token(token) or None


def require_api_token(request: Request) -> None:
    token = token_from_request(request)
    if not token or not hmac.compare_digest(token, ACCESS_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Access Token tidak ada atau salah. "
                   "Sertakan via header X-Access-Token, Authorization: Bearer <token>, "
                   "atau query ?token=.",
        )


# ----------------------------------------------------------------------------
# Envelope & error handler
# ----------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api"):
        return JSONResponse(
            {"status": False, "creator": "RuangNode", "message": exc.detail},
            status_code=exc.status_code,
        )
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)


# ----------------------------------------------------------------------------
# Halaman web
# ----------------------------------------------------------------------------
def catalog_context(request: Request) -> dict:
    return {
        "request": request,
        "categories": CATEGORIES,
        "planned_total": PLANNED_TOTAL,
        "active": ACTIVE_ENDPOINTS,
        "phase": "Fase Website",
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if page_allowed(request):
        return templates.TemplateResponse(request, "dashboard.html", catalog_context(request))
    return templates.TemplateResponse(request, "landing.html", catalog_context(request))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if page_allowed(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


async def _do_login(request: Request, token: str):
    t = clean_token(token)
    if t and hmac.compare_digest(t, ACCESS_TOKEN):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE_NAME,
            make_session(),
            max_age=SESSION_DAYS * 24 * 3600,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "error": "Token salah. Periksa kembali Access Token kamu."},
        status_code=401,
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, token: str = Form(default="")):
    return await _do_login(request, token)


@app.post("/unlock", response_class=HTMLResponse)
async def unlock_alias(request: Request, token: str = Form(default="")):
    # Alias lama (fase layar kunci) — diarahkan ke logika login yang sama.
    return await _do_login(request, token)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/docs", response_class=HTMLResponse)
async def docs(request: Request):
    if not page_allowed(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "docs.html", catalog_context(request))


# ----------------------------------------------------------------------------
# API JSON
# ----------------------------------------------------------------------------
@app.get("/api/health")
async def api_health():
    return {"status": "ok"}


@app.get("/api")
async def api_catalog(request: Request):
    require_api_token(request)
    return {
        "status": True,
        "creator": "RuangNode",
        "result": {
            "phase": "Fase Website",
            "categories": CATEGORIES,
            "planned_total": PLANNED_TOTAL,
            "active_endpoints": ACTIVE_ENDPOINTS,
            "note": "Modul per kategori dibangun menyusul, satu kategori tuntas 100% sebelum pindah.",
        },
    }
