"""RuangNode API — website + gerbang Access Token (fase website).

Tanpa Access Token yang benar, tidak ada halaman yang terlihat dan tidak ada
request API yang dilayani. /api/health adalah satu-satunya endpoint publik.
"""

import hashlib
import hmac
import os
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
    docs_url=None,      # dimatikan: dokumentasi publik tidak boleh bocor
    redoc_url=None,
    openapi_url=None,
)

# ----------------------------------------------------------------------------
# Data katalog (fase website: statis; modul dibangun menyusul per kategori)
# ----------------------------------------------------------------------------
CATEGORIES = [
    {"name": "Download", "planned": 29, "status": "segera",
     "desc": "Unduh video/audio/foto dari berbagai platform — diawali modul TikTok (tanpa watermark, MP3, carousel selektif, GIF)."},
    {"name": "AI", "planned": 21, "status": "segera",
     "desc": "Chat AI, generator & editor gambar, text-to-speech, text-to-video."},
    {"name": "Tools", "planned": 43, "status": "segera",
     "desc": "OCR, konverter, QR, shortener, screenshot web, temp mail, info PDF."},
    {"name": "Stalk", "planned": 26, "status": "segera",
     "desc": "Intip profil publik dari puluhan platform sosial & game."},
    {"name": "Pterodactyl", "planned": 18, "status": "segera",
     "desc": "Kelola panel game server: server, user, suspend, restart."},
    {"name": "RE", "planned": 12, "status": "segera",
     "desc": "Reverse engineering: analisis APK/IPA, deobfuscator, SSL pinning inspector."},
    {"name": "Info", "planned": 8, "status": "segera",
     "desc": "IP lookup, DNS, WHOIS, cek SSL, status website."},
    {"name": "Maker", "planned": 6, "status": "segera",
     "desc": "Generator teks & gambar: brat, carbon snippet, quote canvas."},
]
PLANNED_TOTAL = sum(c["planned"] for c in CATEGORIES)
ACTIVE_ENDPOINTS = [
    {"method": "GET", "path": "/api/health", "desc": "Cek liveness (publik, tanpa token)"},
    {"method": "GET", "path": "/api", "desc": "Katalog modul (butuh token)"},
]

# ----------------------------------------------------------------------------
# Helper keamanan
# ----------------------------------------------------------------------------
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
            token = auth[7:].strip()
    if not token:
        token = request.query_params.get("token")
    return token


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
# Halaman web (semuanya di balik gerbang token)
# ----------------------------------------------------------------------------
def lock_context(request: Request, error: str | None = None) -> dict:
    return {"request": request, "error": error}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if page_allowed(request):
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "categories": CATEGORIES,
                "planned_total": PLANNED_TOTAL,
                "active": ACTIVE_ENDPOINTS,
                "phase": "Fase Website",
            },
        )
    return templates.TemplateResponse(request, "lock.html", lock_context(request))


@app.post("/unlock", response_class=HTMLResponse)
async def unlock(request: Request, token: str = Form(default="")):
    t = (token or "").strip()
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
        "lock.html",
        lock_context(request, error="Token salah. Periksa kembali Access Token kamu."),
        status_code=401,
    )


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/docs", response_class=HTMLResponse)
async def docs(request: Request):
    if not page_allowed(request):
        return templates.TemplateResponse(request, "lock.html", lock_context(request))
    return templates.TemplateResponse(
        request,
        "docs.html",
        {
            "request": request,
            "categories": CATEGORIES,
            "planned_total": PLANNED_TOTAL,
            "active": ACTIVE_ENDPOINTS,
        },
    )


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
