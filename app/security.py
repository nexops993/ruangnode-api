"""Helper keamanan RuangNode API: Access Token, sesi, anti brute-force.

Dipakai main.py (halaman web) dan semua router API. ACCESS_TOKEN dan
SESSION_SECRET dibaca dari environment (di VPS: /etc/ruangnode-api/env).
"""

import hashlib
import hmac
import os
import re
import time

from fastapi import HTTPException, Request

ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")

# API key publik (keputusan pemilik 2026-10-14): API dibuka untuk siapa pun
# tanpa pendaftaran, cukup sertakan key ini. ACCESS_TOKEN pribadi pemilik
# tetap berlaku sebagai kunci kedua (akses penuh yang sama).
PUBLIC_APIKEY = "aditttdev"
SESSION_DAYS = 7
COOKIE_NAME = "rn_session"

# ----------------------------------------------------------------------------
# Anti brute-force: kunci per-IP setelah beberapa kegagalan token.
# ----------------------------------------------------------------------------
FAIL_WINDOW = 600        # detik
FAIL_MAX = 5             # kegagalan maksimum dalam jendela
LOCK_SECONDS = 900       # lama kunci
_fails: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def client_ip(request: Request) -> str:
    # Di balik Cloudflare Tunnel, IP asli pengunjung ada di CF-Connecting-IP.
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    return request.client.host if request.client else "unknown"


def is_locked(ip: str) -> bool:
    until = _locked_until.get(ip)
    if until and time.time() < until:
        return True
    if until:
        _locked_until.pop(ip, None)
        _fails.pop(ip, None)
    return False


def record_fail(ip: str) -> None:
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    recent.append(now)
    _fails[ip] = recent
    if len(recent) >= FAIL_MAX:
        _locked_until[ip] = now + LOCK_SECONDS


def clear_fails(ip: str) -> None:
    _fails.pop(ip, None)


# ----------------------------------------------------------------------------
# Rate limit per-IP untuk request API yang SAH (menjaga stabilitas server
# karena API key kini publik): longgar untuk pemakaian wajar & bot.
# ----------------------------------------------------------------------------
RATE_WINDOW = 600        # detik
RATE_MAX = 600           # request sah maksimum per IP dalam jendela
_hits: dict[str, list[float]] = {}


def rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
    _hits[ip] = recent
    return len(recent) >= RATE_MAX


def record_hit(ip: str) -> None:
    now = time.time()
    recent = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
    recent.append(now)
    _hits[ip] = recent


# ----------------------------------------------------------------------------
# Token & sesi
# ----------------------------------------------------------------------------
def clean_token(value: str | None) -> str:
    """Buang SEMUA whitespace — token tempelan chat sering membawa baris baru."""
    return re.sub(r"\s+", "", value or "")


def _sign(ts: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts)}"


def valid_session(cookie_val: str | None) -> bool:
    if not cookie_val or "." not in cookie_val:
        return False
    ts, sig = cookie_val.split(".", 1)
    if not ts.isdigit():
        return False
    if not hmac.compare_digest(_sign(ts), sig):
        return False
    if int(time.time()) - int(ts) > SESSION_DAYS * 24 * 3600:
        return False
    return True


def page_allowed(request: Request) -> bool:
    return valid_session(request.cookies.get(COOKIE_NAME))


def token_from_request(request: Request) -> str | None:
    token = request.headers.get("X-Access-Token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        token = request.query_params.get("token")
    if not token:
        token = request.query_params.get("apikey")
    return clean_token(token) or None


def require_api_token(request: Request) -> None:
    """Dependency/handler gerbang API: 401 tanpa token benar, 429 saat terkunci."""
    ip = client_ip(request)
    if is_locked(ip):
        raise HTTPException(
            status_code=429,
            detail="Terlalu banyak percobaan token yang salah dari IP ini. "
                   "Coba lagi dalam 15 menit.",
        )
    token = token_from_request(request)
    valid = bool(token) and (
        hmac.compare_digest(token, ACCESS_TOKEN)
        or hmac.compare_digest(token, PUBLIC_APIKEY)
    )
    if not valid:
        record_fail(ip)
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: API key tidak ada atau salah. "
                   "Sertakan API key publik lewat query ?apikey=, "
                   "header X-Access-Token, atau Authorization: Bearer <apikey>.",
        )
    clear_fails(ip)
    if rate_limited(ip):
        raise HTTPException(
            status_code=429,
            detail="Batas pemakaian wajar per IP tercapai (600 request / 10 menit). "
                   "Kurangi kecepatan request Anda.",
        )
    record_hit(ip)
