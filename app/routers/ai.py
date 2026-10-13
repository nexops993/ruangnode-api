"""Kategori AI (21 endpoint).

Backend: infrastruktur milik pemilik sendiri, bukan agregator pihak ketiga.
- 9Router (api.pixelspace.my.id, 581 model): chat, persona, vision.
- Cloudflare Workers AI milik pemilik: text-to-image (flux-1-schnell).
- Library lokal: edge-tts (TTS), Pillow (filter gambar), ffmpeg (efek audio/video).

Model gateway yang TERVERIFIKASI menjawab (uji live): cl/openai/gpt-6.1-sol-pro,
cl/anthropic/claude-sonnet-5.5 (+haiku-5.5), cf/@cf/mistralai/mistral-small-3.1-24b-instruct,
cl/qwen/qwen3.8-max-prime, cl/google/gemma-3-27b-it, cl/qwen/qwen2.5-vl-72b-instruct.
"""

import asyncio
import base64
import io
import os
import re
import subprocess
import tempfile

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from PIL import Image, ImageFilter, ImageOps

from .. import ai_backend as ab
from ..common import (
    DEFAULT_HEADERS,
    bad_request,
    ok,
    require_url,
    upstream_fail,
)
from ..security import require_api_token

router = APIRouter(prefix="/api/ai", dependencies=[Depends(require_api_token)])

MAX_IMG_BYTES = 8 * 1024 * 1024


def _need(text: str | None, name: str) -> str:
    text = (text or "").strip()
    if not text:
        bad_request(f"Parameter {name} wajib diisi.")
    return text


async def _download_media(url: str, max_bytes: int = MAX_IMG_BYTES) -> bytes:
    """Unduh berkas dari URL; gagal -> 502 jujur."""
    url = require_url(url)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0,
                                     trust_env=False) as client:
            resp = await client.get(url, headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]})
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


def _image_response(img_bytes: bytes, fmt: str = "jpeg") -> Response:
    mt = "image/jpeg" if fmt == "jpeg" else f"image/{fmt}"
    return Response(content=img_bytes, media_type=mt)


# ============================================================================
# 1-4, 19-20: chat teks via gateway
# ============================================================================
@router.get("/chatgpt")
async def ai_chatgpt(prompt: str = Query(default=""),
                     chat_id: str = Query(default=""),
                     auth: str = Query(default="")):
    prompt = _need(prompt, "prompt")
    system = ("Kamu adalah asisten AI yang membantu, menjawab ringkas dalam "
              "Bahasa Indonesia.")
    session = (chat_id or "").strip() or "chatgpt"
    messages = ab.SESSIONS.build(session, system, prompt)
    answer = await ab.nine_chat(messages, model=ab.M_CHATGPT)
    ab.SESSIONS.add(session, "user", prompt)
    ab.SESSIONS.add(session, "assistant", answer)
    return ok({"model": ab.M_CHATGPT, "session": session, "answer": answer,
               "note": "Chat via gateway 9Router milik RuangNode; "
                       "parameter auth tidak dipakai (akses sudah memakai Access Token)."})


@router.get("/mistral")
async def ai_mistral(query: str = Query(default="")):
    query = _need(query, "query")
    answer = await ab.nine_chat(
        [{"role": "system", "content": "Kamu adalah Mistral AI yang membantu, menjawab ringkas dalam Bahasa Indonesia."},
         {"role": "user", "content": query}],
        model=ab.M_MISTRAL)
    return ok({"model": ab.M_MISTRAL, "answer": answer,
               "note": "Model Mistral (mistral-small-3.1-24b) disajikan via Cloudflare Workers AI milik RuangNode."})


@router.get("/qwen3")
async def ai_qwen3(q: str = Query(default="")):
    q = _need(q, "q")
    answer = await ab.nine_chat(
        [{"role": "system", "content": "Kamu adalah Qwen3 AI yang membantu, menjawab ringkas dalam Bahasa Indonesia."},
         {"role": "user", "content": q}],
        model=ab.M_QWEN)
    return ok({"model": ab.M_QWEN, "answer": answer,
               "note": "Model Qwen via gateway 9Router milik RuangNode."})


@router.get("/perplexity")
async def ai_perplexity(query: str = Query(default="")):
    query = _need(query, "query")
    answer = await ab.nine_chat(
        [{"role": "system", "content": ("Kamu menjawab seperti mesin jawab AI riset: "
                                       "langsung ke jawaban, sebutkan poin-poin kunci, "
                                       "Bahasa Indonesia.")},
         {"role": "user", "content": query}],
        model=ab.M_CHATGPT, max_tokens=1500)
    return ok({"model": ab.M_CHATGPT, "answer": answer,
               "note": "Jawaban dari model chat RuangNode; bukan pencarian web "
                       "langsung ala Perplexity asli."})


@router.get("/bypassai")
async def ai_bypassai(q: str = Query(default="")):
    q = _need(q, "q")
    answer = await ab.nine_chat(
        [{"role": "system", "content": ("Tulis ulang teks berikut agar terdengar natural "
                                       "seperti ditulis manusia berbahasa Indonesia: variasikan "
                                       "panjang kalimat, hindari pola kaku khas AI, jaga makna "
                                       "dan fakta asli. Keluarkan hanya hasil tulis ulang.")},
         {"role": "user", "content": q}],
        model=ab.M_CHATGPT, max_tokens=1500)
    return ok({"original": q, "rewritten": answer,
               "note": "Parafrase dibantu model chat; hasil bervariasi dan tidak "
                       "dijamin lolos semua detektor."})


def _ai_probability_heuristics(text: str) -> dict:
    """Heuristik lokal keteraturan teks (bukan Grammarly)."""
    words = re.findall(r"[A-Za-zÀ-ž]+", text.lower())
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    if not words or not sentences:
        return {"score": 0.0, "signals": ["teks terlalu pendek untuk dinilai"]}
    avg_len = sum(len(s.split()) for s in sentences) / len(sentences)
    lens = [len(s.split()) for s in sentences]
    mean = sum(lens) / len(lens)
    var = sum((x - mean) ** 2 for x in lens) / len(lens)
    cv = (var ** 0.5 / mean) if mean else 0.0
    unique_ratio = len(set(words)) / len(words)
    clichés = ["selain itu", "selanjutnya", "dalam dunia yang", "secara keseluruhan",
               "penting untuk dicatat", "perlu diingat", "tidak hanya", "tetapi juga"]
    cliché_hits = sum(text.lower().count(c) for c in clichés)
    score = 0.5
    signals = []
    if cv < 0.45:
        score += 0.15; signals.append("panjang kalimat sangat seragam")
    if unique_ratio > 0.85:
        score += 0.10; signals.append("variasi kata tinggi tanpa pengulangan alami")
    if cliché_hits:
        score += min(0.15, 0.05 * cliché_hits); signals.append(f"{cliché_hits} frasa khas AI")
    if 12 <= avg_len <= 25:
        score += 0.05; signals.append("panjang kalimat rata-rata khas tulisan AI")
    score = max(0.0, min(0.99, score))
    return {"score": round(score, 2), "signals": signals or ["tidak ada sinyal kuat"]}


@router.get("/detectai")
async def ai_detectai(q: str = Query(default="")):
    q = _need(q, "q")
    heu = _ai_probability_heuristics(q)
    verdict = ("kemungkinan besar ditulis AI" if heu["score"] >= 0.65
               else "kemungkinan campuran AI/manusia" if heu["score"] >= 0.4
               else "kemungkinan besar ditulis manusia")
    return ok({"text_preview": q[:120], "ai_probability": heu["score"],
               "signals": heu["signals"], "verdict": verdict,
               "note": "ESTIMASI dari heuristik lokal di server RuangNode — "
                       "bukan Grammarly; jangan dipakai sebagai vonis pasti."})


# ============================================================================
# 5-9: persona chat + multi-model
# ============================================================================
RUANGBOT_SYSTEM = ("Kamu adalah RuangNode Assistant, asisten AI milik RuangNode "
                   "yang ramah, membantu, dan berbicara Bahasa Indonesia santai. "
                   "Jawabanmu ringkas dan to the point.")


@router.get("/kuroneko")
async def ai_kuroneko(q: str = Query(default=""), session: str = Query(default="")):
    q = _need(q, "q")
    sid = (session or "").strip() or "kuroneko"
    messages = ab.SESSIONS.build(sid, RUANGBOT_SYSTEM, q)
    answer = await ab.nine_chat(messages, model=ab.M_CLAUDE)
    ab.SESSIONS.add(sid, "user", q)
    ab.SESSIONS.add(sid, "assistant", answer)
    return ok({"model": ab.M_CLAUDE, "session": sid, "answer": answer})


FEEB_SYSTEM = ("Kamu adalah Feel Better Bot, teman yang suportif dan empatik. "
               "Dengarkan keluh kesah pengguna, validasi perasaannya, beri dorongan "
               "hangat dan saran kecil yang realistis. Bahasa Indonesia yang lembut. "
               "Kamu bukan pengganti bantuan profesional.")


@router.get("/feeb")
async def ai_feeb(q: str = Query(default=""), session: str = Query(default="")):
    q = _need(q, "q")
    sid = (session or "").strip() or "feeb"
    messages = ab.SESSIONS.build(sid, FEEB_SYSTEM, q)
    answer = await ab.nine_chat(messages, model=ab.M_HAIKU)
    ab.SESSIONS.add(sid, "user", q)
    ab.SESSIONS.add(sid, "assistant", answer)
    return ok({"model": ab.M_HAIKU, "session": sid, "answer": answer})


PERSONAS = {
    "netral": "Kamu asisten netral yang membantu.",
    "santai": "Kamu teman ngobrol santai Bahasa Indonesia, gaya gaul tapi sopan.",
    "formal": "Kamu asisten formal dan profesional.",
    "kocak": "Kamu asisten yang humoris dan suka bercanda ringan.",
    "bijak": "Kamu penasihat bijak yang tenang dan reflektif.",
}


@router.get("/notrack")
async def ai_notrack(q: str = Query(default=""), persona: str = Query(default="netral")):
    q = _need(q, "q")
    key = (persona or "").strip().lower()
    system = PERSONAS.get(key)
    if system is None:
        system = PERSONAS["netral"]
        note = f"Persona '{persona}' tidak dikenal — dipakai 'netral'. Pilihan: {', '.join(PERSONAS)}."
    else:
        note = "Mode tanpa jejak: sesi ini tidak disimpan."
    answer = await ab.nine_chat(
        [{"role": "system", "content": system + " Jawab ringkas dalam Bahasa Indonesia."},
         {"role": "user", "content": q}],
        model=ab.M_CHATGPT)
    return ok({"model": ab.M_CHATGPT, "persona": key if key in PERSONAS else "netral",
               "answer": answer, "note": note})


@router.get("/nova")
async def ai_nova(q: str = Query(default="")):
    q = _need(q, "q")
    answer = await ab.nine_chat(
        [{"role": "system", "content": "Kamu Nova AI: asisten cepat dan langsung ke intinya, Bahasa Indonesia."},
         {"role": "user", "content": q}],
        model=ab.M_NOVA, max_tokens=800)
    return ok({"model": ab.M_NOVA, "answer": answer})


AISEEK_MODELS = {
    "gpt": ab.M_CHATGPT, "claude": ab.M_CLAUDE, "mistral": ab.M_MISTRAL,
    "qwen": ab.M_QWEN, "nova": ab.M_NOVA,
}


@router.get("/aiseek")
async def ai_aiseek(prompt: str = Query(default=""), model: str = Query(default="claude"),
                    action: str = Query(default="chat"),
                    session_id: str = Query(default="")):
    prompt = _need(prompt, "prompt")
    action = (action or "").strip().lower()
    if action == "image":
        img = await ab.cf_run_image(prompt)
        return _image_response(img)
    key = (model or "").strip().lower()
    chosen = AISEEK_MODELS.get(key)
    if chosen is None:
        bad_request(f"Model '{model}' tidak dikenal. Pilihan: {', '.join(sorted(AISEEK_MODELS))} atau action=image.")
    sid = (session_id or "").strip() or "aiseek"
    messages = ab.SESSIONS.build(sid, "Kamu asisten AI yang membantu; jawab ringkas dalam Bahasa Indonesia.", prompt)
    answer = await ab.nine_chat(messages, model=chosen)
    ab.SESSIONS.add(sid, "user", prompt)
    ab.SESSIONS.add(sid, "assistant", answer)
    return ok({"model": chosen, "session": sid, "answer": answer})


# ============================================================================
# 10-12, 16: gambar — Cloudflare flux (T2I) + vision gateway
# ============================================================================
@router.get("/flux")
async def ai_flux(prompt: str = Query(default="")):
    prompt = _need(prompt, "prompt")
    img = await ab.cf_run_image(prompt)
    return _image_response(img)


ART_STYLES = ("digital painting yang detail dan artistik",)


@router.get("/aiart")
async def ai_aiart(q: str = Query(default="")):
    q = _need(q, "q")
    img = await ab.cf_run_image(f"{q}, {ART_STYLES[0]}")
    return _image_response(img)


@router.get("/toonmix")
async def ai_toonmix(q: str = Query(default=""), image: str = Query(default="")):
    q = _need(q, "q")
    image = (image or "").strip()
    if not image:
        img = await ab.cf_run_image(f"{q}, gaya kartun/toon yang ceria")
        return _image_response(img)
    # I2I: backend img2img Cloudflare tidak diizinkan untuk akun ini
    # (403 saat diuji) -> jujur TERBATAS, tapi tetap hasilkan versi kartun
    # dari deskripsi gambar sumber.
    raw = await _download_media(image)
    try:
        b64 = base64.b64encode(raw[:2_000_000]).decode()
        desc = await ab.nine_vision(
            b64, "Deskripsikan objek utama dan latar gambar ini dalam satu kalimat Bahasa Inggris.",
            max_tokens=120)
    except Exception:  # noqa: BLE001
        desc = "a portrait"
    img = await ab.cf_run_image(f"cartoon toon style version of: {desc}. {q}")
    return Response(
        content=img, media_type="image/jpeg",
        headers={"X-RuangNode-Limited": "true",
                 "X-RuangNode-Note": ("Image-to-image tidak tersedia (akun Cloudflare tidak "
                                      "diizinkan memakai model img2img); gambar di atas adalah "
                                      "hasil generate ulang gaya kartun dari deskripsi gambar sumber.")},
    )


@router.get("/img2prompt")
async def ai_img2prompt(url: str = Query(default="")):
    raw = await _download_media(url)
    b64 = base64.b64encode(raw[:2_000_000]).decode()
    prompt_text = await ab.nine_vision(
        b64,
        ("Analisis gambar ini lalu tulis SATU prompt generator gambar (Bahasa Inggris) "
         "yang mendeskripsikan subjek, gaya, komposisi, pencahayaan, dan warna. "
         "Keluarkan hanya prompt-nya."),
        max_tokens=300)
    return ok({"model": ab.M_VISION, "prompt": prompt_text,
               "note": "Prompt dibuat oleh model vision via gateway 9Router milik RuangNode."})


# ============================================================================
# 13-15: editor/filter gambar — jujur soal keterbatasan
# ============================================================================
@router.get("/nanobanana")
async def ai_nanobanana(url: str = Query(default=""), prompt: str = Query(default="")):
    _need(url, "url"); _need(prompt, "prompt")
    upstream_fail(
        "TERBATAS: edit gambar berbasis prompt (ala Nano Banana) belum tersedia — "
        "tidak ada model image-editing yang hidup di backend RuangNode (model img2img "
        "Cloudflare tidak diizinkan untuk akun ini, 403 saat diuji). Gambar sumber tidak "
        "diubah sama sekali agar tidak mengarang hasil.")


@router.get("/animetoreal")
async def ai_animetoreal(url: str = Query(default="")):
    _need(url, "url")
    upstream_fail(
        "TERBATAS: konversi anime->realistis butuh model image-to-image; model img2img "
        "Cloudflare tidak diizinkan untuk akun ini (403 saat diuji). Tidak ada hasil "
        "yang dibuat agar tidak menipu.")


FILTERS = {
    "grayscale": "Hitam-putih (grayscale).",
    "blur": "Blur lembut.",
    "sharpen": "Pertajam detail.",
    "edge": "Deteksi tepi (sketsa).",
    "emboss": "Efek emboss 3D.",
    "invert": "Negatif (invert warna).",
}


def _apply_local_filter(raw: bytes, name: str) -> bytes:
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:  # noqa: BLE001
        bad_request("Berkas di URL bukan gambar yang bisa dibaca.")
    if name == "grayscale":
        im = ImageOps.grayscale(im).convert("RGB")
    elif name == "blur":
        im = im.filter(ImageFilter.GaussianBlur(6))
    elif name == "sharpen":
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=150))
    elif name == "edge":
        im = im.filter(ImageFilter.FIND_EDGES)
    elif name == "emboss":
        im = im.filter(ImageFilter.EMBOSS)
    elif name == "invert":
        im = ImageOps.invert(im)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


@router.get("/aifilter")
async def ai_aifilter(url: str = Query(default=""), filter: str = Query(default="grayscale")):
    raw = await _download_media(url)
    name = (filter or "").strip().lower()
    if name not in FILTERS:
        bad_request(f"Filter '{filter}' tidak dikenal. Pilihan: {', '.join(sorted(FILTERS))}.")
    out = await asyncio.to_thread(_apply_local_filter, raw, name)
    return Response(
        content=out, media_type="image/jpeg",
        headers={"X-RuangNode-Note": f"Filter lokal (Pillow) di server RuangNode: {FILTERS[name]} — bukan model AI generatif."},
    )


# ============================================================================
# 17-18, 21: audio/video — edge-tts, ffmpeg
# ============================================================================
TTS_VOICES = {
    "female": "id-ID-GadisNeural",
    "gadis": "id-ID-GadisNeural",
    "male": "id-ID-ArdiNeural",
    "ardi": "id-ID-ArdiNeural",
    "en-female": "en-US-AriaNeural",
    "en-male": "en-US-GuyNeural",
}


@router.get("/tts")
async def ai_tts(text: str = Query(default=""), model: str = Query(default="female")):
    text = _need(text, "text")
    if len(text) > 2000:
        bad_request("Panjang text maksimal 2000 karakter.")
    key = (model or "").strip().lower()
    voice = TTS_VOICES.get(key)
    if voice is None:
        if re.match(r"^[a-z]{2}-[A-Z]{2}-", model or ""):
            voice = model  # izinkan voice id edge-tts langsung
        else:
            bad_request(f"Suara '{model}' tidak dikenal. Pilihan: {', '.join(sorted(TTS_VOICES))}.")
    try:
        import edge_tts  # type: ignore
    except ImportError:
        upstream_fail("Library edge-tts belum terpasang di server.")

    def _speak() -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out = f.name
        try:
            async def _run():
                comm = edge_tts.Communicate(text, voice)
                await comm.save(out)
            asyncio.run(_run())
            with open(out, "rb") as fh:
                data = fh.read()
            return data
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass

    data = await asyncio.to_thread(_speak)
    if not data or not data.startswith(b"\xff\xfb") and not data.startswith(b"ID3"):
        upstream_fail("Hasil TTS bukan berkas MP3 valid.")
    return Response(content=data, media_type="audio/mpeg")


def _run_ffmpeg(args: list[str]) -> None:
    try:
        subprocess.run(["ffmpeg", "-y", *args],
                       capture_output=True, timeout=120, check=True)
    except FileNotFoundError:
        upstream_fail("ffmpeg belum terpasang di server.")
    except subprocess.TimeoutExpired:
        upstream_fail("ffmpeg kehabisan waktu memproses media.")
    except subprocess.CalledProcessError:
        upstream_fail("ffmpeg gagal memproses media.")


@router.get("/text2vid")
async def ai_text2vid(prompt: str = Query(default=""), ratio: str = Query(default="16:9"),
                      sound: str = Query(default="")):
    prompt = _need(prompt, "prompt")
    ratio = (ratio or "16:9").strip()
    size = "1280x720" if ratio == "16:9" else "720x1280" if ratio == "9:16" else "1280x720"
    # Tidak ada model text-to-video di backend -> animasi zoom sederhana
    # dari gambar hasil generate, dilabeli JUJUR sebagai TERBATAS.
    img = await ab.cf_run_image(prompt)
    with tempfile.TemporaryDirectory() as td:
        frame = os.path.join(td, "frame.jpg")
        out = os.path.join(td, "out.mp4")
        with open(frame, "wb") as f:
            f.write(img)
        _run_ffmpeg([
            "-loop", "1", "-i", frame,
            "-vf", f"scale=1600:900,zoompan=z='min(zoom+0.0018,1.25)':d=90:s={size}:fps=30",
            "-t", "3", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", out,
        ])
        with open(out, "rb") as f:
            data = f.read()
    if len(data) < 1024:
        upstream_fail("Gagal merakit video.")
    return Response(
        content=data, media_type="video/mp4",
        headers={"X-RuangNode-Limited": "true",
                 "X-RuangNode-Note": ("TERBATAS: bukan model text-to-video — ini animasi zoom 3 detik "
                                      "dari gambar hasil generate (ffmpeg lokal), karena tidak ada "
                                      "model video yang hidup di backend RuangNode.")},
    )


@router.get("/zeta")
async def ai_zeta(url: str = Query(default="")):
    raw = await _download_media(url, max_bytes=25 * 1024 * 1024)
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in")
        out = os.path.join(td, "out.mp3")
        with open(src, "wb") as f:
            f.write(raw)
        # Naikkan pitch ~35% (efek suara "waifu") via asetrate + aresample.
        _run_ffmpeg([
            "-i", src,
            "-af", "asetrate=44100*1.35,aresample=44100,atempo=1/1.15",
            "-c:a", "libmp3lame", "-b:a", "128k", out,
        ])
        with open(out, "rb") as f:
            data = f.read()
    if not data or not (data.startswith(b"\xff\xfb") or data.startswith(b"ID3") or data.startswith(b"\xff\xf3")):
        upstream_fail("Gagal menghasilkan audio MP3.")
    return Response(
        content=data, media_type="audio/mpeg",
        headers={"X-RuangNode-Note": "Efek pitch-shift lokal (ffmpeg) di server RuangNode — bukan model voice conversion."},
    )
