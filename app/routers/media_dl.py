"""Kategori Download — grup yt-dlp.

Endpoint: ytmp3, ytmp4, ytpost, bilibili, dailymotion, vimeo, soundcloud.
yt_dlp dipakai sebagai LIBRARY lokal yang berjalan di server sendiri
(bukan API pihak ketiga), untuk mengekstrak metadata + URL stream langsung.
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile

import httpx
import yt_dlp
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from ..common import (
    DEFAULT_HEADERS,
    bad_request,
    fetch_text,
    ok,
    require_url,
    safe_filename,
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

YT_NAV_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

MAX_MP3_SECONDS = 20 * 60  # ytmp3/file menolak video > 20 menit


# ----------------------------------------------------------------------------
# Bantuan yt-dlp (blocking -> dijalankan di thread)
# ----------------------------------------------------------------------------
def _extract_sync(url: str, fmt: str | None = None) -> dict:
    opts = dict(YTDL_BASE)
    if fmt:
        opts["format"] = fmt
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        upstream_fail(f"Ekstraksi gagal: {str(e)[:220]}")
    except Exception as e:  # noqa: BLE001
        upstream_fail(f"Ekstraksi gagal: {type(e).__name__}.")


async def _extract(url: str, fmt: str | None = None) -> dict:
    return await asyncio.to_thread(_extract_sync, url, fmt)


def _meta(info: dict) -> dict:
    return {
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
    }


def _pick_progressive_mp4(info: dict, max_height: int | None) -> dict | None:
    best = None
    for f in info.get("formats") or []:
        if f.get("ext") != "mp4" or f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue
        h = f.get("height") or 0
        if max_height and h > max_height:
            continue
        if not f.get("url"):
            continue
        key = (h, f.get("tbr") or 0)
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
        "ext": "mp4",
        "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
    }


def _pick_merged_mp4(info: dict, max_height: int | None) -> dict | None:
    vids = [f for f in info.get("formats") or []
            if f.get("ext") == "mp4" and f.get("vcodec") != "none"
            and f.get("acodec") == "none" and f.get("url")
            and (not max_height or (f.get("height") or 0) <= max_height)]
    auds = [f for f in info.get("formats") or []
            if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url")]
    if not vids or not auds:
        return None
    v = max(vids, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))
    a = max(auds, key=lambda f: (f.get("abr") or 0))
    return {
        "kind": "merged",
        "video_url": v["url"],
        "audio_url": a["url"],
        "quality": v.get("height"),
        "ext": "mp4",
        "note": "Video dan audio terpisah — perlu digabung (mux) dengan ffmpeg.",
    }


def _best_download(info: dict, max_height: int | None = None) -> dict | None:
    dl = _pick_progressive_mp4(info, max_height)
    if dl:
        return dl
    return _pick_merged_mp4(info, max_height)


def _pick_bestaudio(info: dict) -> dict | None:
    fmts = info.get("formats") or []
    auds = [f for f in fmts
            if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url")]
    if auds:
        a = max(auds, key=lambda f: (f.get("abr") or 0))
        return {"url": a["url"], "ext": a.get("ext"),
                "abr": a.get("abr"),
                "filesize_approx": a.get("filesize") or a.get("filesize_approx")}
    # fallback: format utama bila tidak ada format audio-only
    url = info.get("url")
    if url:
        return {"url": url, "ext": info.get("ext"), "abr": None, "filesize_approx": None}
    return None


def _generic_meta(url: str, max_height: int | None = None) -> dict:
    url = require_url(url)
    return {"url": url, "max_height": max_height}


# ============================================================================
# ytmp4 / ytmp3
# ============================================================================
@router.get("/ytmp4")
async def download_ytmp4(url: str = Query(default=""), quality: int = Query(default=720)):
    url = require_url(url)
    info = await _extract(url)
    dl = _best_download(info, quality)
    if not dl:
        upstream_fail("Tidak ada format video yang bisa diambil untuk URL ini.")
    result = _meta(info)
    result["download"] = dl
    return ok(result)


@router.get("/ytmp3")
async def download_ytmp3(url: str = Query(default="")):
    url = require_url(url)
    info = await _extract(url)
    audio = _pick_bestaudio(info)
    if not audio:
        upstream_fail("Tidak ada format audio yang bisa diambil untuk URL ini.")
    result = _meta(info)
    result["audio"] = audio
    result["file_url"] = f"/api/download/ytmp3/file?url={url}"
    result["note"] = ("audio_url adalah stream audio langsung (bisa m4a/webm/opus). "
                      "Gunakan file_url untuk MP3 hasil konversi server.")
    return ok(result)


@router.get("/ytmp3/file")
async def download_ytmp3_file(url: str = Query(default="")):
    url = require_url(url)
    info = await _extract(url)
    dur = info.get("duration") or 0
    if dur > MAX_MP3_SECONDS:
        bad_request(f"Video terlalu panjang ({info.get('duration_string')}). "
                    "Batas konversi MP3 server: 20 menit.")
    title = safe_filename(info.get("title") or "audio", "audio")

    def _work():
        tmpdir = tempfile.mkdtemp(prefix="rn_mp3_")
        outtmpl = os.path.join(tmpdir, "audio.%(ext)s")
        opts = dict(YTDL_BASE, format="bestaudio/best", outtmpl=outtmpl)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        srcs = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)
                if os.path.isfile(os.path.join(tmpdir, f))]
        if not srcs:
            upstream_fail("Unduhan audio dari sumber gagal (file kosong).")
        src = max(srcs, key=os.path.getsize)
        mp3 = os.path.join(tmpdir, "out.mp3")
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vn", "-ar", "44100",
             "-ac", "2", "-b:a", "192k", mp3],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )
        if proc.returncode != 0 or not os.path.exists(mp3):
            upstream_fail("Konversi ke MP3 gagal (ffmpeg).")
        return mp3

    try:
        mp3 = await asyncio.to_thread(_work)
    except yt_dlp.utils.DownloadError as e:
        upstream_fail(f"Unduhan audio gagal: {str(e)[:220]}")

    return FileResponse(mp3, media_type="audio/mpeg", filename=f"{title}.mp3")


# ============================================================================
# ytpost — postingan komunitas YouTube (scraping halaman, bukan yt-dlp)
# ============================================================================
def _find_key(obj, key):
    """Cari kunci secara rekursif dalam struktur dict/list."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _runs_text(runs) -> str:
    if not runs:
        return ""
    return "".join(r.get("text", "") for r in runs if isinstance(r, dict))


@router.get("/ytpost")
async def download_ytpost(url: str = Query(default="")):
    url = require_url(url)
    html, final_url = await fetch_text(url, headers=YT_NAV_HEADERS)
    m = re.search(r"var ytInitialData = (\{.*?\});\s*</script>", html, re.S)
    if not m:
        upstream_fail("Data postingan tidak ditemukan di halaman (struktur berubah "
                       "atau postingan tidak publik).")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        upstream_fail("Data postingan tidak bisa diparse.")
    post = _find_key(data, "backstagePostRenderer")
    if not post:
        upstream_fail("Postingan komunitas tidak ditemukan di halaman ini.")
    attach = post.get("backstageAttachment") or {}
    img = _find_key(attach, "backstageImageRenderer")
    images = []
    if img:
        thumbs = ((img.get("image") or {}).get("thumbnails")) or []
        for th in thumbs:
            if th.get("url"):
                images.append({
                    "url": th["url"],
                    "width": th.get("width"),
                    "height": th.get("height"),
                })
    result = {
        "post_id": post.get("postId"),
        "text": _runs_text((post.get("contentText") or {}).get("runs")),
        "author": _runs_text((post.get("authorText") or {}).get("runs")),
        "published": _runs_text((post.get("publishedTimeText") or {}).get("runs")),
        "images": images,
        "source": final_url,
    }
    buttons = post.get("actionButtons") or {}
    if isinstance(buttons, dict):
        result["like_count"] = (buttons.get("likeButtonRenderer") or {}).get("likeCount")
    else:
        result["like_count"] = None
    return ok(result)


# ============================================================================
# bilibili / dailymotion / vimeo / soundcloud
# ============================================================================
def _platform_ok(url: str, info: dict) -> dict:
    result = _meta(info)
    dl = _best_download(info)
    if not dl:
        # fallback: format utama extractor (mis. soundcloud audio)
        audio = _pick_bestaudio(info)
        if audio:
            result["audio"] = audio
            return result
        upstream_fail("Tidak ada format yang bisa diambil untuk URL ini.")
    result["download"] = dl
    return result


@router.get("/bilibili")
async def download_bilibili(url: str = Query(default="")):
    url = require_url(url)
    info = await _extract(url)
    return ok(_platform_ok(url, info))


@router.get("/dailymotion")
async def download_dailymotion(url: str = Query(default="")):
    url = require_url(url)
    info = await _extract(url)
    return ok(_platform_ok(url, info))


@router.get("/vimeo")
async def download_vimeo(url: str = Query(default="")):
    url = require_url(url)
    info = await _extract(url)
    return ok(_platform_ok(url, info))


@router.get("/soundcloud")
async def download_soundcloud(url: str = Query(default="")):
    url = require_url(url)
    info = await _extract(url)
    result = _meta(info)
    audio = _pick_bestaudio(info)
    if audio:
        result["audio"] = audio
        result["note"] = ("audio_url adalah URL stream langsung dari SoundCloud "
                          "(ber-token, masa berlaku pendek).")
    else:
        dl = _best_download(info)
        if dl:
            result["download"] = dl
        else:
            upstream_fail("Tidak ada format yang bisa diambil untuk URL ini.")
    return ok(result)
