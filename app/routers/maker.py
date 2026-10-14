"""Kategori Maker — 6 endpoint generator teks & gambar.

blurface, brat, carbon, emojikitchen, qc, tonude.

Teknik yang dipakai (diadaptasi dari pola yang terbukti jalan):
- blurface: deteksi wajah Haar cascade OpenCV (haarcascade_frontalface_default.xml
  bawaan paket opencv-python-headless) + Gaussian blur per area wajah.
- brat: kotak hijau brat (#8ACE00) + teks DejaVu Sans Condensed Bold; di-render
  lalu di-downscale dan di-upscale kembali agar tampak low-fi seperti meme aslinya.
- carbon: gambar snippet kode ala carbon.now.sh — syntax highlighting via pygments,
  font DejaVu Sans Mono, lampu macOS, nomor baris.
- emojikitchen: metadata komunitas emojikitchen.dev
  (xsalazar/emoji-kitchen-backend) yang memetakan pasangan emoji ke gStaticUrl
  resmi Google; server mengambil PNG-nya lalu meneruskannya.
- qc: quote canvas ala QuotLyBot — bubble, avatar lingkaran, nama, teks
  terbungkus; murni Pillow lokal.
- tonude: DITOLAK fungsional (keputusan pemilik) — nudify gambar orang asli adalah
  konten intim non-konsensual; membalas 403 dengan alasan jelas.
"""

import asyncio
import io
import json
import textwrap
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Query, Response
from fastapi import HTTPException
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from ..common import bad_request, require_url, upstream_fail
from ..security import require_api_token

router = APIRouter(prefix="/api/maker", dependencies=[Depends(require_api_token)])

_FONTS = "/usr/share/fonts/truetype/dejavu"
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_EK_META = _DATA_DIR / "emojikitchen-metadata.json"
_EK_META_URL = ("https://raw.githubusercontent.com/xsalazar/"
                "emoji-kitchen-backend/main/app/metadata.json")

_ek_cache: dict | None = None


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(f"{_FONTS}/{name}", size)


def _need(value: str | None, name: str) -> str:
    value = (value or "").strip()
    if not value:
        bad_request(f"Parameter {name} wajib diisi.")
    return value


def _png_response(img: Image.Image) -> Response:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


async def _download_bytes(url: str, max_bytes: int = 15 * 1024 * 1024) -> bytes:
    url = require_url(url)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                     trust_env=False) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                chunks, total = [], 0
                async for chunk in r.aiter_bytes(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        bad_request("Berkas terlalu besar (maks 15 MB).")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPStatusError as e:
        upstream_fail(f"Sumber menolak request (HTTP {e.response.status_code}).")
    except httpx.HTTPError:
        upstream_fail("Gagal mengunduh dari URL tersebut.")


def _open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        bad_request("URL tidak menunjuk ke gambar yang valid.")
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")
    return img


# ============================================================================
# 1. Blur Face — deteksi wajah Haar cascade + Gaussian blur
# ============================================================================
def _blur_faces(data: bytes) -> tuple[Image.Image, int]:
    import cv2
    import numpy as np

    nparr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        bad_request("URL tidak menunjuk ke gambar yang valid.")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade_path = (cv2.data.haarcascades
                    + "haarcascade_frontalface_default.xml")
    cascade = cv2.CascadeClassifier(cascade_path)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                     minSize=(30, 30))
    for (x, y, w, h) in faces:
        k = max(3, (min(w, h) // 3) | 1)  # kernel ganjil proporsional wajah
        if k % 2 == 0:
            k += 1
        roi = img[y:y + h, x:x + w]
        img[y:y + h, x:x + w] = cv2.GaussianBlur(roi, (k, k), 0)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb), len(faces)


@router.get("/blurface")
async def m_blurface(url: str = Query(default="")):
    """Mengaburkan wajah pada sebuah gambar dari URL."""
    data = await _download_bytes(url)
    try:
        img, count = await asyncio.to_thread(_blur_faces, data)
    except ImportError:
        upstream_fail("Modul deteksi wajah belum terinstal di server.")
    resp = _png_response(img)
    resp.headers["X-Faces-Blurred"] = str(count)
    return resp


# ============================================================================
# 2. Brat Text Generator — kotak hijau brat + teks low-fi
# ============================================================================
_BRAT_GREEN = (138, 206, 0)


@router.get("/brat")
async def m_brat(text: str = Query(default="")):
    """Membuat gambar teks gaya brat: kotak hijau dengan teks putih blur khas meme."""
    text = _need(text, "text")[:200]

    def render() -> Image.Image:
        size = 640
        img = Image.new("RGB", (size, size), _BRAT_GREEN)
        draw = ImageDraw.Draw(img)
        font_size = 120
        lines: list[str] = []
        while font_size >= 24:
            font = _font("DejaVuSansCondensed-Bold.ttf", font_size)
            avg = max(1, int(font_size * 0.5))
            wrap_w = max(1, (size - 80) // avg)
            lines = []
            for para in text.split("\n"):
                lines.extend(textwrap.wrap(para, width=wrap_w) or [""])
            widths = [draw.textlength(l, font=font) for l in lines] or [0]
            lh = int(font_size * 1.05)
            if max(widths) <= size - 60 and len(lines) * lh <= size - 60:
                break
            font_size -= 8
        font = _font("DejaVuSansCondensed-Bold.ttf", font_size)
        lh = int(font_size * 1.05)
        total_h = len(lines) * lh
        y = (size - total_h) // 2
        for line in lines:
            w = draw.textlength(line, font=font)
            draw.text(((size - w) / 2, y), line, font=font, fill="white")
            y += lh
        # Efek low-fi khas brat: kecilkan lalu besarkan kembali
        small = img.resize((160, 160), Image.LANCZOS)
        return small.resize((size, size), Image.BILINEAR)

    return _png_response(await asyncio.to_thread(render))


# ============================================================================
# 3. Carbon Code Snippet — pygments + jendela kode
# ============================================================================
def _render_carbon(code: str) -> Image.Image:
    from pygments import lex
    from pygments.lexers import PythonLexer, guess_lexer
    from pygments.styles import get_style_by_name
    from pygments.token import Token

    try:
        lexer = guess_lexer(code)
    except Exception:
        lexer = PythonLexer()
    style = get_style_by_name("monokai")

    def tok_color(ttype) -> str:
        info = style.style_for_token(ttype)
        c = info.get("color")
        return ("#" + c) if c else "#f8f8f2"

    font = _font("DejaVuSansMono.ttf", 16)
    pad, header_h, line_h = 28, 52, 26
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    raw_lines = code.replace("\t", "    ").splitlines() or [""]
    if len(raw_lines) > 200:
        raw_lines = raw_lines[:200]
    # Bungkus baris yang terlalu panjang agar lebar gambar wajar
    lines: list[str] = []
    for ln in raw_lines:
        lines.extend(textwrap.wrap(ln, width=90) or [""])
    gutter_w = int(tmp.textlength(str(len(lines)) + "  ", font=font))
    code_w = max(tmp.textlength(l, font=font) for l in lines)
    win_w = int(code_w + gutter_w + pad * 2)
    win_h = header_h + len(lines) * line_h + pad

    outer = 40
    canvas = Image.new("RGB", (win_w + outer * 2, win_h + outer * 2), "#151515")
    draw = ImageDraw.Draw(canvas)
    wx, wy = outer, outer
    draw.rounded_rectangle([wx, wy, wx + win_w, wy + win_h], radius=14,
                           fill="#272822")
    # Lampu macOS
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        cx = wx + 26 + i * 26
        draw.ellipse([cx, wy + 18, cx + 16, wy + 34], fill=c)
    # Nomor baris
    for i, line in enumerate(lines, start=1):
        y = wy + header_h + (i - 1) * line_h
        draw.text((wx + pad, y), str(i), font=font, fill="#75715e")
    # Kode dengan highlight per token
    tokens = list(lex("\n".join(lines), lexer))
    x0 = wx + pad + gutter_w
    y = wy + header_h
    x = x0
    for ttype, tval in tokens:
        color = tok_color(ttype)
        for ch in tval:
            if ch == "\n":
                y += line_h
                x = x0
                continue
            draw.text((x, y), ch, font=font, fill=color)
            x += tmp.textlength(ch, font=font)
    return canvas


@router.get("/carbon")
async def m_carbon(code: str = Query(default="")):
    """Membuat gambar snippet kode cantik ala carbon.now.sh dari teks kode."""
    code = _need(code, "code")[:20000]
    return _png_response(await asyncio.to_thread(_render_carbon, code))


# ============================================================================
# 4. Emoji Kitchen — stiker gabungan dua emoji via metadata resmi Google
# ============================================================================
def _emoji_to_cp(emoji: str) -> str:
    return "-".join(f"{ord(c):x}" for c in emoji)


def _cp_variants(emoji: str) -> list[str]:
    """Kandidat codepoint: apa adanya, tanpa FE0F, dan dengan FE0F.

    Pengirim sering mengirim varian yang berbeda (mis. U+2764 vs U+2764 U+FE0F)
    sementara metadata Kitchen hanya mengenal salah satunya.
    """
    base = _emoji_to_cp(emoji)
    cands = [base]
    if base.endswith("-fe0f"):
        cands.append(base[:-5])
    else:
        cands.append(base + "-fe0f")
    return cands


def _ek_load() -> dict:
    global _ek_cache
    if _ek_cache is None:
        if not _EK_META.exists():
            upstream_fail("Database Emoji Kitchen belum tersedia di server.")
        with open(_EK_META, encoding="utf-8") as f:
            _ek_cache = json.load(f)
    return _ek_cache


def _ek_find_url(meta: dict, cp1: str, cp2: str) -> str | None:
    data = meta.get("data", {})
    for a, b in ((cp1, cp2), (cp2, cp1)):
        combos = data.get(a, {}).get("combinations", {}).get(b)
        if combos:
            for entry in combos:
                if entry.get("isLatest"):
                    return entry.get("gStaticUrl")
            return combos[0].get("gStaticUrl")
    return None


@router.get("/emojikitchen")
async def m_emojikitchen(emoji1: str = Query(default=""),
                         emoji2: str = Query(default="")):
    """Menggabungkan dua emoji menjadi satu stiker ala Emoji Kitchen Google."""
    e1 = _need(emoji1, "emoji1")
    e2 = _need(emoji2, "emoji2")
    if len(e1) > 12 or len(e2) > 12:
        bad_request("Parameter emoji1/emoji2 harus berupa satu emoji.")
    meta = await asyncio.to_thread(_ek_load)
    url = None
    for c1 in _cp_variants(e1):
        for c2 in _cp_variants(e2):
            url = await asyncio.to_thread(_ek_find_url, meta, c1, c2)
            if url:
                break
        if url:
            break
    if not url:
        upstream_fail("Kombinasi emoji tersebut tidak tersedia di Emoji Kitchen.")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=25.0,
                                     trust_env=False) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
    except httpx.HTTPError:
        upstream_fail("Gambar stiker Emoji Kitchen tidak dapat diunduh.")
    if not data.startswith(b"\x89PNG"):
        upstream_fail("Respons Emoji Kitchen bukan gambar yang valid.")
    return Response(content=data, media_type="image/png")


# ============================================================================
# 5. Quote Canvas (QC) — bubble kutipan ala QuotLyBot
# ============================================================================
def _circle_avatar(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _initial_avatar(name: str, size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (108, 92, 231, 255))
    draw = ImageDraw.Draw(img)
    font = _font("DejaVuSans-Bold.ttf", size // 2)
    ch = (name.strip()[:1] or "?").upper()
    w = draw.textlength(ch, font=font)
    bb = font.getbbox(ch)
    draw.text(((size - w) / 2, (size - (bb[3] - bb[1])) / 2 - bb[1]),
              ch, font=font, fill="white")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _render_qc(text: str, nama: str, avatar: Image.Image | None,
               bg: tuple[int, int, int]) -> Image.Image:
    W = 720
    font_name = _font("DejaVuSans-Bold.ttf", 34)
    font_msg = _font("DejaVuSans.ttf", 32)
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    wrap_w = 34
    msg_lines: list[str] = []
    for para in text.split("\n"):
        msg_lines.extend(textwrap.wrap(para, width=wrap_w) or [""])
    name_h = 44
    msg_h = len(msg_lines) * 46
    bubble_pad, av = 36, 96
    content_h = max(av, name_h + 8 + msg_h) + bubble_pad * 2
    H = content_h + 72

    canvas = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(canvas)
    bx, by = 36, 36
    bw, bh = W - 72, content_h
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=28,
                           fill="white")
    ax, ay = bx + 32, by + 32
    if avatar is None:
        avatar = _initial_avatar(nama, av)
    canvas.paste(avatar, (ax, ay), avatar)
    tx = ax + av + 24
    draw.text((tx, ay + 2), nama, font=font_name, fill="#2b7de9")
    my = ay + name_h + 10
    for line in msg_lines:
        draw.text((tx, my), line, font=font_msg, fill="#111111")
        my += 46
    return canvas


@router.get("/qc")
async def m_qc(text: str = Query(default=""),
               nama: str = Query(default=""),
               url: str | None = Query(default=None),
               color: str | None = Query(default=None)):
    """Membuat gambar quote canvas: bubble kutipan dengan avatar, nama, dan teks."""
    text = _need(text, "text")[:1000]
    nama = _need(nama, "nama")[:60]
    bg = (15, 20, 25)
    if color:
        try:
            bg = ImageColor.getrgb(color.strip())[:3]
        except ValueError:
            bad_request("Parameter color tidak valid (gunakan nama warna atau hex).")
    avatar = None
    if url:
        data = await _download_bytes(url)
        avatar = await asyncio.to_thread(_circle_avatar, _open_image(data), 96)
    img = await asyncio.to_thread(_render_qc, text, nama, avatar, bg)
    return _png_response(img)


# ============================================================================
# DITOLAK: tonude (keputusan pemilik)
# ============================================================================
@router.get("/tonude")
async def m_tonude(url: str = Query(default="")):
    raise HTTPException(
        status_code=403,
        detail="Endpoint tidak disediakan: membuat versi telanjang dari foto "
               "orang asli adalah konten intim non-konsensual dan tidak akan "
               "dibangun.")
