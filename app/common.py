"""Helper bersama untuk router-router API RuangNode."""

import re

import httpx
from fastapi import HTTPException

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
}


def ok(result) -> dict:
    """Envelope sukses seragam."""
    return {"status": True, "creator": "RuangNode", "result": result}


def bad_request(message: str):
    raise HTTPException(status_code=400, detail=message)


def upstream_fail(message: str):
    raise HTTPException(status_code=502, detail=message)


def require_url(url: str | None) -> str:
    url = (url or "").strip()
    if not url:
        bad_request("Parameter url wajib diisi.")
    if not re.match(r"^https?://", url):
        bad_request("Parameter url tidak valid (harus diawali http:// atau https://).")
    return url


def safe_filename(name: str, fallback: str = "file") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._")
    return name or fallback


async def fetch_text(url: str, headers: dict | None = None, timeout: float = 30.0,
                     follow_redirects: bool = True) -> tuple[str, str]:
    """Ambil halaman; kembalikan (teks, url_final_setelah_redirect)."""
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    try:
        async with httpx.AsyncClient(follow_redirects=follow_redirects, timeout=timeout,
                                     trust_env=False) as client:
            resp = await client.get(url, headers=h)
            resp.raise_for_status()
            return resp.text, str(resp.url)
    except httpx.HTTPStatusError as e:
        upstream_fail(f"Sumber menolak request (HTTP {e.response.status_code}).")
    except httpx.HTTPError as e:
        upstream_fail(f"Gagal menghubungi sumber: {type(e).__name__}.")


async def fetch_bytes(url: str, headers: dict | None = None, timeout: float = 60.0) -> bytes:
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                     trust_env=False) as client:
            resp = await client.get(url, headers=h)
            resp.raise_for_status()
            return resp.content
    except httpx.HTTPStatusError as e:
        upstream_fail(f"Sumber menolak request (HTTP {e.response.status_code}).")
    except httpx.HTTPError as e:
        upstream_fail(f"Gagal menghubungi sumber: {type(e).__name__}.")
