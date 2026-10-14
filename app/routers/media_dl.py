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
from urllib.parse import quote

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
from ..engines import (
    cobalt_fetch,
    cobalt_tunnel_url,
    download_to_temp,
    title_from_filename,
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

# Bila suatu hari disediakan file cookie YouTube (YTDLP_COOKIES=/path/cookies.txt),
# endpoint YouTube pulih penuh tanpa perubahan kode — dinding bot YouTube
# membutuhkan sesi login yang valid.
_COOKIES = os.environ.get("YTDLP_COOKIES")
if _COOKIES:
    YTDL_BASE["cookiefile"] = _COOKIES

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
# Relay Cobalt: tunnel Cobalt (127.0.0.1) tidak bisa dijangkau klien publik,
# jadi server mem-proxy file-nya lewat endpoint ini (tetap di balik token).
# ----------------------------------------------------------------------------
def _guess_media(filename: str | None, mode: str) -> tuple[str, str]:
    fn = (filename or "").lower()
    if fn.endswith(".mp3") or mode == "audio":
        return "audio/mpeg", ".mp3"
    if fn.endswith(".webm"):
        return "video/webm", ".webm"
    return "video/mp4", ".mp4"


async def _cobalt_file_response(page_url: str, mode: str = "auto"):
    """Ambil tunnel Cobalt untuk page_url lalu stream file-nya ke klien."""
    data, err = await cobalt_fetch(page_url, mode=mode)
    if data is None:
        upstream_fail(f"Mesin Cobalt gagal memproses URL ini ({err}).")
    if data.get("cobalt_status") == "picker":
        upstream_fail("Cobalt mengembalikan beberapa item (mis. carousel) — "
                      "relay file hanya untuk satu media.")
    tunnel = cobalt_tunnel_url(data)
    if not tunnel:
        upstream_fail("Cobalt tidak memberikan tautan file untuk URL ini.")
    media, suffix = _guess_media(data.get("filename"), mode)
    fname = title_from_filename(data.get("filename"))
    try:
        path, size = await download_to_temp(tunnel, suffix=suffix)
    except httpx.HTTPError:
        upstream_fail("Gagal mengambil file dari tunnel Cobalt.")
    if size == 0:
        upstream_fail("File dari tunnel Cobalt kosong.")
    return FileResponse(
        path, media_type=media,
        filename=f"{safe_filename(fname or 'media', 'media')}{suffix}",
    )


def _cobalt_relay_url(page_url: str, mode: str = "auto") -> str:
    return (f"/api/download/cobalt/file?url={quote(page_url, safe='')}"
            f"&mode={mode}")


@router.get("/cobalt/file")
async def download_cobalt_file(url: str = Query(default=""),
                               mode: str = Query(default="auto")):
    """Unduh media via mesin Cobalt (fallback untuk dinding platform).

    mode: auto (video) | audio (MP3) | mute (video tanpa suara).
    """
    url = require_url(url)
    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "audio", "mute"):
        bad_request("mode harus salah satu: auto, audio, mute.")
    return await _cobalt_file_response(url, mode=mode)


# ----------------------------------------------------------------------------
# Bantuan yt-dlp (blocking -> dijalankan di thread)
# ----------------------------------------------------------------------------
def _extract_sync(url: str, fmt: str | None = None) -> dict:
    opts = dict(YTDL_BASE)
    if fmt:
        opts["format"] = fmt
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


async def _extract_or_none(url: str, fmt: str | None = None) -> tuple[dict | None, str]:
    """Kembalikan (info, pesan_error). Tidak melempar — pemanggil yang memutuskan."""
    try:
        return await asyncio.to_thread(_extract_sync, url, fmt), ""
    except yt_dlp.utils.DownloadError as e:
        return None, str(e)[:220]
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}."


async def _extract(url: str, fmt: str | None = None) -> dict:
    info, err = await _extract_or_none(url, fmt)
    if info is None:
        upstream_fail(f"Ekstraksi gagal: {err}")
    return info


async def _oembed_meta(page_url: str, oembed_api: str) -> dict | None:
    """Metadata dasar (judul/author/thumbnail) via oEmbed resmi platform."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0,
                                     trust_env=False) as client:
            resp = await client.get(
                oembed_api, params={"url": page_url},
                headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]})
            if resp.status_code != 200:
                return None
            d = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    return {
        "title": d.get("title"),
        "uploader": d.get("author_name"),
        "duration": None,
        "duration_string": None,
        "thumbnail": d.get("thumbnail_url"),
        "webpage_url": page_url,
        "view_count": None,
        "upload_date": None,
    }


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
        # Hanya file langsung (protocol https/http) — bukan manifest HLS/DASH.
        if (f.get("protocol") or "https") not in ("https", "http"):
            continue
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
    dl = _pick_merged_mp4(info, max_height)
    if dl:
        return dl
    # Fallback terakhir: manifest streaming (HLS/DASH) — bukan file langsung.
    for f in info.get("formats") or []:
        proto = f.get("protocol") or ""
        if "m3u8" in proto or "dash" in proto:
            if f.get("url"):
                return {
                    "kind": "stream-manifest",
                    "url": f["url"],
                    "protocol": proto,
                    "ext": f.get("ext"),
                    "quality": f.get("height"),
                    "note": ("Ini manifest streaming HLS/DASH — putar dengan player "
                             "yang mendukungnya (mis. VLC/hls.js), bukan file "
                             "video langsung yang bisa diunduh satu klik."),
                }
    return None


def _pick_bestaudio(info: dict) -> dict | None:
    fmts = info.get("formats") or []
    auds = [f for f in fmts
            if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url")]
    # Utamakan file audio langsung (bukan manifest streaming).
    direct = [f for f in auds if (f.get("protocol") or "https") in ("https", "http")]
    if direct:
        a = max(direct, key=lambda f: (f.get("abr") or 0))
        return {"kind": "single", "url": a["url"], "ext": a.get("ext"),
                "abr": a.get("abr"),
                "filesize_approx": a.get("filesize") or a.get("filesize_approx")}
    manifest = None
    for f in auds:
        proto = f.get("protocol") or ""
        if ("m3u8" in proto or "dash" in proto) and (
                manifest is None or (f.get("abr") or 0) > (manifest.get("abr") or 0)):
            manifest = f
    if manifest:
        return {
            "kind": "stream-manifest",
            "url": manifest["url"], "ext": manifest.get("ext"), "abr": manifest.get("abr"),
            "protocol": manifest.get("protocol"),
            "note": ("Manifest streaming HLS/DASH — putar dengan player yang "
                     "mendukungnya (mis. VLC), bukan file audio langsung."),
        }
    # fallback: format utama bila tidak ada format audio-only
    url = info.get("url")
    if url:
        return {"kind": "single", "url": url, "ext": info.get("ext"),
                "abr": None, "filesize_approx": None}
    return None


def _generic_meta(url: str, max_height: int | None = None) -> dict:
    url = require_url(url)
    return {"url": url, "max_height": max_height}


# ============================================================================
# ytmp4 / ytmp3
# ============================================================================
YOUTUBE_LIMIT_NOTE = (
    "TERBATAS: YouTube menolak ekstraksi dari IP server ini "
    "(\"Sign in to confirm you're not a bot\"). Metadata diambil dari oEmbed "
    "resmi; tautan unduh butuh cookie sesi YouTube (env YTDLP_COOKIES)."
)


@router.get("/ytmp4")
async def download_ytmp4(url: str = Query(default=""), quality: int = Query(default=720)):
    url = require_url(url)
    info, err = await _extract_or_none(url)
    if info:
        dl = _best_download(info, quality)
        if not dl:
            upstream_fail("Tidak ada format video yang bisa diambil untuk URL ini.")
        result = _meta(info)
        result["engine"] = "yt-dlp"
        result["download"] = dl
        return ok(result)
    fails = [f"yt-dlp: {err[:160]}"]
    # Fallback: mesin Cobalt (terbukti menembus dinding bot YouTube dari IP ini).
    cob, cob_err = await cobalt_fetch(url, mode="auto")
    if cob and cobalt_tunnel_url(cob):
        meta = await _oembed_meta(url, "https://www.youtube.com/oembed") or {}
        fname = cob.get("filename")
        return ok({
            "title": meta.get("title") or title_from_filename(fname),
            "uploader": meta.get("uploader"),
            "duration": meta.get("duration"),
            "thumbnail": meta.get("thumbnail"),
            "webpage_url": meta.get("webpage_url") or url,
            "engine": "cobalt",
            "download": {
                "kind": "cobalt-tunnel",
                "url": _cobalt_relay_url(url, mode="auto"),
                "filename": fname,
                "engine": "cobalt",
            },
            "note": ("Ekstraksi langsung diblokir YouTube dari IP server ini; "
                     "unduhan dialihkan via mesin Cobalt — file di-proxy "
                     "server ini lewat download.url."),
        })
    fails.append(f"cobalt: {cob_err[:160]}")
    meta = await _oembed_meta(url, "https://www.youtube.com/oembed")
    if not meta:
        upstream_fail("YouTube tidak bisa diakses dari server ini: "
                      + " | ".join(fails))
    meta.update({
        "limited": True,
        "engine": None,
        "download": None,
        "note": ("TERBATAS: " + " | ".join(fails) + " Metadata diambil dari "
                 "oEmbed resmi YouTube."),
    })
    return ok(meta)


@router.get("/ytmp3")
async def download_ytmp3(url: str = Query(default="")):
    url = require_url(url)
    info, err = await _extract_or_none(url)
    if info:
        audio = _pick_bestaudio(info)
        if not audio:
            upstream_fail("Tidak ada format audio yang bisa diambil untuk URL ini.")
        result = _meta(info)
        result["engine"] = "yt-dlp"
        result["audio"] = audio
        result["file_url"] = f"/api/download/ytmp3/file?url={quote(url, safe='')}"
        result["note"] = ("audio_url adalah stream audio langsung (bisa m4a/webm/opus). "
                          "Gunakan file_url untuk MP3 hasil konversi server.")
        return ok(result)
    fails = [f"yt-dlp: {err[:160]}"]
    # Fallback: mesin Cobalt — audio tunnel (MP3).
    cob, cob_err = await cobalt_fetch(url, mode="audio", audio_format="mp3")
    if cob and cobalt_tunnel_url(cob):
        meta = await _oembed_meta(url, "https://www.youtube.com/oembed") or {}
        fname = cob.get("filename")
        return ok({
            "title": meta.get("title") or title_from_filename(fname),
            "uploader": meta.get("uploader"),
            "thumbnail": meta.get("thumbnail"),
            "webpage_url": meta.get("webpage_url") or url,
            "engine": "cobalt",
            "audio": {
                "kind": "cobalt-tunnel",
                "url": _cobalt_relay_url(url, mode="audio"),
                "filename": fname,
                "engine": "cobalt",
            },
            "file_url": f"/api/download/ytmp3/file?url={quote(url, safe='')}",
            "note": ("Ekstraksi langsung diblokir YouTube dari IP server ini; "
                     "audio dialihkan via mesin Cobalt (MP3, di-proxy server "
                     "ini lewat audio.url / file_url)."),
        })
    fails.append(f"cobalt: {cob_err[:160]}")
    meta = await _oembed_meta(url, "https://www.youtube.com/oembed")
    if not meta:
        upstream_fail("YouTube tidak bisa diakses dari server ini: "
                      + " | ".join(fails))
    meta.update({
        "limited": True,
        "engine": None,
        "audio": None,
        "file_url": None,
        "note": ("TERBATAS: " + " | ".join(fails) + " Metadata diambil dari "
                 "oEmbed resmi YouTube."),
    })
    return ok(meta)


@router.get("/ytmp3/file")
async def download_ytmp3_file(url: str = Query(default="")):
    url = require_url(url)
    info, err = await _extract_or_none(url)
    if info is None:
        # Fallback: mesin Cobalt memberi tunnel audio MP3 langsung — proxy saja.
        return await _cobalt_file_response(url, mode="audio")
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
    info, err = await _extract_or_none(url)
    if info is None:
        # Fallback: mesin Cobalt (ditest: gagal juga dari IP ini, tapi jalur
        # tetap dicoba — bila suatu saat hidup, endpoint pulih otomatis).
        cob, cob_err = await cobalt_fetch(url, mode="auto")
        if cob and cobalt_tunnel_url(cob):
            return ok({
                "title": title_from_filename(cob.get("filename")),
                "webpage_url": url,
                "engine": "cobalt",
                "download": {
                    "kind": "cobalt-tunnel",
                    "url": _cobalt_relay_url(url, mode="auto"),
                    "filename": cob.get("filename"),
                    "engine": "cobalt",
                },
                "note": "Unduhan via mesin Cobalt — file di-proxy server ini.",
            })
        upstream_fail("Bilibili menolak request dari IP server ini (HTTP 412 — WAF "
                      f"Bilibili memblokir IP datacenter; Cobalt juga gagal: "
                      f"{cob_err[:120]}). Detail: {err[:140]}")
    res = _platform_ok(url, info)
    res["engine"] = "yt-dlp"
    return ok(res)


@router.get("/dailymotion")
async def download_dailymotion(url: str = Query(default="")):
    url = require_url(url)
    info = await _extract(url)
    res = _platform_ok(url, info)
    res["engine"] = "yt-dlp"
    return ok(res)


@router.get("/vimeo")
async def download_vimeo(url: str = Query(default="")):
    url = require_url(url)
    info, err = await _extract_or_none(url)
    if info:
        res = _platform_ok(url, info)
        res["engine"] = "yt-dlp"
        return ok(res)
    # Fallback: mesin Cobalt (ditest: gagal juga dari IP ini, tapi jalur
    # tetap dicoba — bila suatu saat hidup, endpoint pulih otomatis).
    cob, cob_err = await cobalt_fetch(url, mode="auto")
    if cob and cobalt_tunnel_url(cob):
        meta = await _oembed_meta(url, "https://vimeo.com/api/oembed.json") or {}
        return ok({
            "title": meta.get("title") or title_from_filename(cob.get("filename")),
            "uploader": meta.get("uploader"),
            "thumbnail": meta.get("thumbnail"),
            "webpage_url": meta.get("webpage_url") or url,
            "engine": "cobalt",
            "download": {
                "kind": "cobalt-tunnel",
                "url": _cobalt_relay_url(url, mode="auto"),
                "filename": cob.get("filename"),
                "engine": "cobalt",
            },
            "note": "Unduhan via mesin Cobalt — file di-proxy server ini.",
        })
    # Vimeo memblokir IP server ini (web client butuh login, config 403) —
    # fallback metadata resmi via oEmbed.
    meta = await _oembed_meta(url, "https://vimeo.com/api/oembed.json")
    if not meta:
        upstream_fail(f"Ekstraksi gagal: yt-dlp: {err[:120]} | "
                      f"cobalt: {cob_err[:120]}")
    meta.update({
        "limited": True,
        "engine": None,
        "download": None,
        "note": ("TERBATAS: Vimeo menolak ekstraksi file dari IP server ini "
                 "(web client butuh login; Cobalt juga gagal). Metadata dari "
                 "oEmbed resmi; tautan unduh tidak tersedia."),
    })
    return ok(meta)


@router.get("/soundcloud")
async def download_soundcloud(url: str = Query(default="")):
    url = require_url(url)
    info = await _extract(url)
    result = _meta(info)
    audio = _pick_bestaudio(info)
    if audio:
        result["audio"] = audio
        if audio.get("kind") == "stream-manifest":
            result["note"] = audio.pop("note")
        else:
            result["note"] = ("audio_url adalah URL stream langsung dari SoundCloud "
                              "(ber-token, masa berlaku pendek).")
    else:
        dl = _best_download(info)
        if dl:
            result["download"] = dl
        else:
            upstream_fail("Tidak ada format yang bisa diambil untuk URL ini.")
    return ok(result)
