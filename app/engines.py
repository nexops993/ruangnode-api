"""Mesin unduhan berlapis: yt-dlp (jalur utama, dipakai di router)
+ Cobalt (fallback) + utilitas relay file.

Cobalt = engine downloader self-hosted (imputnet/cobalt) yang berjalan di VPS
ini sebagai service systemd `cobalt` di 127.0.0.1:9000 dengan auth Api-Key.
Skema request/respons mengikuti docs/api.md resmi cobalt:
POST / {"url", "downloadMode", "videoQuality", "audioFormat", ...}
  -> {"status": "tunnel"/"redirect"/"picker"/"local-processing"/"error", ...}
GET / -> {"cobalt": {"version", "services", ...}}
"""

import os
import re
import tempfile

import httpx

COBALT_URL = os.environ.get("COBALT_URL", "http://127.0.0.1:9000").rstrip("/")
COBALT_API_KEY = os.environ.get("COBALT_API_KEY", "")

_POST_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


def _headers() -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if COBALT_API_KEY:
        h["Authorization"] = f"Api-Key {COBALT_API_KEY}"
    return h


def cobalt_configured() -> bool:
    return bool(COBALT_URL)


async def cobalt_ping() -> tuple[bool, str]:
    """Cek cepat instance Cobalt hidup. Kembalikan (ok, info_singkat)."""
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.get(f"{COBALT_URL}/")
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        d = resp.json()
        cob = d.get("cobalt") or {}
        return True, f"v{cob.get('version')}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}"


async def cobalt_fetch(url: str, mode: str = "auto",
                       video_quality: str = "1080",
                       audio_format: str = "mp3") -> tuple[dict | None, str]:
    """Minta Cobalt memproses URL. Kembalikan (hasil_normal, pesan_error).

    hasil_normal selalu memuat: engine="cobalt", cobalt_status, dan
    url/filename/tunnels/items sesuai statusnya.
    """
    payload = {
        "url": url,
        "downloadMode": mode,
        "videoQuality": video_quality,
        "audioFormat": audio_format,
        "filenameStyle": "basic",
    }
    try:
        async with httpx.AsyncClient(timeout=_POST_TIMEOUT,
                                     trust_env=False) as client:
            resp = await client.post(f"{COBALT_URL}/", json=payload,
                                     headers=_headers())
    except httpx.HTTPError as e:
        return None, f"cobalt: {type(e).__name__}"
    except Exception as e:  # noqa: BLE001
        return None, f"cobalt: {type(e).__name__}"
    if resp.status_code != 200:
        return None, f"cobalt: HTTP {resp.status_code}"
    try:
        d = resp.json()
    except ValueError:
        return None, "cobalt: respons bukan JSON"
    status = d.get("status")
    if status in ("tunnel", "redirect"):
        return {
            "engine": "cobalt",
            "cobalt_status": status,
            "url": d.get("url"),
            "filename": d.get("filename"),
        }, ""
    if status == "picker":
        return {
            "engine": "cobalt",
            "cobalt_status": "picker",
            "items": d.get("picker") or [],
            "audio": d.get("audio"),
            "audioFilename": d.get("audioFilename"),
        }, ""
    if status == "local-processing":
        return {
            "engine": "cobalt",
            "cobalt_status": "local-processing",
            "tunnels": d.get("tunnel") or [],
            "process_type": d.get("type"),
            "service": d.get("service"),
            "filename": ((d.get("output") or {}).get("filename")),
        }, ""
    err = (d.get("error") or {})
    code = err.get("code") or status or "unknown"
    return None, f"cobalt gagal: {code}"


def cobalt_tunnel_url(data: dict) -> str | None:
    """Ambil satu tautan file dari hasil cobalt_fetch yang sukses."""
    if data.get("cobalt_status") in ("tunnel", "redirect"):
        return data.get("url")
    tunnels = data.get("tunnels") or []
    return tunnels[0] if tunnels else None


def title_from_filename(filename: str | None) -> str | None:
    """'Video Title (1080p, h264).mp4' -> 'Video Title'."""
    if not filename:
        return None
    name = re.sub(r"\.(mp4|mp3|webm|mkv|m4a|ogg|wav|opus|gif)$", "", filename,
                  flags=re.I)
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    return name or None


async def download_to_temp(url: str, suffix: str = ".bin",
                           timeout: float = 600.0) -> tuple[str, int]:
    """Stream URL (mis. tunnel Cobalt) ke file temp. Kembalikan (path, bytes)."""
    fd, path = tempfile.mkstemp(prefix="rn_cobalt_", suffix=suffix)
    total = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout),
                                     trust_env=False) as client:
            async with client.stream("GET", url,
                                     headers={"User-Agent":
                                              "Mozilla/5.0"}) as resp:
                resp.raise_for_status()
                with os.fdopen(fd, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
                        total += len(chunk)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path, total
