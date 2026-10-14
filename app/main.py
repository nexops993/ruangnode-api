"""RuangNode API — website + gerbang Access Token.

Beranda bersifat publik (landing page + tombol Login). Dashboard, dokumentasi,
dan semua request API hanya bisa diakses dengan Access Token yang benar.
Satu-satunya endpoint API publik adalah /api/health (cek liveness).
"""

import hmac
import json
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
from .routers import stalk as stalk_router
from .routers import ptero as ptero_router
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
app.include_router(stalk_router.router)
app.include_router(ptero_router.router)

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
    {"name": "Stalk", "icon": "stalk", "planned": 26, "status": "aktif",
     "desc": "Lihat profil publik dari puluhan platform sosial dan game."},
    {"name": "Pterodactyl", "icon": "ptero", "planned": 18, "status": "aktif",
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
    {"method": "GET", "path": "/api/health", "desc": "Mengecek apakah server API hidup dan merespons"},
    {"method": "GET", "path": "/api", "desc": "Melihat katalog semua modul dan endpoint yang tersedia"},
    {"method": "GET", "path": "/api/download/tiktok", "desc": "Mengambil info unduhan TikTok: video tanpa watermark, MP3, dan foto carousel"},
    {"method": "GET", "path": "/api/download/tiktok/file", "desc": "Mengunduh file TikTok pilihan: video, MP3, foto (bisa ZIP), atau GIF"},
    {"method": "GET", "path": "/api/download/ytmp4", "desc": "Mengambil info dan tautan unduhan video YouTube"},
    {"method": "GET", "path": "/api/download/ytmp3", "desc": "Mengambil info dan tautan audio dari video YouTube"},
    {"method": "GET", "path": "/api/download/ytmp3/file", "desc": "Mengonversi video YouTube menjadi file MP3"},
    {"method": "GET", "path": "/api/download/cobalt/file", "desc": "Mengunduh media lewat mesin Cobalt (relay file server)"},
    {"method": "GET", "path": "/api/download/ytpost", "desc": "Melihat detail postingan komunitas YouTube: teks, gambar, dan penulis"},
    {"method": "GET", "path": "/api/download/bilibili", "desc": "Mengambil info dan tautan unduhan video Bilibili"},
    {"method": "GET", "path": "/api/download/dailymotion", "desc": "Mengambil info dan tautan unduhan video Dailymotion"},
    {"method": "GET", "path": "/api/download/vimeo", "desc": "Mengambil info video Vimeo"},
    {"method": "GET", "path": "/api/download/soundcloud", "desc": "Mengambil info dan tautan stream lagu SoundCloud"},
    {"method": "GET", "path": "/api/download/douyin", "desc": "Mengambil info dan tautan unduhan video Douyin"},
    {"method": "GET", "path": "/api/download/instagram", "desc": "Mengambil info dan tautan unduhan foto/video Instagram publik"},
    {"method": "GET", "path": "/api/download/facebook", "desc": "Mengambil info dan tautan unduhan video Facebook publik"},
    {"method": "GET", "path": "/api/download/x", "desc": "Mengambil info dan tautan unduhan video X (Twitter)"},
    {"method": "GET", "path": "/api/download/x2", "desc": "Mengambil info dan tautan unduhan video X (Twitter) lewat jalur alternatif"},
    {"method": "GET", "path": "/api/download/snackvideo", "desc": "Mengambil info dan tautan unduhan video SnackVideo"},
    {"method": "GET", "path": "/api/download/capcut", "desc": "Mengambil video pratinjau template CapCut"},
    {"method": "GET", "path": "/api/download/pinterest", "desc": "Mengambil media pin Pinterest: video atau gambar resolusi penuh"},
    {"method": "GET", "path": "/api/download/videy", "desc": "Mengambil tautan MP4 langsung dari Videy"},
    {"method": "GET", "path": "/api/download/xnxx", "desc": "Mengambil info dan tautan unduhan video XNXX"},
    {"method": "GET", "path": "/api/download/mediafire", "desc": "Mengambil tautan unduh langsung berkas MediaFire"},
    {"method": "GET", "path": "/api/download/gofile", "desc": "Melihat daftar berkas dan tautan unduhan GoFile"},
    {"method": "GET", "path": "/api/download/pixeldrain", "desc": "Mengambil info berkas dan tautan unduhan Pixeldrain"},
    {"method": "GET", "path": "/api/download/krakenfiles", "desc": "Melihat info berkas KrakenFiles: nama, ukuran, dan jumlah unduhan"},
    {"method": "GET", "path": "/api/download/uploadhaven", "desc": "Melihat info berkas UploadHaven: nama dan ukuran"},
    {"method": "GET", "path": "/api/download/terabox", "desc": "Melihat daftar berkas dari tautan berbagi TeraBox"},
    {"method": "GET", "path": "/api/download/mega", "desc": "Melihat info berkas dari tautan publik Mega: nama dan ukuran"},
    {"method": "GET", "path": "/api/download/scribd", "desc": "Mengambil isi dokumen Scribd"},
    {"method": "GET", "path": "/api/download/mcpe", "desc": "Melihat info add-on Minecraft dari MCPEDL: deskripsi dan gambar sampul"},
    {"method": "GET", "path": "/api/download/github", "desc": "Melihat info repositori GitHub dan mengunduh arsip ZIP/TAR-nya"},
    {"method": "GET", "path": "/api/download/applemusic", "desc": "Melihat info lagu Apple Music dan memutar pratinjau 30 detik"},
    {"method": "GET", "path": "/api/ai/chatgpt", "desc": "Mengobrol dengan AI model GPT, bisa bersesi"},
    {"method": "GET", "path": "/api/ai/mistral", "desc": "Mengobrol dengan AI model Mistral"},
    {"method": "GET", "path": "/api/ai/qwen3", "desc": "Mengobrol dengan AI model Qwen3"},
    {"method": "GET", "path": "/api/ai/perplexity", "desc": "Bertanya dan menerima jawaban ringkas bergaya mesin riset"},
    {"method": "GET", "path": "/api/ai/kuroneko", "desc": "Mengobrol dengan RuangNode Assistant yang ramah berbahasa Indonesia"},
    {"method": "GET", "path": "/api/ai/feeb", "desc": "Teman curhat AI yang suportif dan menenangkan"},
    {"method": "GET", "path": "/api/ai/notrack", "desc": "Mengobrol dengan AI tanpa jejak, persona bisa dipilih"},
    {"method": "GET", "path": "/api/ai/nova", "desc": "Mengobrol cepat dengan AI serbaguna"},
    {"method": "GET", "path": "/api/ai/aiseek", "desc": "Mengobrol dengan AI berbagai model, atau membuat gambar"},
    {"method": "GET", "path": "/api/ai/flux", "desc": "Membuat gambar dari deskripsi teks"},
    {"method": "GET", "path": "/api/ai/aiart", "desc": "Membuat gambar artistik dari deskripsi teks"},
    {"method": "GET", "path": "/api/ai/toonmix", "desc": "Membuat gambar bergaya kartun dari teks atau foto"},
    {"method": "GET", "path": "/api/ai/nanobanana", "desc": "Mengedit gambar mengikuti perintah teks"},
    {"method": "GET", "path": "/api/ai/animetoreal", "desc": "Mengubah gambar anime menjadi gaya realistis"},
    {"method": "GET", "path": "/api/ai/aifilter", "desc": "Memberi filter pada gambar: grayscale, blur, emboss, dan lainnya"},
    {"method": "GET", "path": "/api/ai/img2prompt", "desc": "Mengubah gambar menjadi prompt teks untuk generator gambar"},
    {"method": "GET", "path": "/api/ai/tts", "desc": "Mengubah teks menjadi suara (audio MP3) berbahasa Indonesia"},
    {"method": "GET", "path": "/api/ai/text2vid", "desc": "Membuat video pendek dari deskripsi teks"},
    {"method": "GET", "path": "/api/ai/bypassai", "desc": "Menulis ulang teks agar terdengar lebih natural seperti tulisan manusia"},
    {"method": "GET", "path": "/api/ai/detectai", "desc": "Memeriksa apakah sebuah teks kemungkinan ditulis AI"},
    {"method": "GET", "path": "/api/ai/zeta", "desc": "Mengubah suara audio menjadi suara karakter anime"},
    {"method": "GET", "path": "/api/tools/text2base64", "desc": "Mengubah teks menjadi kode Base64"},
    {"method": "GET", "path": "/api/tools/base64totext", "desc": "Mengubah kode Base64 kembali menjadi teks"},
    {"method": "GET", "path": "/api/tools/text2binner", "desc": "Mengubah teks menjadi kode biner"},
    {"method": "GET", "path": "/api/tools/binner2text", "desc": "Mengubah kode biner kembali menjadi teks"},
    {"method": "GET", "path": "/api/tools/text2qr", "desc": "Membuat QR code dari teks atau tautan"},
    {"method": "GET", "path": "/api/tools/nik", "desc": "Membaca isi NIK: wilayah, tanggal lahir, dan jenis kelamin"},
    {"method": "GET", "path": "/api/tools/imageconvert", "desc": "Mengonversi dan mengubah ukuran gambar (format, dimensi, kualitas)"},
    {"method": "GET", "path": "/api/tools/hd", "desc": "Meningkatkan resolusi dan ketajaman gambar (versi 1)"},
    {"method": "GET", "path": "/api/tools/hd2", "desc": "Meningkatkan resolusi dan ketajaman gambar (versi 2)"},
    {"method": "GET", "path": "/api/tools/hd3", "desc": "Meningkatkan resolusi dan ketajaman gambar (versi 3)"},
    {"method": "GET", "path": "/api/tools/hd4", "desc": "Meningkatkan resolusi dan ketajaman gambar (versi 4)"},
    {"method": "GET", "path": "/api/tools/remini", "desc": "Memperjelas dan mempertajam foto yang buram"},
    {"method": "GET", "path": "/api/tools/pdf-info", "desc": "Melihat info PDF: jumlah halaman, metadata, dan ukuran"},
    {"method": "GET", "path": "/api/tools/pdf-merge", "desc": "Menggabungkan beberapa PDF menjadi satu berkas"},
    {"method": "GET", "path": "/api/tools/pdf-split", "desc": "Memisahkan halaman tertentu dari PDF"},
    {"method": "GET", "path": "/api/tools/pdf-compress", "desc": "Mengecilkan ukuran berkas PDF"},
    {"method": "GET", "path": "/api/tools/saveweb", "desc": "Mengarsipkan halaman web menjadi berkas ZIP"},
    {"method": "GET", "path": "/api/tools/ocr", "desc": "Mengambil teks dari dalam gambar"},
    {"method": "GET", "path": "/api/tools/ceknsfw", "desc": "Memeriksa apakah gambar aman atau mengandung konten dewasa"},
    {"method": "GET", "path": "/api/tools/translate", "desc": "Menerjemahkan teks antarbahasa"},
    {"method": "GET", "path": "/api/tools/ytsum", "desc": "Meringkas isi video YouTube dari transkripnya"},
    {"method": "GET", "path": "/api/tools/findsong", "desc": "Mengenali judul lagu dari potongan audio"},
    {"method": "GET", "path": "/api/tools/removevocal", "desc": "Mengurangi vokal pada lagu untuk karaoke"},
    {"method": "GET", "path": "/api/tools/tinyurl", "desc": "Memperpendek tautan dengan TinyURL"},
    {"method": "GET", "path": "/api/tools/cleanuri", "desc": "Memperpendek tautan dengan CleanURI"},
    {"method": "GET", "path": "/api/tools/dagd", "desc": "Memperpendek tautan dengan da.gd"},
    {"method": "GET", "path": "/api/tools/spoome", "desc": "Memperpendek tautan dengan spoo.me"},
    {"method": "GET", "path": "/api/tools/pagespeed", "desc": "Mengukur skor kecepatan halaman web dari Google PageSpeed"},
    {"method": "GET", "path": "/api/tools/proxy", "desc": "Melihat daftar proxy publik yang aktif"},
    {"method": "GET", "path": "/api/tools/genmail", "desc": "Membuat alamat email sementara beserta kotak masuknya"},
    {"method": "GET", "path": "/api/tools/tempmail2", "desc": "Membuat email sementara dan mengecek pesan masuk"},
    {"method": "GET", "path": "/api/tools/tempmail3", "desc": "Membuat email sementara untuk menerima pesan sekali pakai"},
    {"method": "GET", "path": "/api/tools/patreon-search", "desc": "Mencari kreator di Patreon"},
    {"method": "GET", "path": "/api/tools/patreon-creator", "desc": "Melihat profil kreator Patreon"},
    {"method": "GET", "path": "/api/tools/patreon-post", "desc": "Melihat detail postingan Patreon"},
    {"method": "GET", "path": "/api/tools/patreon-posts", "desc": "Melihat daftar postingan kreator Patreon"},
    {"method": "GET", "path": "/api/tools/ssweb", "desc": "Mengambil tangkapan layar (screenshot) halaman web"},
    {"method": "GET", "path": "/api/tools/ngl", "desc": "Mengirim pesan anonim ke tautan NGL seseorang"},
    {"method": "GET", "path": "/api/tools/cekresi", "desc": "Melacak status pengiriman paket dari nomor resi"},
    {"method": "GET", "path": "/api/tools/xlcheck", "desc": "Mengecek sisa kuota nomor XL/Axis"},
    {"method": "GET", "path": "/api/tools/xlcircle", "desc": "Mengecek status dan kuota XL Circle"},
    {"method": "GET", "path": "/api/tools/spamotp", "desc": "Tidak disediakan di RuangNode API"},
    {"method": "GET", "path": "/api/tools/alightmotion", "desc": "Tidak disediakan di RuangNode API"},
    {"method": "GET", "path": "/api/stalk/githubuser", "desc": "Melihat profil GitHub: bio, follower, dan repo populer"},
    {"method": "GET", "path": "/api/stalk/npm", "desc": "Melihat info paket npm: versi terbaru, deskripsi, dan maintainer"},
    {"method": "GET", "path": "/api/stalk/bluesky", "desc": "Melihat profil Bluesky: bio, jumlah pengikut, dan postingan"},
    {"method": "GET", "path": "/api/stalk/genshin", "desc": "Melihat profil pemain Genshin Impact dari UID"},
    {"method": "GET", "path": "/api/stalk/steam", "desc": "Melihat profil Steam dari nama vanity atau SteamID64"},
    {"method": "GET", "path": "/api/stalk/telegram", "desc": "Melihat info profil, grup, atau channel Telegram publik"},
    {"method": "GET", "path": "/api/stalk/youtube", "desc": "Melihat info channel YouTube: nama, deskripsi, dan subscriber"},
    {"method": "GET", "path": "/api/stalk/kick", "desc": "Melihat profil streamer Kick: bio, follower, dan status live"},
    {"method": "GET", "path": "/api/stalk/twitch", "desc": "Melihat info profil Twitch publik"},
    {"method": "GET", "path": "/api/stalk/threads", "desc": "Melihat info profil Threads publik"},
    {"method": "GET", "path": "/api/stalk/twitter", "desc": "Melihat info profil X (Twitter) publik"},
    {"method": "GET", "path": "/api/stalk/facebook", "desc": "Melihat info profil Facebook publik"},
    {"method": "GET", "path": "/api/stalk/pinterest", "desc": "Melihat profil Pinterest: nama dan jumlah pengikut"},
    {"method": "GET", "path": "/api/stalk/discord", "desc": "Menghitung tanggal pembuatan akun dari Discord user ID"},
    {"method": "GET", "path": "/api/stalk/whatsapp", "desc": "Memformat nomor WhatsApp dan membuat tautan wa.me"},
    {"method": "GET", "path": "/api/stalk/tiktok", "desc": "Melihat info profil TikTok: nickname dan statistik"},
    {"method": "GET", "path": "/api/stalk/instagram", "desc": "Melihat info profil Instagram publik"},
    {"method": "GET", "path": "/api/stalk/mlbb", "desc": "Mengecek nickname akun Mobile Legends dari ID dan Zone ID"},
    {"method": "GET", "path": "/api/stalk/codm", "desc": "Mengecek nickname akun Call of Duty Mobile dari UID"},
    {"method": "GET", "path": "/api/stalk/freefire", "desc": "Mengecek nickname akun Free Fire dari UID"},
    {"method": "GET", "path": "/api/stalk/valorant", "desc": "Mengecek nickname akun Valorant dari Riot ID"},
    {"method": "GET", "path": "/api/stalk/pubg", "desc": "Mengecek nickname akun PUBG Mobile dari ID pemain"},
    {"method": "GET", "path": "/api/stalk/reddit", "desc": "Melihat info akun Reddit publik"},
    {"method": "GET", "path": "/api/stalk/linkedin", "desc": "Melihat info profil LinkedIn publik"},
    {"method": "GET", "path": "/api/stalk/spotify", "desc": "Melihat info profil atau artis Spotify publik"},
    {"method": "GET", "path": "/api/stalk/roblox", "desc": "Melihat info akun Roblox dari username"},
    {"method": "GET", "path": "/api/pterodactyl/statuspanel", "desc": "Mengecek status panel Pterodactyl dan menghitung total server serta user"},
    {"method": "GET", "path": "/api/pterodactyl/listadmin", "desc": "Melihat daftar admin (root_admin) di panel Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/createadmin", "desc": "Membuat akun admin baru di panel Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/setadmin", "desc": "Menaikkan akun panel Pterodactyl menjadi admin"},
    {"method": "GET", "path": "/api/pterodactyl/deleteadmin", "desc": "Menghapus akun admin dari panel Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/create", "desc": "Membuat server game baru di panel Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/deleteserver", "desc": "Menghapus server dari panel Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/suspend", "desc": "Menangguhkan (suspend) server di panel Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/unsuspend", "desc": "Melepas penangguhan (unsuspend) server di panel Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/checksuspend", "desc": "Mengecek status penangguhan server di panel Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/deleteallserver", "desc": "Menghapus semua server di panel Pterodactyl sekaligus"},
    {"method": "GET", "path": "/api/pterodactyl/deletealluser", "desc": "Menghapus semua user non-admin di panel Pterodactyl sekaligus"},
    {"method": "GET", "path": "/api/pterodactyl/listserver", "desc": "Melihat daftar server milik kunci Client API Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/statusserver", "desc": "Melihat status dan pemakaian resource server Pterodactyl secara real-time"},
    {"method": "GET", "path": "/api/pterodactyl/onserver", "desc": "Menyalakan server Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/offserver", "desc": "Mematikan server Pterodactyl dengan aman (stop)"},
    {"method": "GET", "path": "/api/pterodactyl/restartserver", "desc": "Me-restart server Pterodactyl"},
    {"method": "GET", "path": "/api/pterodactyl/clearserver", "desc": "Menginstal ulang (reinstall) server Pterodactyl"},
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
def _read_health() -> dict:
    """Baca hasil health-check berkala (ditulis scripts/healthcheck.py).

    Tidak pernah melempar — dashboard harus tetap tampil walau file belum ada.
    """
    try:
        with open(os.path.join(BASE_DIR, "static", "health.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def _health_view(h: dict) -> dict:
    """Ringkasan health untuk template (logika keluar dari HTML)."""
    eng = h.get("engines") or {}
    yd = eng.get("ytdlp") or {}
    cb = eng.get("cobalt") or {}
    return {
        "has": bool(h),
        "checked_at": h.get("checked_at_display") or h.get("checked_at") or "",
        "ytdlp_ok": bool(yd.get("ok")),
        "ytdlp_ver": str(yd.get("version") or ""),
        "cobalt_ok": bool(cb.get("ok")),
        "cobalt_ver": str(cb.get("version") or ""),
        "checks": h.get("checks") or [],
    }


def catalog_context(request: Request) -> dict:
    health = _read_health()
    return {
        "request": request,
        "categories": CATEGORIES,
        "planned_total": PLANNED_TOTAL,
        "active": ACTIVE_ENDPOINTS,
        "phase": "Fase Endpoint",
        "health": health,
        "health_view": _health_view(health),
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
