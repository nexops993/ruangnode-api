"""Kategori Download — endpoint unduhan per platform.

Fase ini: TikTok (endpoint unggulan, scraping dari awal).
Endpoint platform lain ditambahkan bertahap dalam kategori yang sama.
"""

import asyncio
import io
import json
import os
import re
import subprocess
import tempfile
import zipfile

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from ..common import (
    DEFAULT_HEADERS,
    fetch_bytes,
    fetch_text,
    ok,
    require_url,
    safe_filename,
    upstream_fail,
    bad_request,
)
from ..security import require_api_token

router = APIRouter(prefix="/api/download", dependencies=[Depends(require_api_token)])

TIKTOK_REFERER = {"Referer": "https://www.tiktok.com/"}

# Header navigasi browser PENUH. Tanpa ini (Accept + Sec-Fetch-*), TikTok
# menyajikan shell halaman tanpa data tertanam — terverifikasi di VPS.
TIKTOK_NAV_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ============================================================================
# TikTok — scraping dari awal (parse data tertanam halaman item)
# ============================================================================

def _extract_rehydration(html: str) -> dict | None:
    m = re.search(
        r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html, re.S,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    scope = data.get("__DEFAULT_SCOPE__", {})
    detail = scope.get("webapp.video-detail", {})
    item = detail.get("itemInfo", {}).get("itemStruct")
    return item


def _find_live_clip(image_obj: dict) -> str | None:
    """Cari URL klip gerak (live photo) di dalam objek gambar slide."""
    def scan(obj, depth=0):
        if depth > 3:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if isinstance(v, str) and ("live" in kl or "video" in kl) and v.startswith("http"):
                    return v
                r = scan(v, depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = scan(v, depth + 1)
                if r:
                    return r
        return None
    return scan(image_obj)


def _parse_tiktok_item(item: dict, source_url: str) -> dict:
    author = item.get("author", {}) or {}
    music = item.get("music", {}) or {}
    video = item.get("video", {}) or {}
    stats = item.get("stats", {}) or {}

    slides = []
    image_post = item.get("imagePost") or {}
    for img in image_post.get("images", []) or []:
        url_list = ((img.get("imageURL") or {}).get("urlList")) or []
        slides.append({
            "image": url_list[0] if url_list else None,
            "image_all": url_list,
            "width": img.get("imageWidth"),
            "height": img.get("imageHeight"),
            "live_clip": _find_live_clip(img),
        })

    is_photo = bool(slides)
    result = {
        "id": item.get("id"),
        "type": "photo" if is_photo else "video",
        "desc": item.get("desc"),
        "create_time": item.get("createTime"),
        "author": {
            "username": author.get("uniqueId"),
            "nickname": author.get("nickname"),
        },
        "stats": {
            "play": stats.get("playCount"),
            "like": stats.get("diggCount"),
            "comment": stats.get("commentCount"),
            "share": stats.get("shareCount"),
        },
        "video": {
            "nowm": video.get("playAddr"),
            "wm": video.get("downloadAddr"),
            "cover": (video.get("cover") or None),
            "duration": video.get("duration"),
            "width": video.get("width"),
            "height": video.get("height"),
        },
        "audio": {
            "mp3": music.get("playUrl"),
            "title": music.get("title"),
            "author": music.get("authorName"),
            "duration": music.get("duration"),
        },
        "slides": slides,
        "source": source_url,
    }
    return result


async def _get_tiktok_item(url: str) -> tuple[dict, dict, str]:
    """Ambil item TikTok; kembalikan (item, parsed, cookie_header).

    Cookie dari sesi pengambilan halaman WAJIB diteruskan ke CDN TikTok —
    tanpa cookie sesi yang sama, playAddr membalas 403 (terverifikasi).
    """
    async def _fetch(warm: bool):
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                     trust_env=False) as client:
            if warm:
                # Sesi hangat: kunjungi beranda dulu agar cookie sesi terbentuk.
                try:
                    await client.get("https://www.tiktok.com/", headers=TIKTOK_NAV_HEADERS)
                except httpx.HTTPError:
                    pass
            resp = await client.get(url, headers=TIKTOK_NAV_HEADERS)
            resp.raise_for_status()
            cookies = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
            return resp.text, str(resp.url), cookies

    try:
        html, final_url, cookie_header = await _fetch(warm=False)
        item = _extract_rehydration(html)
        if not item:
            # Coba sekali lagi dengan sesi hangat sebelum menyerah.
            html, final_url, cookie_header = await _fetch(warm=True)
            item = _extract_rehydration(html)
    except httpx.HTTPStatusError as e:
        upstream_fail(f"TikTok menolak request (HTTP {e.response.status_code}).")
    except httpx.HTTPError as e:
        upstream_fail(f"Gagal menghubungi TikTok: {type(e).__name__}.")
    if not item:
        # Fallback first-party: oEmbed resmi TikTok. TikTok TIDAK mengekspos
        # data slide foto carousel ke halaman web publik (terverifikasi:
        # tanpa itemStruct/og:image di semua varian UA) — untuk postingan
        # foto, oEmbed hanya memberi judul, author, dan cover.
        parsed = await _oembed_fallback(url)
        if parsed:
            return {}, parsed, cookie_header
        upstream_fail(
            "Data postingan TikTok tidak ditemukan di halaman (struktur halaman "
            "berubah atau postingan tidak publik)."
        )
    return item, _parse_tiktok_item(item, final_url), cookie_header


async def _oembed_fallback(url: str) -> dict | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0,
                                     trust_env=False) as client:
            resp = await client.get(
                "https://www.tiktok.com/oembed",
                params={"url": url},
                headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    author_url = data.get("author_url") or ""
    username = author_url.rstrip("/").split("@")[-1] if "@" in author_url else None
    is_photo = "/photo/" in url
    thumb = data.get("thumbnail_url")
    return {
        "id": None,
        "type": "photo" if is_photo else "video",
        "desc": data.get("title"),
        "create_time": None,
        "author": {"username": username, "nickname": data.get("author_name")},
        "stats": {"play": None, "like": None, "comment": None, "share": None},
        "video": {"nowm": None, "wm": None, "cover": thumb, "duration": None,
                  "width": data.get("thumbnail_width"), "height": data.get("thumbnail_height")},
        "audio": {"mp3": None, "title": None, "author": None, "duration": None},
        "slides": ([{"image": thumb, "image_all": [thumb], "width": data.get("thumbnail_width"),
                     "height": data.get("thumbnail_height"), "live_clip": None}] if thumb else []),
        "source": url,
        "limited": True,
        "note": ("Data terbatas dari oEmbed resmi TikTok: TikTok tidak mengekspos "
                 "detail slide foto carousel ke web publik, jadi yang tersedia hanya "
                 "judul, author, dan gambar cover."),
    }


@router.get("/tiktok")
async def download_tiktok(url: str = Query(default="")):
    url = require_url(url)
    _, parsed, _ = await _get_tiktok_item(url)
    return ok(parsed)


async def _stream_remote(remote_url: str, filename: str, media_type: str,
                         headers: dict | None = None):
    h = dict(DEFAULT_HEADERS)
    h.update(TIKTOK_REFERER)
    if headers:
        h.update(headers)
    # (cookie sesi TikTok diteruskan pemanggil lewat headers bila ada)
    client = httpx.AsyncClient(follow_redirects=True, timeout=120.0, trust_env=False)
    try:
        req = client.build_request("GET", remote_url, headers=h)
        resp = await client.send(req, stream=True)
        resp.raise_for_status()
    except httpx.HTTPError:
        await client.aclose()
        upstream_fail("Gagal mengambil file dari CDN TikTok.")

    async def body_iter():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/tiktok/file")
async def download_tiktok_file(
    url: str = Query(default=""),
    type: str = Query(default="video"),
    index: int = Query(default=1),
    indexes: str = Query(default=""),
):
    url = require_url(url)
    item, parsed, cookie_header = await _get_tiktok_item(url)
    item_id = parsed.get("id") or "tiktok"
    t = type.lower().strip()
    sess = {"Cookie": cookie_header} if cookie_header else {}

    if t == "video":
        remote = parsed["video"].get("nowm")
        if not remote:
            bad_request("Postingan ini tidak memiliki video (kemungkinan foto carousel).")
        return await _stream_remote(remote, f"tiktok_{item_id}.mp4", "video/mp4", headers=sess)

    if t == "mp3":
        remote = parsed["audio"].get("mp3")
        if not remote:
            bad_request("Postingan ini tidak memiliki audio yang bisa diunduh.")
        return await _stream_remote(remote, f"tiktok_{item_id}.mp3", "audio/mpeg", headers=sess)

    if t in ("photo", "gif"):
        slides = parsed.get("slides") or []
        if not slides:
            bad_request("Postingan ini bukan foto carousel.")

        if t == "photo" and indexes.strip():
            picked = []
            for part in indexes.split(","):
                part = part.strip()
                if not part.isdigit():
                    bad_request("Parameter indexes harus berupa angka dipisah koma, mis. 1,3.")
                n = int(part)
                if n < 1 or n > len(slides):
                    bad_request(f"Nomor slide {n} di luar jangkauan (1-{len(slides)}).")
                picked.append(n)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for n in picked:
                    img_url = slides[n - 1]["image"]
                    if not img_url:
                        continue
                    data = await fetch_bytes(img_url, headers={**TIKTOK_REFERER, **sess})
                    ext = "jpg"
                    zf.writestr(f"tiktok_{item_id}_slide{n}.{ext}", data)
            return Response(
                buf.getvalue(),
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="tiktok_{item_id}_slides.zip"'},
            )

        if index < 1 or index > len(slides):
            bad_request(f"Nomor slide di luar jangkauan (1-{len(slides)}).")
        slide = slides[index - 1]

        if t == "photo":
            if not slide["image"]:
                bad_request("Slide ini tidak memiliki URL gambar.")
            return await _stream_remote(
                slide["image"], f"tiktok_{item_id}_slide{index}.jpg", "image/jpeg", headers=sess)

        # t == "gif": konversi klip live photo slide menjadi GIF via ffmpeg
        clip = slide.get("live_clip")
        if not clip:
            bad_request("Slide ini tidak bergerak (tidak ada klip Live Photo), jadi tidak bisa dijadikan GIF.")
        clip_bytes = await fetch_bytes(clip, headers={**TIKTOK_REFERER, **sess})
        return await _clip_to_gif(clip_bytes, f"tiktok_{item_id}_slide{index}.gif")

    bad_request("Parameter type tidak dikenal. Pilihan: video, mp3, photo, gif.")


async def _clip_to_gif(clip_bytes: bytes, filename: str):
    tmpdir = tempfile.mkdtemp(prefix="rn_gif_")
    src = os.path.join(tmpdir, "clip.mp4")
    out = os.path.join(tmpdir, "out.gif")
    with open(src, "wb") as f:
        f.write(clip_bytes)
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vf", "fps=15,scale=480:-1:flags=lanczos,split[s0][s1];"
               "[s0]palettegen[p];[s1][p]paletteuse",
        "-loop", "0", out,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.communicate(), timeout=90)
    except asyncio.TimeoutError:
        proc.kill()
        upstream_fail("Konversi GIF melewati batas waktu.")
    if proc.returncode != 0 or not os.path.exists(out):
        upstream_fail("Konversi GIF gagal (ffmpeg tidak tersedia atau klip tidak valid).")
    return FileResponse(out, media_type="image/gif", filename=filename)
