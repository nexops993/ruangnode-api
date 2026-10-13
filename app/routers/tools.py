"""Kategori Tools (43 endpoint).

Sumber: komputasi lokal (library server), API resmi layanan itu sendiri,
backend pemilik (9Router / ai_backend), scraping halaman publik resmi.
Bukan agregator pihak ketiga.

Dua endpoint DITOLAK fungsional (keputusan pemilik):
- spamotp: banjir OTP = pelecehan.
- alightmotion: aktivasi premium ilegal = pembajakan.
Keduanya membalas 403 dengan alasan jelas.
"""

import asyncio
import base64
import binascii
import io
import json
import os
import random
import re
import string
import subprocess
import tempfile
import zipfile
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .. import ai_backend as ab
from ..common import (
    DEFAULT_HEADERS,
    bad_request,
    ok,
    require_url,
    upstream_fail,
)
from ..security import require_api_token

router = APIRouter(prefix="/api/tools", dependencies=[Depends(require_api_token)])

MAX_MEDIA_BYTES = 12 * 1024 * 1024
UA = DEFAULT_HEADERS["User-Agent"]


def _need(text: str | None, name: str) -> str:
    text = (text or "").strip()
    if not text:
        bad_request(f"Parameter {name} wajib diisi.")
    return text


def _limited_note(result: dict, note: str) -> dict:
    result["limited"] = True
    result["note"] = note
    return result


async def _download(url: str, max_bytes: int = MAX_MEDIA_BYTES,
                    timeout: float = 60.0) -> bytes:
    """Unduh berkas; gagal -> 502 jujur."""
    url = require_url(url)
    headers = {
        "User-Agent": UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        "Sec-Fetch-Mode": "no-cors",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                     trust_env=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.content
    except httpx.HTTPStatusError as e:
        upstream_fail(f"Sumber media menolak request (HTTP {e.response.status_code}).")
    except httpx.HTTPError:
        upstream_fail("Gagal mengunduh media dari URL tersebut.")
    if len(data) > max_bytes:
        bad_request(f"Ukuran media melebihi batas {max_bytes // 1024 // 1024} MB.")
    if not data:
        upstream_fail("Media yang diunduh kosong.")
    return data


async def _post_form(url: str, data: dict, timeout: float = 30.0) -> httpx.Response:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                     trust_env=False) as client:
            resp = await client.post(url, data=data,
                                     headers={"User-Agent": UA})
            return resp
    except httpx.HTTPError:
        upstream_fail("Gagal menghubungi layanan shortener.")


def _png_response(img: Image.Image) -> Response:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


def _jpeg_response(img: Image.Image, quality: int = 90) -> Response:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return Response(content=buf.getvalue(), media_type="image/jpeg")


def _open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:
        upstream_fail("Berkas bukan gambar yang valid.")


# ============================================================================
# Konversi teks lokal
# ============================================================================
@router.get("/text2base64")
async def t_text2base64(text: str = Query(default="")):
    text = _need(text, "text")
    return ok({"base64": base64.b64encode(text.encode("utf-8")).decode()})


@router.get("/base64totext")
async def t_base64totext(text: str = Query(default="")):
    text = _need(text, "text")
    try:
        raw = base64.b64decode(text.strip(), validate=True)
        return ok({"text": raw.decode("utf-8")})
    except (binascii.Error, UnicodeDecodeError):
        bad_request("Input bukan Base64 yang valid / bukan teks UTF-8.")


@router.get("/text2binner")
async def t_text2binner(text: str = Query(default="")):
    text = _need(text, "text")
    bits = " ".join(f"{b:08b}" for b in text.encode("utf-8"))
    return ok({"binary": bits})


@router.get("/binner2text")
async def t_binner2text(text: str = Query(default="")):
    text = _need(text, "text")
    cleaned = re.sub(r"[^01]", "", text)
    if not cleaned or len(cleaned) % 8 != 0:
        bad_request("Input biner tidak valid (harus kelipatan 8 bit).")
    raw = bytes(int(cleaned[i:i + 8], 2) for i in range(0, len(cleaned), 8))
    try:
        return ok({"text": raw.decode("utf-8")})
    except UnicodeDecodeError:
        bad_request("Biner tidak membentuk teks UTF-8 yang valid.")


@router.get("/text2qr")
async def t_text2qr(text: str = Query(default="")):
    text = _need(text, "text")
    import qrcode
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    return _png_response(qr.make_image(fill_color="black", back_color="white"))


# ============================================================================
# Parser NIK Indonesia (lokal; tanpa tabel kabupaten/kota)
# ============================================================================
PROVINSI = {
    "11": "Aceh", "12": "Sumatera Utara", "13": "Sumatera Barat",
    "14": "Riau", "15": "Jambi", "16": "Sumatera Selatan",
    "17": "Bengkulu", "18": "Lampung", "19": "Kepulauan Bangka Belitung",
    "21": "Kepulauan Riau", "31": "DKI Jakarta", "32": "Jawa Barat",
    "33": "Jawa Tengah", "34": "DI Yogyakarta", "35": "Jawa Timur",
    "36": "Banten", "51": "Bali", "52": "Nusa Tenggara Barat",
    "53": "Nusa Tenggara Timur", "61": "Kalimantan Barat",
    "62": "Kalimantan Tengah", "63": "Kalimantan Selatan",
    "64": "Kalimantan Timur", "65": "Kalimantan Utara",
    "71": "Sulawesi Utara", "72": "Sulawesi Tengah", "73": "Sulawesi Selatan",
    "74": "Sulawesi Tenggara", "75": "Gorontalo", "76": "Sulawesi Barat",
    "81": "Maluku", "82": "Maluku Utara", "91": "Papua",
    "92": "Papua Barat", "93": "Papua Selatan", "94": "Papua Tengah",
    "95": "Papua Pegunungan", "96": "Papua Barat Daya",
}


@router.get("/nik")
async def t_nik(nik: str = Query(default="")):
    nik = re.sub(r"\D", "", _need(nik, "nik"))
    if len(nik) != 16:
        bad_request("NIK harus 16 digit angka.")
    prov = PROVINSI.get(nik[0:2], "Tidak dikenal")
    dd, mm, yy = int(nik[6:8]), int(nik[8:10]), int(nik[10:12])
    jk = "Perempuan" if dd > 40 else "Laki-laki"
    if dd > 40:
        dd -= 40
    try:
        tahun = 2000 + yy if yy <= 30 else 1900 + yy
        from datetime import date
        tgl = date(tahun, mm, dd).isoformat()
    except ValueError:
        tgl = None
    return ok({
        "nik": nik,
        "provinsi": prov,
        "kode_kab_kota": nik[2:4],
        "kode_kecamatan": nik[4:6],
        "tanggal_lahir": tgl,
        "jenis_kelamin": jk,
        "nomor_urut": nik[12:16],
        "note": ("Tanggal lahir memakai aturan abad (<=30 -> 2000-an) dan bisa "
                 "salah untuk kasus tepi; kode kabupaten/kota & kecamatan tidak "
                 "dipetakan ke nama (butuh tabel Kemendagri penuh)."),
    })


# ============================================================================
# Olah gambar lokal (Pillow)
# ============================================================================
@router.get("/imageconvert")
async def t_imageconvert(url: str = Query(default=""),
                         format: str = Query(default="jpeg"),
                         width: int = Query(default=0),
                         height: int = Query(default=0),
                         quality: int = Query(default=90)):
    data = await _download(url)
    img = _open_image(data)
    fmt = (format or "jpeg").lower().lstrip(".")
    if fmt not in ("jpeg", "jpg", "png", "webp", "bmp", "gif"):
        bad_request("Format tidak didukung (jpeg/png/webp/bmp/gif).")
    if fmt == "jpg":
        fmt = "jpeg"
    if width > 0 or height > 0:
        w, h = img.size
        nw = width if width > 0 else int(w * height / h)
        nh = height if height > 0 else int(h * width / w)
        if nw <= 0 or nh <= 0 or nw > 8000 or nh > 8000:
            bad_request("Dimensi tidak valid (maks 8000 px).")
        img = img.resize((nw, nh), Image.LANCZOS)
    buf = io.BytesIO()
    save_kw = {"quality": max(1, min(100, quality))} if fmt == "jpeg" else {}
    img.convert("RGB" if fmt == "jpeg" else "RGBA").save(
        buf, format=fmt.upper(), **save_kw)
    mt = "image/jpeg" if fmt == "jpeg" else f"image/{fmt}"
    return Response(content=buf.getvalue(), media_type=mt)


def _enhance_local(data: bytes, scale: float, sharpen: float,
                   color: float, contrast: float) -> Image.Image:
    """Upscale Lanczos + penajaman/peningkatan warna lokal (bukan model AI)."""
    img = _open_image(data).convert("RGB")
    w, h = img.size
    nw, nh = int(w * scale), int(h * scale)
    if nw * nh > 16_000_000:
        f = (16_000_000 / (nw * nh)) ** 0.5
        nw, nh = int(nw * f), int(nh * f)
    img = img.resize((nw, nh), Image.LANCZOS)
    img = ImageEnhance.Sharpness(img).enhance(sharpen)
    img = ImageEnhance.Color(img).enhance(color)
    img = ImageEnhance.Contrast(img).enhance(contrast)
    return img


def _hd_route(scale: float, sharpen: float, color: float, contrast: float,
              label: str):
    async def _handler(url: str = Query(default="")):
        data = await _download(url)
        img = _enhance_local(data, scale, sharpen, color, contrast)
        resp = _jpeg_response(img, quality=92)
        resp.headers["X-RuangNode-Limited"] = "true"
        resp.headers["X-RuangNode-Note"] = (
            "Upscaling lokal (Lanczos + sharpening), bukan model AI upscale.")
        return resp
    _handler.__name__ = label
    return _handler


router.get("/hd")(_hd_route(2.0, 1.6, 1.1, 1.05, "t_hd"))
router.get("/hd2")(_hd_route(2.0, 2.0, 1.2, 1.1, "t_hd2"))
router.get("/hd3")(_hd_route(3.0, 1.8, 1.15, 1.08, "t_hd3"))
router.get("/hd4")(_hd_route(4.0, 2.2, 1.25, 1.12, "t_hd4"))


@router.get("/remini")
async def t_remini(url: str = Query(default="")):
    data = await _download(url)
    img = _enhance_local(data, 2.0, 2.4, 1.3, 1.15)
    resp = _jpeg_response(img, quality=92)
    resp.headers["X-RuangNode-Limited"] = "true"
    resp.headers["X-RuangNode-Note"] = (
        "Enhance lokal (bukan aplikasi Remini asli).")
    return resp


# ============================================================================
# PDF (pypdf, lokal)
# ============================================================================
def _parse_pages(spec: str, total: int) -> list[int]:
    """'1-3,5' -> indeks 0-based; salah -> 400."""
    pages: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                bad_request("Format pages tidak valid (contoh: 1-3,5).")
            if lo < 1 or hi > total or lo > hi:
                bad_request("Rentang pages di luar jumlah halaman.")
            pages.update(range(lo - 1, hi))
        else:
            try:
                n = int(part)
            except ValueError:
                bad_request("Format pages tidak valid (contoh: 1-3,5).")
            if n < 1 or n > total:
                bad_request("Nomor halaman di luar jumlah halaman.")
            pages.add(n - 1)
    if not pages:
        bad_request("Parameter pages wajib diisi (contoh: 1-3,5).")
    return sorted(pages)


@router.get("/pdf-info")
async def t_pdf_info(url: str = Query(default="")):
    from pypdf import PdfReader
    data = await _download(url)
    try:
        reader = PdfReader(io.BytesIO(data))
        meta = reader.metadata or {}
    except Exception:
        upstream_fail("Berkas bukan PDF yang valid.")
    return ok({
        "pages": len(reader.pages),
        "title": meta.get("/Title"),
        "author": meta.get("/Author"),
        "subject": meta.get("/Subject"),
        "size_bytes": len(data),
    })


@router.get("/pdf-merge")
async def t_pdf_merge(urls: str = Query(default="")):
    from pypdf import PdfReader, PdfWriter
    parts = [u.strip() for u in _need(urls, "urls").split(",") if u.strip()]
    if len(parts) < 2:
        bad_request("Parameter urls butuh minimal 2 URL PDF (pisahkan koma).")
    if len(parts) > 10:
        bad_request("Maksimal 10 URL.")
    writer = PdfWriter()
    total = 0
    for u in parts:
        data = await _download(u)
        try:
            reader = PdfReader(io.BytesIO(data))
            for p in reader.pages:
                writer.add_page(p)
            total += len(reader.pages)
        except Exception:
            upstream_fail(f"Salah satu berkas bukan PDF yang valid: {u[:60]}")
    buf = io.BytesIO()
    writer.write(buf)
    resp = Response(content=buf.getvalue(), media_type="application/pdf")
    resp.headers["Content-Disposition"] = "attachment; filename=merged.pdf"
    return resp


@router.get("/pdf-split")
async def t_pdf_split(url: str = Query(default=""),
                      pages: str = Query(default="")):
    from pypdf import PdfReader, PdfWriter
    data = await _download(url)
    try:
        reader = PdfReader(io.BytesIO(data))
        total = len(reader.pages)
    except Exception:
        upstream_fail("Berkas bukan PDF yang valid.")
    idx = _parse_pages(pages, total)
    writer = PdfWriter()
    for i in idx:
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    resp = Response(content=buf.getvalue(), media_type="application/pdf")
    resp.headers["Content-Disposition"] = "attachment; filename=split.pdf"
    return resp


@router.get("/pdf-compress")
async def t_pdf_compress(url: str = Query(default="")):
    from pypdf import PdfReader, PdfWriter
    data = await _download(url)
    try:
        reader = PdfReader(io.BytesIO(data))
        writer = PdfWriter()
        for p in reader.pages:
            writer.add_page(p)
        for page in writer.pages:
            page.compress_content_streams()
        buf = io.BytesIO()
        writer.write(buf)
        out = buf.getvalue()
    except Exception:
        upstream_fail("Berkas bukan PDF yang valid.")
    resp = Response(content=out, media_type="application/pdf")
    resp.headers["Content-Disposition"] = "attachment; filename=compressed.pdf"
    resp.headers["X-RuangNode-Limited"] = "true"
    resp.headers["X-RuangNode-Note"] = (
        "Kompresi ringan via pypdf (tanpa ghostscript di server); "
        f"ukuran {len(data)} -> {len(out)} byte.")
    return resp


# ============================================================================
# saveweb: arsip halaman menjadi ZIP (ambil sendiri)
# ============================================================================
@router.get("/saveweb")
async def t_saveweb(url: str = Query(default="")):
    url = require_url(url)
    from ..common import fetch_text
    html, final_url = await fetch_text(url)
    assets = set()
    for m in re.finditer(
            r'''(?:src|href)=["']([^"'#]+?\.(?:png|jpe?g|gif|webp|svg|css|js|ico|woff2?|ttf))["']''',
            html, re.IGNORECASE):
        assets.add(urljoin(final_url, m.group(1)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html)
        host = urlparse(final_url).netloc
        for i, au in enumerate(list(assets)[:50]):
            try:
                ad = await _download(au, max_bytes=3 * 1024 * 1024,
                                     timeout=20.0)
                ext = os.path.splitext(urlparse(au).path)[1][:6] or ".bin"
                zf.writestr(f"assets/asset_{i:02d}{ext}", ad)
            except Exception:
                continue
    resp = Response(content=buf.getvalue(), media_type="application/zip")
    resp.headers["Content-Disposition"] = "attachment; filename=webpage.zip"
    return resp


# ============================================================================
# OCR via tesseract lokal (ind/eng terinstal di server)
# ============================================================================
@router.get("/ocr")
async def t_ocr(url: str = Query(default=""), lang: str = Query(default="ind")):
    data = await _download(url)
    img = _open_image(data)
    if max(img.size) > 4000:
        img.thumbnail((4000, 4000), Image.LANCZOS)
    lang = (lang or "ind").strip().lower()
    if lang not in ("ind", "eng", "ind+eng", "eng+ind"):
        lang = "ind"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        img.convert("RGB").save(tf.name, format="PNG")
        tmp = tf.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "tesseract", tmp, "stdout", "-l", lang, "--psm", "6",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        text = out.decode("utf-8", "ignore").strip()
    except FileNotFoundError:
        upstream_fail("Mesin OCR (tesseract) belum terinstal di server.")
    except asyncio.TimeoutError:
        upstream_fail("OCR kehabisan waktu.")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if not text:
        return ok(_limited_note({"text": ""},
                                "Tidak ada teks yang terdeteksi pada gambar."))
    return ok({"text": text, "lang": lang,
               "note": "OCR lokal via tesseract di server."})


# ============================================================================
# ceknsfw: penilaian via model vision 9Router (label jujur)
# ============================================================================
@router.get("/ceknsfw")
async def t_ceknsfw(image: str = Query(default="")):
    data = await _download(image)
    img = _open_image(data)
    img.thumbnail((768, 768), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    verdict = await ab.nine_vision(
        b64,
        "Klasifikasikan gambar ini. Jawab HANYA dengan format: "
        "SAFE atau UNSAFE, lalu tanda | lalu skor keyakinan 0-100. "
        "UNSAFE hanya jika gambar mengandung ketelanjangan, aktivitas seksual, "
        "atau kekerasan grafis yang eksplisit.",
        max_tokens=64)
    parts = verdict.split("|")
    label = parts[0].strip().upper()
    score = parts[1].strip() if len(parts) > 1 else "?"
    safe = label == "SAFE"
    return ok(_limited_note(
        {"safe": safe, "label": label, "confidence": score},
        "Penilaian oleh model vision umum (bukan model khusus NSFW); "
        "hasil berupa estimasi, bukan vonis pasti."))


# ============================================================================
# translate via model chat 9Router
# ============================================================================
@router.get("/translate")
async def t_translate(text: str = Query(default=""),
                      source: str = Query(default="auto"),
                      target: str = Query(default="id")):
    text = _need(text, "text")
    target = (target or "id").strip()
    source = (source or "auto").strip()
    answer = await ab.nine_chat(
        [{"role": "system",
          "content": (f"Kamu penerjemah. Terjemahkan teks pengguna dari bahasa "
                      f"{source} ke bahasa {target}. Jawab HANYA dengan hasil "
                      f"terjemahan, tanpa penjelasan.")},
         {"role": "user", "content": text}],
        model=ab.M_HAIKU, max_tokens=2048)
    return ok({"source": source, "target": target, "translated": answer,
               "note": "Terjemahan via model chat gateway 9Router milik RuangNode."})


# ============================================================================
# Audio / YouTube
# ============================================================================
def _youtube_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})", url)
    if not m:
        bad_request("URL YouTube tidak valid.")
    return m.group(1)


@router.get("/ytsum")
async def t_ytsum(url: str = Query(default=""), lang: str = Query(default="id")):
    from youtube_transcript_api import YouTubeTranscriptApi
    url = require_url(url)
    vid = _youtube_id(url)
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(vid, languages=[lang, "en", "id"])
        segments = fetched.snippets if hasattr(fetched, "snippets") else fetched
        text = " ".join(s.text if hasattr(s, "text") else s["text"]
                        for s in segments)
    except Exception as e:
        upstream_fail(f"Transkrip tidak tersedia untuk video ini ({type(e).__name__}).")
    if not text.strip():
        upstream_fail("Transkrip video kosong.")
    summary = await ab.nine_chat(
        [{"role": "system",
          "content": ("Kamu peringkas video YouTube. Buat ringkasan padat dalam "
                      "Bahasa Indonesia: 3-6 poin utama, tanpa basa-basi.")},
         {"role": "user", "content": text[:12000]}],
        model=ab.M_HAIKU, max_tokens=1024)
    return ok({"video_id": vid, "summary": summary,
               "note": "Ringkasan dari transkrip resmi video via model chat."})


@router.get("/findsong")
async def t_findsong(url: str = Query(default="")):
    from shazamio import Shazam
    data = await _download(url, max_bytes=15 * 1024 * 1024)
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        shazam = Shazam()
        out = await asyncio.wait_for(shazam.recognize(tmp), timeout=60)
    except asyncio.TimeoutError:
        upstream_fail("Pengenalan lagu kehabisan waktu.")
    except Exception:
        upstream_fail("Layanan pengenalan audio (Shazam) tidak dapat dihubungi.")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    track = out.get("track") or {}
    if not track:
        upstream_fail("Lagu tidak dikenali (audio tidak cocok dengan database).")
    images = (track.get("images") or {})
    hub = track.get("hub") or {}
    actions = hub.get("actions") or []
    listen = next((a.get("uri") for a in actions
                   if a.get("type") == "uri"), None)
    return ok({
        "title": track.get("title"),
        "artist": track.get("subtitle"),
        "shazam_url": track.get("url"),
        "cover": images.get("coverarthq") or images.get("coverart"),
        "listen": listen,
        "note": "Pengenalan audio via layanan Shazam.",
    })


@router.get("/removevocal")
async def t_removevocal(url: str = Query(default="")):
    data = await _download(url, max_bytes=20 * 1024 * 1024)
    with tempfile.NamedTemporaryFile(suffix=".in", delete=False) as fi, \
         tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fo:
        fi.write(data)
        inp, outp = fi.name, fo.name
    try:
        # Efek karaoke: kurangi kanal tengah (tempat vokal biasanya berada).
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-i", inp,
            "-af", "pan=stereo|c0=c0-c1|c1=c1-c0,aresample=44100",
            "-codec:a", "libmp3lame", "-b:a", "192k", outp,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        rc = await asyncio.wait_for(proc.wait(), timeout=180)
        if rc != 0 or os.path.getsize(outp) == 0:
            upstream_fail("ffmpeg gagal memproses audio.")
        result = open(outp, "rb").read()
    except asyncio.TimeoutError:
        upstream_fail("Pemrosesan audio kehabisan waktu.")
    finally:
        for p in (inp, outp):
            try:
                os.unlink(p)
            except OSError:
                pass
    resp = Response(content=result, media_type="audio/mpeg")
    resp.headers["Content-Disposition"] = "attachment; filename=novocal.mp3"
    resp.headers["X-RuangNode-Limited"] = "true"
    resp.headers["X-RuangNode-Note"] = (
        "Ekstraksi kanal tengah via ffmpeg (bukan model AI pemisah stems); "
        "hasil bervariasi tergantung mixing lagu.")
    return resp


# ============================================================================
# Shortener (API resmi masing-masing layanan)
# ============================================================================
@router.get("/tinyurl")
async def t_tinyurl(url: str = Query(default="")):
    url = require_url(url)
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            resp = await client.get("https://tinyurl.com/api-create.php",
                                    params={"url": url})
    except httpx.HTTPError:
        upstream_fail("Layanan TinyURL tidak dapat dihubungi.")
    short = resp.text.strip()
    if resp.status_code != 200 or not short.startswith("http"):
        upstream_fail("TinyURL menolak permintaan tersebut.")
    return ok({"original": url, "short": short, "service": "tinyurl"})


@router.get("/cleanuri")
async def t_cleanuri(url: str = Query(default="")):
    url = require_url(url)
    resp = await _post_form("https://cleanuri.com/api/v1/shorten",
                            {"url": url})
    try:
        data = resp.json()
        short = data["result_url"]
    except (ValueError, KeyError):
        upstream_fail("CleanURI menolak permintaan dari IP server "
                      "(API gratis CleanURI membatasi akses datacenter).")
    return ok({"original": url, "short": short, "service": "cleanuri"})


@router.get("/dagd")
async def t_dagd(url: str = Query(default="")):
    url = require_url(url)
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            resp = await client.get("https://da.gd/s", params={"url": url})
    except httpx.HTTPError:
        upstream_fail("Layanan da.gd tidak dapat dihubungi.")
    short = resp.text.strip()
    if resp.status_code != 200 or not short.startswith("http"):
        upstream_fail("da.gd menolak permintaan tersebut.")
    return ok({"original": url, "short": short, "service": "da.gd"})


@router.get("/spoome")
async def t_spoome(url: str = Query(default="")):
    url = require_url(url)
    resp = await _post_form("https://spoo.me/", {"url": url})
    final = str(resp.url)
    m = re.search(r"https?://spoo\.me/[A-Za-z0-9]+", resp.text)
    short = m.group(0) if m else (final if "spoo.me/" in final and final.rstrip("/") != "https://spoo.me" else None)
    if resp.status_code != 200 or not short:
        return ok(_limited_note(
            {"original": url},
            "spoo.me menolak pemendekan tanpa akun (butuh API key spoo.me)."))
    return ok({"original": url, "short": short, "service": "spoo.me"})


# ============================================================================
# pagespeed: Google PageSpeed Insights API resmi (tanpa key, kuota terbatas)
# ============================================================================
@router.get("/pagespeed")
async def t_pagespeed(url: str = Query(default=""),
                      strategy: str = Query(default="mobile")):
    url = require_url(url)
    strategy = (strategy or "mobile").lower()
    if strategy not in ("mobile", "desktop"):
        bad_request("strategy harus mobile atau desktop.")
    try:
        async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
            resp = await client.get(
                "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                params={"url": url, "strategy": strategy,
                        "category": ["performance", "seo"]})
    except httpx.HTTPError:
        upstream_fail("PageSpeed API tidak dapat dihubungi.")
    if resp.status_code == 429:
        upstream_fail("Kuota PageSpeed API tanpa key habis dari IP server (HTTP 429). "
                      "Coba lagi nanti atau gunakan API key Google.")
    if resp.status_code != 200:
        upstream_fail(f"PageSpeed API membalas HTTP {resp.status_code}.")
    try:
        data = resp.json()
        lr = data["lighthouseResult"]
        cats = lr.get("categories", {})
        audits = lr.get("audits", {})
        metrics = {}
        for k in ("first-contentful-paint", "largest-contentful-paint",
                  "total-blocking-time", "cumulative-layout-shift",
                  "speed-index", "interactive"):
            a = audits.get(k, {})
            metrics[k] = {"value": a.get("displayValue"),
                          "score": a.get("score")}
        return ok({
            "url": url, "strategy": strategy,
            "scores": {k: v.get("score") for k, v in cats.items()},
            "metrics": metrics,
        })
    except (KeyError, ValueError):
        upstream_fail("Respons PageSpeed API tidak terduga.")


# ============================================================================
# proxy: daftar proxy publik (feed publik GeoNode)
# ============================================================================
@router.get("/proxy")
async def t_proxy(limit: int = Query(default=20)):
    limit = max(1, min(100, limit))
    try:
        async with httpx.AsyncClient(timeout=25.0, trust_env=False) as client:
            resp = await client.get(
                "https://proxylist.geonode.com/api/proxy-list",
                params={"limit": limit, "page": 1,
                        "sort_by": "lastChecked", "sort_type": "desc"})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        upstream_fail("Feed proxy publik tidak dapat dihubungi.")
    out = []
    for p in data.get("data", []):
        proto = (p.get("protocols") or ["http"])[0]
        out.append({
            "proxy": f"{proto}://{p.get('ip')}:{p.get('port')}",
            "ip": p.get("ip"), "port": p.get("port"),
            "country": p.get("country"), "city": p.get("city"),
            "anonymity": p.get("anonymityLevel"),
            "protocols": p.get("protocols"),
        })
    return ok({"count": len(out), "proxies": out,
               "source": "proxylist.geonode.com (feed publik, tidak terverifikasi satu per satu)",
               "note": "Proxy publik gratis; kecepatan & umur pendek, gunakan dengan bijak."})


# ============================================================================
# Temp mail via mail.tm (API publik resmi)
# ============================================================================
MAILTM = "https://api.mail.tm"
# Kredensial akun yang dibuat via endpoint ini disimpan in-memory agar
# action inbox/read bisa dipakai tanpa user menghafal password.
_MAILTM_CREDS: dict[str, str] = {}


async def _mailtm(method: str, path: str, token: str = "",
                  body: dict | None = None) -> dict:
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=25.0, trust_env=False) as client:
            resp = await client.request(method, MAILTM + path,
                                        headers=headers,
                                        json=body if body else None)
    except httpx.HTTPError:
        upstream_fail("Layanan mail.tm tidak dapat dihubungi.")
    if resp.status_code in (401, 403):
        upstream_fail("Kredensial mail.tm tidak valid / kedaluwarsa.")
    if resp.status_code >= 400:
        upstream_fail(f"mail.tm membalas HTTP {resp.status_code}.")
    try:
        return resp.json()
    except ValueError:
        upstream_fail("Respons mail.tm tidak terduga.")


async def _mailtm_token(address: str) -> str:
    pw = _MAILTM_CREDS.get(address)
    if not pw:
        bad_request("Password akun tidak tersimpan di server. Buat alamat baru "
                    "via action=create lalu gunakan kembali alamatnya.")
    data = await _mailtm("POST", "/token",
                         body={"address": address, "password": pw})
    token = data.get("token")
    if not token:
        upstream_fail("Gagal login ke mail.tm.")
    return token


async def _mailtm_new_account(username: str, domain: str = "") -> dict:
    domains = await _mailtm("GET", "/domains")
    members = domains.get("hydra:member", [])
    if not members:
        upstream_fail("mail.tm tidak menyediakan domain aktif.")
    if domain:
        dom = next((d["domain"] for d in members
                    if d["domain"] == domain), None)
        if not dom:
            bad_request("Domain tidak tersedia. Cek action=domains.")
    else:
        dom = members[0]["domain"]
    username = re.sub(r"[^a-z0-9._-]", "", username.lower()) or "ruangnode"
    address = f"{username}@{dom}"
    password = "".join(random.choice(string.ascii_letters + string.digits)
                       for _ in range(16))
    data = await _mailtm("POST", "/accounts",
                         body={"address": address, "password": password})
    if "address" not in data:
        upstream_fail("mail.tm menolak pembuatan akun.")
    _MAILTM_CREDS[address] = password
    return {"address": address, "password": password,
            "note": "Simpan password ini; inbox memakai password yang sama."}


async def _mailtm_inbox(address: str) -> dict:
    token = await _mailtm_token(address)
    data = await _mailtm("GET", "/messages", token=token)
    msgs = []
    for m in data.get("hydra:member", []):
        frm = m.get("from") or {}
        msgs.append({"id": m.get("id"), "from": frm.get("address"),
                     "subject": m.get("subject"), "intro": m.get("intro"),
                     "date": m.get("createdAt"), "seen": m.get("seen")})
    return {"address": address, "count": len(msgs), "messages": msgs}


async def _mailtm_read(address: str, message_id: str) -> dict:
    token = await _mailtm_token(address)
    m = await _mailtm("GET", f"/messages/{message_id}", token=token)
    frm = m.get("from") or {}
    return {"id": m.get("id"), "from": frm.get("address"),
            "subject": m.get("subject"), "date": m.get("createdAt"),
            "text": m.get("text"), "html": bool(m.get("html"))}


@router.get("/genmail")
async def t_genmail(action: str = Query(default=""),
                    username: str = Query(default=""),
                    domain: str = Query(default=""),
                    email: str = Query(default=""),
                    link: str = Query(default="")):
    """action: domains | create | inbox | read(+link=messageId)."""
    action = (action or "").lower()
    if action == "domains":
        data = await _mailtm("GET", "/domains")
        return ok({"domains": [d["domain"]
                               for d in data.get("hydra:member", [])],
                   "service": "mail.tm"})
    if action == "create":
        return ok(await _mailtm_new_account(_need(username, "username"),
                                            domain.strip()))
    if action == "inbox":
        return ok(await _mailtm_inbox(_need(email, "email")))
    if action == "read":
        return ok(await _mailtm_read(_need(email, "email"),
                                     _need(link, "link (messageId)")))
    bad_request("action harus: domains | create | inbox | read.")


@router.get("/tempmail2")
async def t_tempmail2(action: str = Query(default=""),
                      alias: str = Query(default=""),
                      domain: str = Query(default=""),
                      email: str = Query(default=""),
                      messageId: str = Query(default="")):
    """action: create | inbox | read. Alias dari genmail."""
    action = (action or "").lower()
    if action == "create":
        return ok(await _mailtm_new_account(_need(alias, "alias"),
                                            domain.strip()))
    if action == "inbox":
        return ok(await _mailtm_inbox(_need(email, "email")))
    if action == "read":
        return ok(await _mailtm_read(_need(email, "email"),
                                     _need(messageId, "messageId")))
    bad_request("action harus: create | inbox | read.")


@router.get("/tempmail3")
async def t_tempmail3(action: str = Query(default=""),
                      email: str = Query(default="")):
    """action: create (email opsional -> acak) | inbox."""
    action = (action or "").lower()
    if action == "create":
        email = (email or "").strip()
        if "@" in email:
            username, _, domain = email.partition("@")
        else:
            username, domain = (email or "ruangnode"), ""
        return ok(await _mailtm_new_account(username or "ruangnode",
                                            domain.strip()))
    if action == "inbox":
        return ok(await _mailtm_inbox(_need(email, "email")))
    bad_request("action harus: create | inbox.")


# ============================================================================
# Patreon (API publik resmi, tanpa login)
# ============================================================================
PATREON_API = "https://www.patreon.com/api"


async def _patreon(path: str, params: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=25.0, trust_env=False) as client:
            resp = await client.get(PATREON_API + path, params=params,
                                    headers={"User-Agent": UA,
                                             "Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        upstream_fail(f"Patreon menolak request (HTTP {e.response.status_code}).")
    except httpx.HTTPError:
        upstream_fail("Patreon tidak dapat dihubungi.")
    except ValueError:
        upstream_fail("Respons Patreon tidak terduga.")


def _patreon_campaign(item: dict) -> dict:
    a = item.get("attributes", {})
    return {
        "id": item.get("id"),
        "name": a.get("name"),
        "vanity": a.get("vanity"),
        "summary": (a.get("summary") or "")[:300],
        "avatar": a.get("avatar_photo_url"),
        "url": a.get("url") or f"https://www.patreon.com/{a.get('vanity') or ''}",
        "patron_count": a.get("patron_count"),
        "is_monthly": a.get("is_monthly"),
    }


@router.get("/patreon-search")
async def t_patreon_search(q: str = Query(default="")):
    q = _need(q, "q")
    data = await _patreon("/search", {"q": q})
    out = [_patreon_campaign(i) for i in data.get("data", [])
           if i.get("type") == "campaign-document"]
    return ok({"query": q, "count": len(out), "campaigns": out,
               "note": "Hasil pencarian publik Patreon."})


@router.get("/patreon-creator")
async def t_patreon_creator(id: str = Query(default=""),
                            vanity: str = Query(default=""),
                            url: str = Query(default="")):
    cid = (id or "").strip()
    if not cid and vanity:
        data = await _patreon("/search", {"q": vanity.strip()})
        for i in data.get("data", []):
            if i.get("type") == "campaign-document":
                cid = str(i.get("id", "")).replace("campaign_", "")
                break
    if not cid and url:
        m = re.search(r"patreon\.com/(?:c/)?([\w-]+)", url)
        if m:
            data = await _patreon("/search", {"q": m.group(1)})
            for i in data.get("data", []):
                if i.get("type") == "campaign-document":
                    cid = str(i.get("id", "")).replace("campaign_", "")
                    break
    if not cid:
        bad_request("Parameter id / vanity / url wajib diisi salah satu.")
    data = await _patreon(f"/campaigns/{cid}")
    item = data.get("data") or {}
    if not item:
        upstream_fail("Kreator tidak ditemukan.")
    return ok(_patreon_campaign(item))


@router.get("/patreon-post")
async def t_patreon_post(id: str = Query(default=""),
                         url: str = Query(default="")):
    pid = (id or "").strip()
    if not pid and url:
        m = re.search(r"/posts/(\d+)", url)
        if m:
            pid = m.group(1)
    if not pid:
        bad_request("Parameter id / url wajib diisi salah satu.")
    data = await _patreon(f"/posts/{pid}")
    item = data.get("data") or {}
    a = item.get("attributes", {})
    if not item:
        upstream_fail("Post tidak ditemukan.")
    return ok({
        "id": item.get("id"),
        "title": a.get("title"),
        "content": (a.get("content") or a.get("teaser_text") or "")[:2000],
        "published_at": a.get("published_at"),
        "url": a.get("url"),
        "is_paid": a.get("is_paid"),
        "like_count": a.get("like_count"),
        "comment_count": a.get("comment_count"),
        "note": "Konten penuh post berbayar tidak dapat diakses tanpa login.",
    })


@router.get("/patreon-posts")
async def t_patreon_posts(id: str = Query(default=""),
                          count: int = Query(default=10)):
    cid = (id or "").strip()
    if not cid:
        bad_request("Parameter id wajib diisi.")
    count = max(1, min(50, count))
    data = await _patreon(f"/campaigns/{cid}/posts",
                          {"page[count]": count,
                           "sort": "-published_at"})
    out = []
    for i in data.get("data", []):
        a = i.get("attributes", {})
        out.append({"id": i.get("id"), "title": a.get("title"),
                    "published_at": a.get("published_at"),
                    "is_paid": a.get("is_paid"),
                    "like_count": a.get("like_count")})
    return ok({"campaign_id": cid, "count": len(out), "posts": out})


# ============================================================================
# ssweb: screenshot via chromium headless lokal (snap, tulis ke HOME user)
# ============================================================================
@router.get("/ssweb")
async def t_ssweb(url: str = Query(default="")):
    url = require_url(url)
    home = os.path.expanduser("~")
    out_path = os.path.join(home, f"ssweb_{os.getpid()}.png")
    try:
        proc = await asyncio.create_subprocess_exec(
            "chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
            "--hide-scrollbars",
            f"--screenshot={out_path}", "--window-size=1280,800", url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=90)
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            upstream_fail("Chromium gagal membuat screenshot.")
        with open(out_path, "rb") as f:
            png = f.read()
    except FileNotFoundError:
        upstream_fail("Chromium belum terinstal di server.")
    except asyncio.TimeoutError:
        upstream_fail("Screenshot kehabisan waktu.")
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    if not png.startswith(b"\x89PNG"):
        upstream_fail("Hasil screenshot bukan PNG valid.")
    return Response(content=png, media_type="image/png")


# ============================================================================
# ngl: kirim satu pesan via alur publik resmi ngl.link
# ============================================================================
@router.get("/ngl")
async def t_ngl(text: str = Query(default=""), id: str = Query(default="")):
    text = _need(text, "text")
    target = _need(id, "id").lstrip("@").strip()
    if len(text) > 300:
        bad_request("Pesan maksimal 300 karakter.")
    try:
        async with httpx.AsyncClient(timeout=25.0, trust_env=False) as client:
            resp = await client.post(
                "https://ngl.link/api/submit",
                headers={"User-Agent": UA, "Content-Type": "application/json",
                         "Referer": f"https://ngl.link/{target}"},
                json={"username": target, "question": text,
                      "deviceId": "ruangnode-api"})
    except httpx.HTTPError:
        upstream_fail("NGL tidak dapat dihubungi.")
    if resp.status_code != 200:
        upstream_fail(f"NGL menolak pengiriman (HTTP {resp.status_code}).")
    try:
        qid = resp.json().get("questionId")
    except ValueError:
        qid = None
    return ok(_limited_note(
        {"to": target, "questionId": qid},
        "NGL membalas questionId; terkirimnya pesan ke inbox tidak bisa "
        "diverifikasi independen dari server."))


# ============================================================================
# cekresi / xlcheck / xlcircle: GAGAL jujur (bukti di bawah)
# ============================================================================
@router.get("/cekresi")
async def t_cekresi(resi: str = Query(default=""),
                    kurir: str = Query(default="")):
    _need(resi, "resi")
    upstream_fail(
        "Pelacakan resi tidak tersedia: situs resmi kurir (JNE: halaman "
        "tracking lama 404; SiCepat: aplikasi JS tanpa API publik) memblokir "
        "akses server-side, dan API publik tanpa key tidak ada. "
        "Butuh API key layanan pelacakan (mis. BinderByte).")


@router.get("/xlcheck")
async def t_xlcheck(number: str = Query(default="")):
    _need(number, "number")
    upstream_fail(
        "Cek kuota XL/Axis butuh login OTP ke nomor pemilik via aplikasi "
        "MyXL (alur preLogin my.xl.co.id mewajibkan kode OTP yang dikirim ke "
        "nomor tersebut). Tanpa OTP dari pemilik nomor, tidak bisa diakses.")


@router.get("/xlcircle")
async def t_xlcircle(number: str = Query(default="")):
    _need(number, "number")
    upstream_fail(
        "Cek XL Circle memakai akun MyXL yang sama (butuh OTP ke nomor "
        "pemilik). Tanpa OTP dari pemilik nomor, tidak bisa diakses.")


# ============================================================================
# DITOLAK: spamotp & alightmotion (keputusan pemilik)
# ============================================================================
@router.get("/spamotp")
async def t_spamotp(phone: str = Query(default="")):
    from fastapi import HTTPException
    raise HTTPException(
        status_code=403,
        detail="Endpoint tidak disediakan: pengiriman OTP berulang ke nomor "
               "orang lain adalah pelecehan/spam dan tidak akan dibangun.")


@router.get("/alightmotion")
async def t_alightmotion(email: str = Query(default=""),
                         options: str = Query(default=""),
                         url: str = Query(default="")):
    from fastapi import HTTPException
    raise HTTPException(
        status_code=403,
        detail="Endpoint tidak disediakan: aktivasi premium ilegal untuk "
               "aplikasi berbayar adalah pembajakan dan tidak akan dibangun.")
