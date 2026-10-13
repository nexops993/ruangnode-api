"""RuangNode API — website + gerbang Access Token.

Beranda bersifat publik (landing page + tombol Login). Dashboard, dokumentasi,
dan semua request API hanya bisa diakses dengan Access Token yang benar.
Satu-satunya endpoint API publik adalah /api/health (cek liveness).
"""

import hmac
import os

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates

from . import security
from .routers import download as download_router
from .routers import media_dl as media_dl_router
from .routers import social_dl as social_dl_router
from .routers import host_dl as host_dl_router
from .routers import ai as ai_router
from .routers import tools as tools_router
from .security import (
    COOKIE_NAME,
    SESSION_DAYS,
    clean_token,
    clear_fails,
    client_ip,
    is_locked,
    make_session,
    page_allowed,
    record_fail,
    require_api_token,
)

if not security.ACCESS_TOKEN or not security.SESSION_SECRET:
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
app.include_router(download_router.router)
app.include_router(media_dl_router.router)
app.include_router(social_dl_router.router)
app.include_router(host_dl_router.router)
app.include_router(ai_router.router)
app.include_router(tools_router.router)

# ----------------------------------------------------------------------------
# Data katalog (modul dibangun per kategori sampai tuntas)
# ----------------------------------------------------------------------------
CATEGORIES = [
    {"name": "Download", "icon": "download", "planned": 29, "status": "aktif",
     "desc": "Unduh video, audio, dan foto dari berbagai platform — diawali TikTok: tanpa watermark, MP3, carousel selektif, GIF."},
    {"name": "AI", "icon": "ai", "planned": 21, "status": "aktif",
     "desc": "Chat AI, generator dan editor gambar, text-to-speech, text-to-video."},
    {"name": "Tools", "icon": "tools", "planned": 43, "status": "aktif",
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
    {"method": "GET", "path": "/api/download/tiktok", "desc": "Info unduhan TikTok: video tanpa watermark, MP3, foto carousel"},
    {"method": "GET", "path": "/api/download/tiktok/file", "desc": "Unduh file TikTok: video/MP3/foto pilihan (ZIP)/GIF dari Live Photo"},
    {"method": "GET", "path": "/api/download/ytmp4", "desc": "Info + tautan video YouTube (terbatas: dinding bot YouTube, metadata via oEmbed)"},
    {"method": "GET", "path": "/api/download/ytmp3", "desc": "Info + tautan audio YouTube (terbatas: metadata via oEmbed)"},
    {"method": "GET", "path": "/api/download/ytmp3/file", "desc": "Konversi MP3 YouTube via server (butuh cookie sesi YouTube)"},
    {"method": "GET", "path": "/api/download/ytpost", "desc": "Detail postingan komunitas YouTube: teks, gambar, author"},
    {"method": "GET", "path": "/api/download/bilibili", "desc": "Info + tautan video Bilibili (terbatas: WAF Bilibili memblokir IP server)"},
    {"method": "GET", "path": "/api/download/dailymotion", "desc": "Info + tautan video Dailymotion"},
    {"method": "GET", "path": "/api/download/vimeo", "desc": "Info video Vimeo (terbatas: metadata via oEmbed, file butuh login)"},
    {"method": "GET", "path": "/api/download/soundcloud", "desc": "Info + tautan stream audio SoundCloud"},
    {"method": "GET", "path": "/api/download/douyin", "desc": "Info + tautan video Douyin (terbatas: butuh cookie sesi Douyin)"},
    {"method": "GET", "path": "/api/download/instagram", "desc": "Info + tautan video/foto Instagram publik"},
    {"method": "GET", "path": "/api/download/facebook", "desc": "Info + tautan video Facebook publik"},
    {"method": "GET", "path": "/api/download/x", "desc": "Info + tautan video X/Twitter (jalur yt-dlp)"},
    {"method": "GET", "path": "/api/download/x2", "desc": "Info + tautan video X/Twitter (jalur scraping halaman resmi)"},
    {"method": "GET", "path": "/api/download/snackvideo", "desc": "Info + tautan video SnackVideo"},
    {"method": "GET", "path": "/api/download/capcut", "desc": "Info + video preview template CapCut"},
    {"method": "GET", "path": "/api/download/pinterest", "desc": "Info + media pin Pinterest (video/gambar penuh)"},
    {"method": "GET", "path": "/api/download/videy", "desc": "Tautan MP4 langsung Videy (terverifikasi)"},
    {"method": "GET", "path": "/api/download/xnxx", "desc": "Info + tautan video XNXX"},
    {"method": "GET", "path": "/api/download/mediafire", "desc": "Tautan unduh langsung MediaFire (terverifikasi)"},
    {"method": "GET", "path": "/api/download/gofile", "desc": "Daftar file + tautan langsung GoFile (API resmi + token akun tamu)"},
    {"method": "GET", "path": "/api/download/pixeldrain", "desc": "Info file + tautan unduh Pixeldrain (API resmi)"},
    {"method": "GET", "path": "/api/download/krakenfiles", "desc": "Metadata KrakenFiles (terbatas: unduhan diproteksi captcha)"},
    {"method": "GET", "path": "/api/download/uploadhaven", "desc": "Metadata UploadHaven (terbatas: unduhan butuh timer browser)"},
    {"method": "GET", "path": "/api/download/terabox", "desc": "Daftar file share TeraBox (terbatas: butuh cookie TERABOX_COOKIES)"},
    {"method": "GET", "path": "/api/download/mega", "desc": "Metadata tautan publik Mega via library mega.py (terbatas: file E2E)"},
    {"method": "GET", "path": "/api/download/scribd", "desc": "Scribd (gagal: dinding Client Challenge JS)"},
    {"method": "GET", "path": "/api/download/mcpe", "desc": "Metadata add-on MCPEDL (terbatas: file via API internal)"},
    {"method": "GET", "path": "/api/download/github", "desc": "Info repo + arsip zip/tar GitHub (API resmi)"},
    {"method": "GET", "path": "/api/download/applemusic", "desc": "Metadata + preview 30 detik Apple Music (iTunes Lookup)"},
    {"method": "GET", "path": "/api/ai/chatgpt", "desc": "Chat AI model GPT (sesi chat_id, via gateway 9Router)"},
    {"method": "GET", "path": "/api/ai/mistral", "desc": "Chat AI model Mistral (via Cloudflare Workers AI)"},
    {"method": "GET", "path": "/api/ai/qwen3", "desc": "Chat AI model Qwen3 (via gateway 9Router)"},
    {"method": "GET", "path": "/api/ai/perplexity", "desc": "Jawab gaya mesin riset (note: bukan pencarian web langsung)"},
    {"method": "GET", "path": "/api/ai/kuroneko", "desc": "RuangNode Assistant — persona ramah Bahasa Indonesia (sesi)"},
    {"method": "GET", "path": "/api/ai/feeb", "desc": "Feel Better Bot — persona suportif/empatik (sesi)"},
    {"method": "GET", "path": "/api/ai/notrack", "desc": "Chat tanpa jejak, persona bisa dipilih"},
    {"method": "GET", "path": "/api/ai/nova", "desc": "Chat AI umum yang cepat (Gemma)"},
    {"method": "GET", "path": "/api/ai/aiseek", "desc": "Chat multi-model (model=...) atau gambar (action=image)"},
    {"method": "GET", "path": "/api/ai/flux", "desc": "Text-to-image Flux (Cloudflare) — balas gambar langsung"},
    {"method": "GET", "path": "/api/ai/aiart", "desc": "Generator gambar artistik (Flux)"},
    {"method": "GET", "path": "/api/ai/toonmix", "desc": "Gambar gaya kartun (T2I; I2I terbatas — tanpa model img2img)"},
    {"method": "GET", "path": "/api/ai/nanobanana", "desc": "Edit gambar via prompt (TERBATAS: belum ada model image-edit)"},
    {"method": "GET", "path": "/api/ai/animetoreal", "desc": "Anime ke realistis (TERBATAS: belum ada model img2img)"},
    {"method": "GET", "path": "/api/ai/aifilter", "desc": "Filter gambar lokal (Pillow): grayscale/blur/edge/dll"},
    {"method": "GET", "path": "/api/ai/img2prompt", "desc": "Gambar menjadi prompt generator (model vision)"},
    {"method": "GET", "path": "/api/ai/tts", "desc": "Text-to-speech Bahasa Indonesia (edge-tts) — balas audio MP3"},
    {"method": "GET", "path": "/api/ai/text2vid", "desc": "Text-to-video (TERBATAS: animasi zoom dari gambar, bukan model video)"},
    {"method": "GET", "path": "/api/ai/bypassai", "desc": "Parafrase teks agar terdengar natural (dibantu model)"},
    {"method": "GET", "path": "/api/ai/detectai", "desc": "Estimasi teks AI dari heuristik lokal (bukan Grammarly)"},
    {"method": "GET", "path": "/api/ai/zeta", "desc": "Audio menjadi suara 'waifu' (pitch-shift lokal ffmpeg)"},
    {"method": "GET", "path": "/api/tools/text2base64", "desc": "Teks menjadi Base64"},
    {"method": "GET", "path": "/api/tools/base64totext", "desc": "Base64 menjadi teks"},
    {"method": "GET", "path": "/api/tools/text2binner", "desc": "Teks menjadi biner"},
    {"method": "GET", "path": "/api/tools/binner2text", "desc": "Biner menjadi teks"},
    {"method": "GET", "path": "/api/tools/text2qr", "desc": "Teks menjadi QR code (PNG)"},
    {"method": "GET", "path": "/api/tools/nik", "desc": "Parser NIK Indonesia: wilayah, tanggal lahir, jenis kelamin"},
    {"method": "GET", "path": "/api/tools/imageconvert", "desc": "Konversi/resize gambar (format, dimensi, kualitas)"},
    {"method": "GET", "path": "/api/tools/hd", "desc": "Tingkatkan gambar v1 (upscale lokal, terbatas)"},
    {"method": "GET", "path": "/api/tools/hd2", "desc": "Tingkatkan gambar v2 (upscale lokal, terbatas)"},
    {"method": "GET", "path": "/api/tools/hd3", "desc": "Tingkatkan gambar v3 (upscale lokal, terbatas)"},
    {"method": "GET", "path": "/api/tools/hd4", "desc": "Tingkatkan gambar v4 (upscale lokal, terbatas)"},
    {"method": "GET", "path": "/api/tools/remini", "desc": "Enhance gambar lokal (bukan aplikasi Remini asli)"},
    {"method": "GET", "path": "/api/tools/pdf-info", "desc": "Info PDF: halaman, metadata, ukuran"},
    {"method": "GET", "path": "/api/tools/pdf-merge", "desc": "Gabung beberapa PDF menjadi satu"},
    {"method": "GET", "path": "/api/tools/pdf-split", "desc": "Pisahkan halaman PDF (pages=1-3,5)"},
    {"method": "GET", "path": "/api/tools/pdf-compress", "desc": "Kompresi ringan PDF (terbatas)"},
    {"method": "GET", "path": "/api/tools/saveweb", "desc": "Arsip halaman web menjadi ZIP"},
    {"method": "GET", "path": "/api/tools/ocr", "desc": "OCR gambar ke teks (tesseract, ind/eng)"},
    {"method": "GET", "path": "/api/tools/ceknsfw", "desc": "Penilaian keamanan gambar (estimasi model vision)"},
    {"method": "GET", "path": "/api/tools/translate", "desc": "Terjemahan teks (source/target bahasa)"},
    {"method": "GET", "path": "/api/tools/ytsum", "desc": "Ringkasan video YouTube dari transkrip"},
    {"method": "GET", "path": "/api/tools/findsong", "desc": "Kenali lagu dari URL audio (Shazam)"},
    {"method": "GET", "path": "/api/tools/removevocal", "desc": "Kurangi vokal audio (efek karaoke ffmpeg, terbatas)"},
    {"method": "GET", "path": "/api/tools/tinyurl", "desc": "Shortener TinyURL (API resmi)"},
    {"method": "GET", "path": "/api/tools/cleanuri", "desc": "Shortener CleanURI (API resmi)"},
    {"method": "GET", "path": "/api/tools/dagd", "desc": "Shortener da.gd (API resmi)"},
    {"method": "GET", "path": "/api/tools/spoome", "desc": "Shortener spoo.me (terbatas: butuh akun)"},
    {"method": "GET", "path": "/api/tools/pagespeed", "desc": "Skor PageSpeed Google (terbatas: kuota tanpa key)"},
    {"method": "GET", "path": "/api/tools/proxy", "desc": "Daftar proxy publik (feed GeoNode)"},
    {"method": "GET", "path": "/api/tools/genmail", "desc": "Temp mail: domains/create/inbox/read (mail.tm)"},
    {"method": "GET", "path": "/api/tools/tempmail2", "desc": "Temp mail v2: create/inbox/read (mail.tm)"},
    {"method": "GET", "path": "/api/tools/tempmail3", "desc": "Temp mail v3: create/inbox (mail.tm)"},
    {"method": "GET", "path": "/api/tools/patreon-search", "desc": "Cari kreator Patreon (API publik)"},
    {"method": "GET", "path": "/api/tools/patreon-creator", "desc": "Detail kreator Patreon"},
    {"method": "GET", "path": "/api/tools/patreon-post", "desc": "Detail post Patreon"},
    {"method": "GET", "path": "/api/tools/patreon-posts", "desc": "Daftar post kreator Patreon"},
    {"method": "GET", "path": "/api/tools/ssweb", "desc": "Screenshot halaman web (Chromium lokal, PNG)"},
    {"method": "GET", "path": "/api/tools/ngl", "desc": "Kirim pesan NGL (alur publik resmi)"},
    {"method": "GET", "path": "/api/tools/cekresi", "desc": "Cek resi (gagal: tidak ada API publik tanpa key)"},
    {"method": "GET", "path": "/api/tools/xlcheck", "desc": "Cek kuota XL/Axis (gagal: butuh OTP pemilik nomor)"},
    {"method": "GET", "path": "/api/tools/xlcircle", "desc": "Cek XL Circle (gagal: butuh OTP pemilik nomor)"},
    {"method": "GET", "path": "/api/tools/spamotp", "desc": "DITOLAK: spam OTP adalah pelecehan (403)"},
    {"method": "GET", "path": "/api/tools/alightmotion", "desc": "DITOLAK: aktivasi premium ilegal (403)"},
]

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
        "phase": "Fase Endpoint",
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
    ip = client_ip(request)
    if is_locked(ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request,
             "error": "Terlalu banyak percobaan gagal dari IP ini. Coba lagi dalam 15 menit."},
            status_code=429,
        )
    t = clean_token(token)
    if t and hmac.compare_digest(t, security.ACCESS_TOKEN):
        clear_fails(ip)
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
    record_fail(ip)
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
            "phase": "Fase Endpoint",
            "categories": CATEGORIES,
            "planned_total": PLANNED_TOTAL,
            "active_endpoints": ACTIVE_ENDPOINTS,
            "note": "Modul per kategori dibangun menyusul, satu kategori tuntas 100% sebelum pindah.",
        },
    }
