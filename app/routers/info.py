"""Kategori Info — 8 endpoint informasi jaringan & domain.

cekapikey, dnslookup, iplookup, leaderboard, portscan, sslcheck,
webstatus, whois.

Teknik yang dipakai (diadaptasi dari pola yang terbukti jalan):
- dnslookup: DNS-over-HTTPS JSON milik Cloudflare (publik, tanpa key).
- iplookup: API JSON gratis ip-api.com (tanpa key).
- leaderboard: penghitung hit in-memory milik server sendiri (app/stats.py).
- portscan: koneksi TCP asyncio dengan batas konkurensi + batas 100 port.
- sslcheck: handshake TLS via modul ssl bawaan Python.
- webstatus: HEAD lalu GET via httpx (pola umum uptime checker).
- whois: protokol WHOIS mentah port 43 via whois.iana.org + referral.
"""

import asyncio
import hmac
import re
import socket
import ssl
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Query, Request

from ..common import bad_request, ok, upstream_fail
from ..security import ACCESS_TOKEN, clean_token, require_api_token
from .. import stats

router = APIRouter(prefix="/api/info", dependencies=[Depends(require_api_token)])

_DOMAIN_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})+$")
_HOST_RE = re.compile(r"^(?!-)[A-Za-z0-9.-]{1,253}(?<!-)$")

_DOH_URL = "https://cloudflare-dns.com/dns-query"
_DOH_TYPES = {"A": 1, "AAAA": 28, "MX": 15, "NS": 2, "TXT": 16, "CNAME": 5}

COMMON_PORTS = [21, 22, 23, 25, 53, 80, 110, 143, 443, 445, 465, 587,
                993, 995, 1433, 1521, 1723, 3306, 3389, 5432, 5900,
                6379, 8080, 8443, 27017]
MAX_SCAN_PORTS = 100


def _valid_domain(value: str | None) -> str:
    value = (value or "").strip().lower().rstrip(".")
    if not value or not _DOMAIN_RE.match(value):
        bad_request("Parameter domain tidak valid.")
    return value


def _resolve_target(host: str) -> str:
    """Validasi host lalu resolve ke IP (untuk portscan)."""
    host = (host or "").strip().rstrip(".")
    if not host or not _HOST_RE.match(host):
        bad_request("Parameter host tidak valid.")
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        upstream_fail("Host tidak bisa di-resolve ke alamat IP.")


def _parse_ports(spec: str | None) -> list[int]:
    spec = (spec or "common").strip().lower()
    if spec == "common":
        return list(COMMON_PORTS)
    ports: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            ports.update(range(max(1, lo), min(65535, hi) + 1))
        else:
            ports.add(int(part))
    ports = {p for p in ports if 1 <= p <= 65535}
    if not ports:
        bad_request("Daftar port tidak valid.")
    if len(ports) > MAX_SCAN_PORTS:
        bad_request(f"Maksimal {MAX_SCAN_PORTS} port per pemindaian.")
    return sorted(ports)


async def _probe(ip: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _cert_name(seq) -> str:
    parts = {}
    for item in seq or []:
        for k, v in item:
            parts[k] = v
    return parts.get("commonName", "")


# ----------------------------------------------------------------------------
# 1. Cek API Key
# ----------------------------------------------------------------------------
@router.get("/cekapikey")
async def cek_apikey(request: Request, token: str | None = Query(default=None)):
    """Memeriksa validitas sebuah Access Token RuangNode."""
    if token:
        valid = bool(clean_token(token)) and hmac.compare_digest(
            clean_token(token), ACCESS_TOKEN)
        return ok({"valid": valid, "checked": "token pada parameter"})
    return ok({"valid": True, "checked": "token yang dipakai memanggil endpoint ini",
               "info": "Token benar karena request lolos gerbang autentikasi."})


# ----------------------------------------------------------------------------
# 2. DNS Lookup (DNS-over-HTTPS Cloudflare)
# ----------------------------------------------------------------------------
@router.get("/dnslookup")
async def dns_lookup(domain: str = Query(...),
                     type: str = Query(default="ALL")):
    domain = _valid_domain(domain)
    want = (type or "ALL").strip().upper()
    types = list(_DOH_TYPES) if want == "ALL" else [want]
    if any(t not in _DOH_TYPES for t in types):
        bad_request("Tipe record tidak dikenal. Pilihan: A, AAAA, MX, NS, TXT, CNAME, ALL.")
    out: dict[str, list[str]] = {}
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            for t in types:
                r = await client.get(
                    _DOH_URL,
                    params={"name": domain, "type": t},
                    headers={"Accept": "application/dns-json",
                             "User-Agent": "RuangNode-API/1.0"})
                r.raise_for_status()
                data = r.json()
                out[t] = [a.get("data", "") for a in data.get("Answer", [])]
    except httpx.HTTPError:
        upstream_fail("Layanan DNS-over-HTTPS tidak dapat dihubungi.")
    return ok({"domain": domain, "records": out})


# ----------------------------------------------------------------------------
# 3. IP Lookup (ip-api.com)
# ----------------------------------------------------------------------------
@router.get("/iplookup")
async def ip_lookup(request: Request, ip: str | None = Query(default=None)):
    target = (ip or "").strip()
    if not target:
        from ..security import client_ip
        target = client_ip(request)
    fields = ("status,message,country,countryCode,region,regionName,city,zip,"
              "lat,lon,timezone,isp,org,as,query")
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.get(f"http://ip-api.com/json/{target}",
                                 params={"fields": fields})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError:
        upstream_fail("Layanan IP lookup tidak dapat dihubungi.")
    if data.get("status") != "success":
        upstream_fail(f"Lookup gagal: {data.get('message', 'unknown')}.")
    data.pop("status", None)
    return ok(data)


# ----------------------------------------------------------------------------
# 4. Leaderboard (penghitung internal server)
# ----------------------------------------------------------------------------
@router.get("/leaderboard")
async def leaderboard(limit: int = Query(default=10, ge=1, le=50)):
    snap = stats.snapshot()
    ranked = sorted(snap["hits"].items(), key=lambda kv: kv[1], reverse=True)
    return ok({
        "total_hits": snap["total"],
        "uptime_seconds": snap["uptime_seconds"],
        "catatan": "Penghitung dimulai ulang setiap service restart.",
        "top": [{"path": p, "hits": n} for p, n in ranked[:limit]],
    })


# ----------------------------------------------------------------------------
# 5. Port Scanner
# ----------------------------------------------------------------------------
@router.get("/portscan")
async def port_scan(host: str = Query(...),
                    ports: str | None = Query(default="common")):
    ip = await asyncio.to_thread(_resolve_target, host)
    want = await asyncio.to_thread(_parse_ports, ports)
    sem = asyncio.Semaphore(50)
    t0 = time.monotonic()

    async def check(p: int):
        async with sem:
            return p, await _probe(ip, p, 1.5)

    results = await asyncio.gather(*(check(p) for p in want))
    open_ports = sorted(p for p, is_open in results if is_open)
    return ok({
        "host": host, "ip": ip,
        "scanned": len(want),
        "open": open_ports,
        "closed_count": len(want) - len(open_ports),
        "duration_ms": round((time.monotonic() - t0) * 1000),
    })


# ----------------------------------------------------------------------------
# 6. SSL Certificate Checker
# ----------------------------------------------------------------------------
def _fetch_cert(domain: str) -> dict:
    ctx = ssl.create_default_context()
    with socket.create_connection((domain, 443), timeout=12) as raw:
        with ctx.wrap_socket(raw, server_hostname=domain) as tls:
            cert = tls.getpeercert()
            tls_version = tls.version()
    fmt = "%b %d %H:%M:%S %Y %Z"
    nb = datetime.strptime(cert["notBefore"], fmt).replace(tzinfo=timezone.utc)
    na = datetime.strptime(cert["notAfter"], fmt).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    sans = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
    return {
        "domain": domain,
        "subject_cn": _cert_name(cert.get("subject")),
        "issuer": _cert_name(cert.get("issuer")),
        "serial_number": cert.get("serialNumber"),
        "not_before": nb.isoformat(),
        "not_after": na.isoformat(),
        "expired": na < now,
        "days_remaining": max(0, (na - now).days),
        "san": sans,
        "tls_version": tls_version,
    }


@router.get("/sslcheck")
async def ssl_check(domain: str = Query(...)):
    domain = _valid_domain(domain)
    try:
        info = await asyncio.to_thread(_fetch_cert, domain)
    except (socket.timeout, ConnectionRefusedError, ssl.SSLError, OSError):
        upstream_fail("Tidak bisa mengambil sertifikat SSL dari domain tersebut.")
    return ok(info)


# ----------------------------------------------------------------------------
# 7. Website Status Checker
# ----------------------------------------------------------------------------
@router.get("/webstatus")
async def web_status(url: str = Query(...)):
    from ..common import require_url
    url = require_url(url)
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0,
                                     trust_env=False) as client:
            try:
                r = await client.head(url)
                if r.status_code in (400, 403, 404, 405, 501):
                    raise httpx.HTTPError("head ditolak")
            except httpx.HTTPError:
                async with client.stream("GET", url) as stream_resp:
                    resp = stream_resp
            else:
                resp = r
            ms = round((time.monotonic() - t0) * 1000)
            return ok({
                "url": url,
                "final_url": str(resp.url),
                "status_code": resp.status_code,
                "reason": resp.reason_phrase,
                "response_time_ms": ms,
                "server": resp.headers.get("server"),
                "content_type": resp.headers.get("content-type"),
                "content_length": resp.headers.get("content-length"),
            })
    except httpx.HTTPError:
        upstream_fail("Website tidak dapat dihubungi (timeout/koneksi gagal).")


# ----------------------------------------------------------------------------
# 8. WHOIS Domain
# ----------------------------------------------------------------------------
def _whois_query(server: str, query: str, timeout: float = 12.0) -> str:
    with socket.create_connection((server, 43), timeout=timeout) as s:
        s.sendall((query + "\r\n").encode("utf-8", "ignore"))
        chunks = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode("utf-8", "ignore")


def _whois_lookup(domain: str) -> dict:
    text = _whois_query("whois.iana.org", domain)
    referral = None
    for line in text.splitlines():
        if line.lower().startswith("whois:"):
            referral = line.split(":", 1)[1].strip()
            break
    if referral:
        try:
            text = _whois_query(referral, domain)
        except OSError:
            pass
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("%", "#", ">")) or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if v and v.lower() != "not defined":
            fields.setdefault(k, []).append(v)

    def pick(*names):
        for n in names:
            if n in fields:
                return fields[n][0]
        return None

    def pick_all(*names):
        out = []
        for n in names:
            out.extend(fields.get(n, []))
        seen, uniq = set(), []
        for x in out:
            xl = x.lower()
            if xl not in seen:
                seen.add(xl)
                uniq.append(x)
        return uniq

    return {
        "domain": domain,
        "registrar": pick("registrar"),
        "creation_date": pick("creation date", "created", "registered on"),
        "updated_date": pick("updated date", "last updated", "last-modified"),
        "expiry_date": pick("registry expiry date", "expiry date", "expires",
                            "expiration date", "paid-till"),
        "status": pick_all("domain status", "status"),
        "name_servers": pick_all("name server", "nserver"),
        "registrant_country": pick("registrant country"),
        "whois_server": referral,
    }


@router.get("/whois")
async def whois_lookup(domain: str = Query(...)):
    domain = _valid_domain(domain)
    try:
        info = await asyncio.to_thread(_whois_lookup, domain)
    except OSError:
        upstream_fail("Server WHOIS tidak dapat dihubungi.")
    if not info.get("registrar") and not info.get("creation_date"):
        upstream_fail("Data WHOIS tidak ditemukan untuk domain tersebut.")
    return ok(info)
