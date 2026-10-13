"""Kategori Download — grup SOSIAL.

Endpoint: douyin, instagram, facebook, x, x2, snackvideo, capcut,
pinterest, videy, xnxx.

Prinsip: yt-dlp (library lokal di server, bukan API pihak ketiga) adalah jalur
utama bila didukung extractor-nya. Jalur kustom hanya memakai halaman/API
resmi platform itu sendiri (meta tag, preload link, pola CDN publik mereka
yang diverifikasi) — tanpa agregator pihak ketiga.
"""

import asyncio
import html as ihtml
import json
import os
import re

import httpx
import yt_dlp
from fastapi import APIRouter, Depends, Query

from ..common import (
    DEFAULT_HEADERS,
    bad_request,
    fetch_text,
    ok,
    require_url,
    upstream_fail,
)
from ..security import require_api_token

router = APIRouter(prefix="/api/download", dependencies=[Depends(require_api_token)])

YTDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "retries": 2,
}

NAV_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# ----------------------------------------------------------------------------
# Bantuan yt-dlp (blocking -> dijalankan di thread), dengan hook cookie env
# ----------------------------------------------------------------------------
def _ytdl_opts(cookie_env: str | None = None) -> dict:
    opts = dict(YTDL_BASE)
    if cookie_env:
        path = os.environ.get(cookie_env)
        if path:
            opts["cookiefile"] = path
    return opts


def _extract_sync(url: str, cookie_env: str | None = None) -> dict:
    with yt_dlp.YoutubeDL(_ytdl_opts(cookie_env)) as ydl:
        return ydl.extract_info(url, download=False)


async def _extract_or_none(url: str, cookie_env: str | None = None) -> tuple[dict | None, str]:
    try:
        return await asyncio.to_thread(_extract_sync, url, cookie_env), ""
    except yt_dlp.utils.DownloadError as e:
        return None, str(e)[:240]
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}."


def _meta(info: dict) -> dict:
    return {
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
    }


def _best_direct_video(info: dict) -> dict | None:
    best = None
    for f in info.get("formats") or []:
        if (f.get("protocol") or "https") not in ("https", "http"):
            continue
        if f.get("vcodec") == "none" or not f.get("url"):
            continue
        key = ((f.get("height") or 0), f.get("tbr") or 0)
        if best is None or key > best[0]:
            best = (key, f)
    if not best:
        return None
    f = best[1]
    return {
        "kind": "single",
        "url": f["url"],
        "quality": f.get("height"),
        "format_note": f.get("format_note"),
        "ext": f.get("ext"),
        "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
    }


def _largest_thumbnail(info: dict) -> dict | None:
    thumbs = info.get("thumbnails") or []
    cands = [t for t in thumbs if t.get("url")]
    if not cands:
        return None
    t = max(cands, key=lambda x: (x.get("width") or 0) * (x.get("height") or 0))
    return {
        "kind": "image",
        "url": t["url"],
        "width": t.get("width"),
        "height": t.get("height"),
        "note": "Pin berupa gambar — tautan gambar resolusi terbesar.",
    }


def _meta_tag(html: str, prop: str) -> str | None:
    """Ambil meta tag og:/twitter:/name= (toleran property/name, entity HTML)."""
    m = re.search(
        r'<meta[^>]+(?:property|name)="'
        + re.escape(prop) + r'"[^>]+content="([^"]+)"',
        html, re.I,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="'
            + re.escape(prop) + r'"',
            html, re.I,
        )
    return ihtml.unescape(m.group(1)).strip() if m else None


# ============================================================================
# douyin — yt-dlp (butuh cookie sesi)
# ============================================================================
DOUYIN_COOKIE_NOTE = (
    "Douyin menolak ekstraksi dari IP server ini "
    "(\"Fresh cookies are needed\"). Sediakan file cookie sesi Douyin via "
    "env DOUYIN_COOKIES=/path/cookies.txt lalu restart service."
)


@router.get("/douyin")
async def download_douyin(url: str = Query(default="")):
    url = require_url(url)
    info, err = await _extract_or_none(url, cookie_env="DOUYIN_COOKIES")
    if info is None:
        if "cookies" in err.lower() or "login" in err.lower():
            upstream_fail("TERBATAS: " + DOUYIN_COOKIE_NOTE)
        upstream_fail(f"Ekstraksi Douyin gagal: {err}")
    result = _meta(info)
    dl = _best_direct_video(info)
    if not dl:
        upstream_fail("Tidak ada format video yang bisa diambil untuk URL ini.")
    result["download"] = dl
    result["note"] = ("download.url adalah tautan video langsung Douyin "
                      "(tanpa watermark bila tersedia dari extractor).")
    return ok(result)


# ============================================================================
# instagram — yt-dlp (+ hook IG_COOKIES bila kena login wall)
# ============================================================================
IG_COOKIE_NOTE = (
    "Instagram meminta login dari IP server ini. Sediakan file cookie sesi "
    "Instagram via env IG_COOKIES=/path/cookies.txt lalu restart service."
)


@router.get("/instagram")
async def download_instagram(url: str = Query(default="")):
    url = require_url(url)
    info, err = await _extract_or_none(url, cookie_env="IG_COOKIES")
    if info is None:
        low = err.lower()
        if "login" in low or "cookies" in low or "rate-limit" in low or "429" in low:
            upstream_fail("TERBATAS: " + IG_COOKIE_NOTE + f" Detail: {err[:140]}")
        upstream_fail(f"Ekstraksi Instagram gagal: {err}")
    result = _meta(info)
    dl = _best_direct_video(info)
    if dl:
        result["download"] = dl
    else:
        img = _largest_thumbnail(info)
        if img:
            result["download"] = img
            result["note"] = "Postingan berupa foto/carousel — tautan gambar terbesar."
        else:
            upstream_fail("Tidak ada media yang bisa diambil untuk URL ini.")
    return ok(result)


# ============================================================================
# facebook — yt-dlp, fallback og:video
# ============================================================================
@router.get("/facebook")
async def download_facebook(url: str = Query(default="")):
    url = require_url(url)
    info, err = await _extract_or_none(url, cookie_env="FB_COOKIES")
    if info:
        result = _meta(info)
        dl = _best_direct_video(info)
        if dl:
            result["download"] = dl
            return ok(result)
    # Fallback: meta tag resmi halaman Facebook.
    html, _ = await fetch_text(url, headers=NAV_HEADERS)
    og_video = _meta_tag(html, "og:video:url") or _meta_tag(html, "og:video")
    if og_video:
        return ok({
            "title": _meta_tag(html, "og:title"),
            "uploader": None,
            "thumbnail": _meta_tag(html, "og:image"),
            "webpage_url": url,
            "download": {"kind": "single", "url": og_video, "ext": "mp4"},
            "limited": True,
            "note": "Metadata + video dari meta tag resmi halaman Facebook.",
        })
    low = (err or "").lower()
    if "login" in low:
        upstream_fail("TERBATAS: Facebook meminta login dari IP server ini "
                      "(konten tidak publik atau butuh sesi).")
    upstream_fail(f"Ekstraksi Facebook gagal: {err}")


# ============================================================================
# x — yt-dlp (jalur 1)
# ============================================================================
@router.get("/x")
async def download_x(url: str = Query(default="")):
    url = require_url(url)
    info, err = await _extract_or_none(url)
    if info is None:
        upstream_fail(f"Ekstraksi X gagal: {err}")
    result = _meta(info)
    # Kumpulkan semua varian video langsung (bukan manifest).
    vids = []
    for f in info.get("formats") or []:
        if (f.get("protocol") or "https") not in ("https", "http"):
            continue
        if f.get("vcodec") == "none" or not f.get("url"):
            continue
        vids.append({
            "url": f["url"],
            "quality": f.get("height"),
            "ext": f.get("ext"),
            "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
        })
    if not vids:
        upstream_fail("Postingan ini tidak berisi video/gambar yang bisa diambil.")
    vids.sort(key=lambda v: v["quality"] or 0, reverse=True)
    result["download"] = {"kind": "single", "url": vids[0]["url"],
                          "quality": vids[0]["quality"], "ext": vids[0]["ext"]}
    result["variants"] = vids
    # Gambar terlampir (bila ada).
    imgs = [t["url"] for t in (info.get("thumbnails") or []) if t.get("url")]
    if imgs:
        result["images"] = imgs
    return ok(result)


# ============================================================================
# x2 — scraping HTML resmi X (jalur 2, genuinely berbeda dari yt-dlp)
# ============================================================================
@router.get("/x2")
async def download_x2(url: str = Query(default="")):
    url = require_url(url)
    html, final_url = await fetch_text(url, headers=NAV_HEADERS)
    title = _meta_tag(html, "og:title")
    desc = _meta_tag(html, "og:description")
    thumb = _meta_tag(html, "og:image")
    # Varian video langsung dari halaman resmi X (video.twimg.com).
    found = re.findall(
        r'(https://video\.twimg\.com/[^"\\\s<>]+?\.mp4(?:\?[^"\\\s<>]*)?)', html)
    vids = []
    seen = set()
    for raw in found:
        u = ihtml.unescape(raw)
        if u in seen:
            continue
        seen.add(u)
        m = re.search(r'/(\d+)x(\d+)/', u)
        vids.append({
            "url": u,
            "quality": int(m.group(2)) if m else None,
            "ext": "mp4",
        })
    if not vids:
        # Mungkin postingan teks saja / terproteksi.
        if thumb and ("amplify_video_thumb" in thumb or "media" in thumb):
            return ok({
                "title": title, "text": desc, "thumbnail": thumb,
                "webpage_url": final_url, "download": None,
                "limited": True,
                "note": "Tidak ada varian video langsung di halaman ini "
                        "(mungkin postingan teks/gambar saja atau terproteksi).",
            })
        upstream_fail("Tidak ada media video di postingan ini "
                      "(atau postingan terproteksi/tidak publik).")
    vids.sort(key=lambda v: v["quality"] or 0, reverse=True)
    return ok({
        "title": title,
        "text": desc,
        "thumbnail": thumb,
        "webpage_url": final_url,
        "download": {"kind": "single", "url": vids[0]["url"],
                     "quality": vids[0]["quality"], "ext": "mp4"},
        "variants": vids,
        "note": "Varian video diekstrak dari halaman resmi X (jalur scraping, bukan yt-dlp).",
    })


# ============================================================================
# snackvideo — parse halaman (yt-dlp tidak mendukung pola /@user/video/)
# ============================================================================
@router.get("/snackvideo")
async def download_snackvideo(url: str = Query(default="")):
    url = require_url(url)
    html, final_url = await fetch_text(url, headers=NAV_HEADERS)
    mp4s = sorted(set(re.findall(r'https://[^"\\\s<>]+\.mp4', html)), key=len)
    if not mp4s:
        upstream_fail("Video tidak ditemukan di halaman SnackVideo ini.")
    best = mp4s[-1]
    title = _meta_tag(html, "og:title")
    if not title:
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        title = ihtml.unescape(m.group(1)).strip() if m else None
    return ok({
        "title": title,
        "uploader": None,
        "thumbnail": _meta_tag(html, "og:image"),
        "webpage_url": final_url,
        "download": {"kind": "single", "url": best, "ext": "mp4"},
        "note": "Tautan video langsung dari CDN SnackVideo/Kwai (hasil parse halaman).",
    })


# ============================================================================
# capcut — preload video resmi halaman template
# ============================================================================
@router.get("/capcut")
async def download_capcut(url: str = Query(default="")):
    url = require_url(url)
    html, final_url = await fetch_text(url, headers=NAV_HEADERS)
    if "template-detail/default-tool" in final_url:
        upstream_fail("Template CapCut tidak ditemukan (tautan kedaluwarsa/tidak valid).")
    # <link rel="preload" ... as="video" href="..."> — video preview resmi template.
    video = None
    for m in re.finditer(r'<link[^>]*>', html, re.I):
        tag = m.group(0)
        if 'as="video"' in tag or "as='video'" in tag:
            hm = re.search(r'href="([^"]+)"', tag)
            if hm:
                video = ihtml.unescape(hm.group(1)).replace("&amp;", "&")
                break
    desc = _meta_tag(html, "description") or _meta_tag(html, "og:description")
    author = None
    am = re.search(r"Check out ([^’']+?)[’']s template", html)
    if am:
        author = am.group(1).strip()
    title = _meta_tag(html, "og:title") or (desc.split(".")[0] if desc else None)
    result = {
        "title": title,
        "author": author,
        "description": desc,
        "cover": _meta_tag(html, "og:image"),
        "webpage_url": final_url,
    }
    if video:
        result["download"] = {"kind": "single", "url": video, "ext": "mp4"}
        result["note"] = ("Video preview resmi template dari CDN CapCut "
                          "(halaman template-detail resmi).")
    else:
        result["download"] = None
        result["limited"] = True
        result["note"] = ("TERBATAS: halaman template tidak menyertakan video "
                          "preview yang bisa diunduh — hanya metadata + cover.")
    return ok(result)


# ============================================================================
# pinterest — yt-dlp (video) / gambar resolusi penuh (pin foto)
# ============================================================================
@router.get("/pinterest")
async def download_pinterest(url: str = Query(default="")):
    url = require_url(url)
    info, err = await _extract_or_none(url)
    if info is None:
        upstream_fail(f"Ekstraksi Pinterest gagal: {err}")
    result = _meta(info)
    dl = _best_direct_video(info)
    if dl:
        result["download"] = dl
        result["note"] = "Video pin — tautan video langsung."
        return ok(result)
    img = _largest_thumbnail(info)
    if img:
        # Pinterest menyimpan original di pinimg; ambil varian /originals/ bila ada.
        u = img["url"]
        orig = re.sub(r"/\d+x/", "/originals/", u)
        if orig != u:
            img = dict(img, url=orig)
        result["download"] = img
        result["note"] = "Pin berupa gambar — tautan gambar resolusi penuh."
        return ok(result)
    upstream_fail("Tidak ada media yang bisa diambil untuk pin ini.")


# ============================================================================
# videy — pola CDN publik terverifikasi per-request (HEAD)
# ============================================================================
@router.get("/videy")
async def download_videy(url: str = Query(default="")):
    url = require_url(url)
    vid = None
    m = re.search(r"[?&]id=([A-Za-z0-9]+)", url)
    if m:
        vid = m.group(1)
    else:
        m = re.search(r"cdn\.videy\.co/([A-Za-z0-9]+)(?:\.mp4)?", url)
        if m:
            vid = m.group(1)
    if not vid:
        bad_request("URL Videy tidak dikenali (format: https://videy.co/v?id=XXXX).")
    cdn = f"https://cdn.videy.co/{vid}.mp4"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0,
                                     trust_env=False) as client:
            resp = await client.head(cdn, headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]})
            if resp.status_code != 200 or "video" not in (resp.headers.get("content-type") or ""):
                upstream_fail("File video tidak ditemukan di CDN Videy (ID tidak valid/kedaluwarsa).")
            size = resp.headers.get("content-length")
    except httpx.HTTPError:
        upstream_fail("Gagal menghubungi CDN Videy.")
    return ok({
        "id": vid,
        "webpage_url": f"https://videy.co/v?id={vid}",
        "download": {
            "kind": "single", "url": cdn, "ext": "mp4",
            "filesize_approx": int(size) if size and size.isdigit() else None,
        },
        "note": "Tautan MP4 langsung dari CDN publik Videy (keberadaannya diverifikasi).",
    })


# ============================================================================
# xnxx — yt-dlp (mendukung xnxx.com maupun mirror-nya)
# ============================================================================
@router.get("/xnxx")
async def download_xnxx(url: str = Query(default="")):
    url = require_url(url)
    info, err = await _extract_or_none(url)
    if info is None:
        upstream_fail(f"Ekstraksi XNXX gagal: {err}")
    result = _meta(info)
    vids = []
    for f in info.get("formats") or []:
        if (f.get("protocol") or "https") not in ("https", "http"):
            continue
        if f.get("vcodec") == "none" or not f.get("url"):
            continue
        vids.append({
            "url": f["url"],
            "quality": f.get("height"),
            "ext": f.get("ext"),
            "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
        })
    if not vids:
        # fallback: URL video utama extractor
        if info.get("url"):
            vids.append({"url": info["url"], "quality": None, "ext": info.get("ext")})
    if not vids:
        upstream_fail("Tidak ada format video yang bisa diambil untuk URL ini.")
    vids.sort(key=lambda v: v["quality"] or 0, reverse=True)
    result["download"] = {"kind": "single", "url": vids[0]["url"],
                          "quality": vids[0]["quality"], "ext": vids[0]["ext"]}
    result["variants"] = vids
    return ok(result)
