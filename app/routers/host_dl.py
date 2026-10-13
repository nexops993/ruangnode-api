"""Kategori Download — grup FILE HOST & LAINNYA.

Endpoint: mediafire, gofile, pixeldrain, krakenfiles, uploadhaven, terabox,
mega, scribd, mcpe, github, applemusic.

Prinsip: hanya memakai halaman/API RESMI tiap layanan atau library lokal di
server sendiri (mega.py) — tanpa agregator pihak ketiga. Setiap endpoint
dilabeli jujur: AKTIF (terbukti), TERBATAS (sebagian + penyebab), atau
GAGAL (penyebab teknis jelas).
"""

import asyncio
import hashlib
import html as ihtml
import json
import os
import re
import time

import httpx
from fastapi import APIRouter, Depends, Query

from ..common import (
    DEFAULT_HEADERS,
    bad_request,
    fetch_text,
    ok,
    require_url,
    upstream_fail,
)
from ..security import require_api_token

router = APIRouter(prefix="/api/download", dependencies=[Depends(require_api_token)])

NAV_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _meta_tag(html: str, prop: str):
    m = re.search(
        r'<meta[^>]+(?:property|name)="'
        + re.escape(prop) + r'"[^>]+content="([^"]+)"',
        html, re.I,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="'
            + re.escape(prop) + r'"',
            html, re.I,
        )
    return ihtml.unescape(m.group(1)).strip() if m else None


def _page_title(html: str):
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    return ihtml.unescape(m.group(1)).strip() if m else None


# ============================================================================
# mediafire — AKTIF: tombol unduh langsung di HTML halaman file
# ============================================================================
@router.get("/mediafire")
async def download_mediafire(url: str = Query(default="")):
    url = require_url(url)
    if "mediafire.com" not in url:
        bad_request("URL bukan tautan MediaFire.")
    html, final_url = await fetch_text(url, headers=NAV_HEADERS)
    if "The file you are trying to download" in html and "no longer available" in html:
        upstream_fail("File MediaFire tidak tersedia (dihapus/diblokir).")
    m = (re.search(r'aria-label="Download file"[^>]*href="([^"]+)"', html)
         or re.search(r'href="([^"]+)"[^>]*aria-label="Download file"', html))
    if not m:
        upstream_fail("Tombol unduh tidak ditemukan di halaman MediaFire "
                      "(struktur berubah atau file tidak publik).")
    direct = ihtml.unescape(m.group(1)).replace("&amp;", "&")
    name = _meta_tag(html, "og:title") or _page_title(html)
    size = re.search(r'File size:\s*</?[^>]*>?\s*([\d.,]+\s*[KMG]B)', html, re.I)
    return ok({
        "filename": name,
        "size": size.group(1) if size else None,
        "webpage_url": final_url,
        "download": {"kind": "single", "url": direct},
        "note": "Tautan unduh langsung dari CDN MediaFire (hasil parse tombol resmi).",
    })


# ============================================================================
# gofile — AKTIF: API resmi + X-Website-Token ala klien web
# (metode didokumentasikan project open source martadams89/gofile-dl:
#  wt = sha256(UA::lang::accountToken::window4jam::salt))
# ============================================================================
GOFILE_UA = os.environ.get(
    "GOFILE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
GOFILE_LANG = os.environ.get("GOFILE_LANGUAGE", "en-US")
GOFILE_WT_SALT = os.environ.get("GOFILE_WT_SALT", "12af056dacea0b")


def _gofile_wt(account_token: str, offset: int = 0) -> str:
    window = int(time.time() // 14400) + offset
    raw = f"{GOFILE_UA}::{GOFILE_LANG}::{account_token}::{window}::{GOFILE_WT_SALT}"
    return hashlib.sha256(raw.encode()).hexdigest()


@router.get("/gofile")
async def download_gofile(url: str = Query(default="")):
    url = require_url(url)
    m = re.search(r"gofile\.io/d/([A-Za-z0-9]+)", url)
    content_id = m.group(1) if m else url.strip().rstrip("/").split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9]+", content_id or ""):
        bad_request("ID konten GoFile tidak dikenali (format: https://gofile.io/d/XXXX).")

    async def _api(method: str, api_url: str, **kw):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                         trust_env=False) as client:
                resp = await client.request(method, api_url, **kw)
                return resp
        except httpx.HTTPError:
            upstream_fail("Gagal menghubungi API GoFile.")

    r = await _api("POST", "https://api.gofile.io/accounts",
                   headers={"User-Agent": GOFILE_UA})
    try:
        token = r.json()["data"]["token"]
    except (ValueError, KeyError):
        upstream_fail("Gagal membuat akun tamu GoFile.")
    params = {"contentFilter": "", "page": 1, "pageSize": 1000,
              "sortField": "createTime", "sortDirection": -1}
    headers = {"User-Agent": GOFILE_UA,
               "Authorization": f"Bearer {token}",
               "X-Website-Token": _gofile_wt(token),
               "X-BL": GOFILE_LANG}
    r = await _api("GET", f"https://api.gofile.io/contents/{content_id}",
                   params=params, headers=headers)
    try:
        d = r.json()
    except ValueError:
        upstream_fail("Respons API GoFile tidak valid.")
    status = d.get("status")
    if status == "error-notFound":
        upstream_fail("Konten GoFile tidak ditemukan (ID salah/kedaluwarsa).")
    if status == "error-notPremium":
        upstream_fail("GoFile membatasi endpoint ini untuk akun premium "
                      "(token situs ditolak).")
    if status != "ok":
        upstream_fail(f"API GoFile menjawab: {status}.")
    data = d.get("data") or {}
    children = data.get("children") or data.get("contents") or {}
    files = []
    for _cid, f in children.items():
        if (f.get("type") or "") != "file":
            continue
        files.append({
            "name": f.get("name"),
            "size": f.get("size"),
            "mimetype": f.get("mimetype"),
            "create_time": f.get("createTime"),
            "download_url": f.get("link"),
            "server": f.get("serverSelected") or f.get("server"),
        })
    return ok({
        "content_id": content_id,
        "name": data.get("name"),
        "total_files": len(files),
        "files": files,
        "download": ({"kind": "single", "url": files[0]["download_url"]}
                     if len(files) == 1 else None),
        "auth": {
            "type": "bearer",
            "token": token,
            "usage": ("Tautan unduh GoFile butuh header "
                      "'Authorization: Bearer <token>' atau cookie accountToken "
                      "dengan nilai token ini. Token akun tamu sekali pakai, "
                      "dibuat khusus untuk request ini."),
        },
        "note": ("Daftar file dari API resmi GoFile memakai X-Website-Token "
                 "seperti klien web mereka. Unduhan file memakai tautan "
                 "langsung + token akun tamu di atas."),
    })


# ============================================================================
# pixeldrain — AKTIF: API resmi /api/file/{id}/info + /api/file/{id}
# ============================================================================
@router.get("/pixeldrain")
async def download_pixeldrain(url: str = Query(default="")):
    url = require_url(url)
    m = re.search(r"pixeldrain\.com/(?:u|l|api/file)/([A-Za-z0-9]+)", url)
    fid = m.group(1) if m else url.strip().rstrip("/").split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9]+", fid or ""):
        bad_request("ID file Pixeldrain tidak dikenali "
                    "(format: https://pixeldrain.com/u/XXXX).")

    async def _api(api_url: str):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                         trust_env=False) as client:
                return await client.get(
                    api_url,
                    headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]})
        except httpx.HTTPError:
            upstream_fail("Gagal menghubungi API Pixeldrain.")

    r = await _api(f"https://pixeldrain.com/api/file/{fid}/info")
    if r.status_code == 404:
        upstream_fail("File Pixeldrain tidak ditemukan (ID salah/kedaluwarsa).")
    if r.status_code != 200:
        upstream_fail(f"API Pixeldrain menjawab HTTP {r.status_code}.")
    try:
        info = r.json()
    except ValueError:
        upstream_fail("Respons API Pixeldrain tidak valid.")
    if not info.get("success", True):
        upstream_fail(f"Pixeldrain: {info.get('message') or info.get('value')}.")
    return ok({
        "id": info.get("id"),
        "name": info.get("name"),
        "size": info.get("size"),
        "mime_type": info.get("mime_type"),
        "views": info.get("views"),
        "downloads": info.get("downloads"),
        "date_upload": info.get("date_upload"),
        "webpage_url": f"https://pixeldrain.com/u/{fid}",
        "download": {"kind": "single",
                     "url": f"https://pixeldrain.com/api/file/{fid}"},
        "note": "Metadata dari API resmi Pixeldrain; tautan /api/file/{id} "
                "mengunduh langsung tanpa login.",
    })


# ============================================================================
# krakenfiles — TERBATAS: metadata via /json + stream preview;
# unduhan langsung butuh captcha Turnstile (terverifikasi: "captcha not valid")
# ============================================================================
@router.get("/krakenfiles")
async def download_krakenfiles(url: str = Query(default="")):
    url = require_url(url)
    m = re.search(r"krakenfiles\.com/(?:view/([A-Za-z0-9]+)|json/([A-Za-z0-9]+))", url)
    fhash = (m.group(1) or m.group(2)) if m else url.strip().rstrip("/").split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9]+", fhash or ""):
        bad_request("Hash file KrakenFiles tidak dikenali "
                    "(format: https://krakenfiles.com/view/XXXX/file.html).")

    async def _get(u: str, nav: bool = False):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                         trust_env=False) as client:
                return await client.get(u, headers=NAV_HEADERS if nav
                                        else {"User-Agent": DEFAULT_HEADERS["User-Agent"]})
        except httpx.HTTPError:
            upstream_fail("Gagal menghubungi KrakenFiles.")

    r = await _get(f"https://krakenfiles.com/json/{fhash}")
    if r.status_code == 404:
        upstream_fail("File KrakenFiles tidak ditemukan (hash salah/kedaluwarsa).")
    try:
        meta = r.json()
    except ValueError:
        upstream_fail("Respons KrakenFiles tidak valid.")
    # URL stream preview dari elemen video halaman (bila ada).
    preview = None
    page = await _get(f"https://krakenfiles.com/view/{fhash}/file.html", nav=True)
    if page is not None and page.status_code == 200:
        vm = re.search(r'src="(https://hs\d+\.krakencloud\.net/play/video/[^"]+)"', page.text)
        if vm:
            preview = vm.group(1)
    return ok({
        "hash": fhash,
        "title": meta.get("title"),
        "size": meta.get("size"),
        "type": meta.get("type"),
        "upload_date": meta.get("uploadDate"),
        "views": meta.get("views"),
        "downloads": meta.get("downloads"),
        "thumbnail": meta.get("thumbUrl"),
        "webpage_url": f"https://krakenfiles.com/view/{fhash}/file.html",
        "preview_url": preview,
        "download": None,
        "limited": True,
        "note": ("TERBATAS: metadata + URL stream preview (bila file video) "
                 "berhasil diambil. Tombol unduh langsung KrakenFiles diproteksi "
                 "captcha Turnstile — alur form server-side dibalas "
                 "'captcha not valid', jadi unduhan file hanya bisa lewat "
                 "browser."),
    })


# ============================================================================
# uploadhaven — TERBATAS: metadata terparse; unduhan gratis butuh timer JS
# ============================================================================
@router.get("/uploadhaven")
async def download_uploadhaven(url: str = Query(default="")):
    url = require_url(url)
    if "uploadhaven.com/download/" not in url:
        bad_request("URL bukan tautan unduh UploadHaven "
                    "(format: https://uploadhaven.com/download/XXXX).")
    html, final_url = await fetch_text(url, headers=NAV_HEADERS)
    fname = None
    for nm in re.finditer(r'uh-dl-fname[^>]*>\s*([^<>]+?)\s*<', html):
        cand = ihtml.unescape(nm.group(1)).strip()
        if cand:
            fname = cand
            break
    size = re.search(r'(\d+(?:\.\d+)?\s*(?:MB|GB|KB))', html)
    if not fname:
        upstream_fail("File UploadHaven tidak ditemukan (tautan salah/kedaluwarsa).")
    return ok({
        "filename": fname,
        "size": size.group(1) if size else None,
        "webpage_url": final_url,
        "download": None,
        "limited": True,
        "note": ("TERBATAS: nama + ukuran file terparse dari halaman resmi. "
                 "Unduhan gratis UploadHaven memakai form + timer JavaScript "
                 "yang terikat sesi browser — tidak bisa diselesaikan "
                 "server-side. Buka webpage_url di browser untuk mengunduh."),
    })


# ============================================================================
# terabox — TERBATAS: parse window.yunData bila ada; tanpa cookie biasanya
# kosong -> hook env TERABOX_COOKIES (pola cookie hook grup lain)
# ============================================================================
TERABOX_COOKIE_NOTE = (
    "TeraBox tidak mengekspos data share ke klien tanpa sesi. Sediakan "
    "cookie sesi TeraBox via env TERABOX_COOKIES=\"NDUSS=...\" lalu restart "
    "service."
)


@router.get("/terabox")
async def download_terabox(url: str = Query(default="")):
    url = require_url(url)
    m = re.search(r"(?:terabox\.com|teraboxapp\.com|1024terabox\.com|nephobox\.com|4funbox\.com)/s/([A-Za-z0-9_-]+)", url)
    if not m:
        bad_request("URL bukan tautan share TeraBox (format: https://terabox.com/s/XXXX).")
    surl = m.group(1)

    async def _get(u: str):
        cookies = os.environ.get("TERABOX_COOKIES")
        hdrs = dict(NAV_HEADERS)
        if cookies:
            hdrs["Cookie"] = cookies
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                         trust_env=False) as client:
                return await client.get(u, headers=hdrs)
        except httpx.HTTPError:
            upstream_fail("Gagal menghubungi TeraBox.")

    r = await _get(f"https://www.terabox.com/s/{surl}")
    html = r.text if r is not None else ""
    files = []
    ym = re.search(r"window\.yunData\s*=\s*(\{.*?\});", html, re.S)
    if ym:
        try:
            yun = json.loads(ym.group(1))
            flist = yun.get("file_list") or []
            for f in flist:
                files.append({
                    "name": f.get("server_filename"),
                    "size": f.get("size"),
                    "isdir": bool(f.get("isdir")),
                    "fs_id": f.get("fs_id"),
                })
        except (ValueError, AttributeError):
            pass
    if not files:
        upstream_fail("TERBATAS: " + TERABOX_COOKIE_NOTE)
    return ok({
        "share_id": surl,
        "total_files": len(files),
        "files": files,
        "webpage_url": f"https://www.terabox.com/s/{surl}",
        "limited": True,
        "note": ("Daftar file dari data tertanam halaman share resmi TeraBox. "
                 "Tautan unduh langsung TeraBox butuh jsToken sesi — unduh "
                 "file besar tetap lewat browser/aplikasi."),
    })


# mega endpoint
def _mega_info_sync(url):
    from mega import Mega
    mega = Mega()
    mega.login()
    return mega.get_public_url_info(url)


@router.get("/mega")
async def download_mega(url: str = Query(default="")):
    url = require_url(url)
    if "mega." "nz" not in url and "mega." "co.nz" not in url:
        bad_request("URL bukan tautan Mega.")
    if "#" not in url and "!" not in url:
        bad_request("Tautan Mega tidak memuat kunci dekripsi (#...).")
    try:
        info = await asyncio.to_thread(_mega_info_sync, url)
    except Exception as e:  # noqa: BLE001
        upstream_fail(f"Gagal membaca tautan Mega: {type(e).__name__} "
                      f"{str(e)[:140]}")
    name = info.get("name") if isinstance(info, dict) else None
    size = info.get("size") if isinstance(info, dict) else None
    if not name:
        upstream_fail("Tautan Mega tidak valid/kedaluwarsa atau butuh kunci dekripsi.")
    return ok({
        "name": name,
        "size": size,
        "webpage_url": url,
        "download": None,
        "limited": True,
        "note": ("Metadata dari API Mega via library mega.py (login anonim). "
                 "File Mega terenkripsi ujung-ke-ujung; kunci ada di fragmen URL. "
                 "Unduh lewat aplikasi/situs resmi Mega memakai tautan di atas."),
    })


# ============================================================================
# scribd — GAGAL: halaman "Client Challenge" (tantangan JS) untuk klien server
# ============================================================================
@router.get("/scribd")
async def download_scribd(url: str = Query(default="")):
    url = require_url(url)
    if "scribd.com" not in url:
        bad_request("URL bukan tautan Scribd.")
    html, _ = await fetch_text(url, headers=NAV_HEADERS)
    title = _page_title(html)
    if title and "Client Challenge" not in title:
        return ok({
            "title": title,
            "webpage_url": url,
            "text": None,
            "limited": True,
            "note": "Hanya judul yang bisa diambil; isi dokumen butuh sesi browser.",
        })
    upstream_fail("GAGAL: Scribd menyajikan halaman 'Client Challenge' "
                  "(tantangan JavaScript) ke klien server — judul maupun teks "
                  "dokumen tidak bisa diekstrak tanpa browser interaktif.")


# ============================================================================
# mcpe — TERBATAS: metadata dari meta tag resmi; rantai unduh butuh app API
# ============================================================================
@router.get("/mcpe")
async def download_mcpe(url: str = Query(default="")):
    url = require_url(url)
    if "mcpedl.com" not in url:
        bad_request("URL bukan halaman MCPEDL.")
    html, final_url = await fetch_text(url, headers=NAV_HEADERS)
    title = _meta_tag(html, "og:title") or _page_title(html)
    if title and "404" in title:
        upstream_fail("Halaman MCPEDL tidak ditemukan.")
    files = []
    for m in re.finditer(r'href="([^"]+\.(?:mcaddon|mcpack|zip))"', html, re.I):
        u = m.group(1)
        if u.startswith("/"):
            u = "https://mcpedl.com" + u
        if u not in files:
            files.append(u)
    result = {
        "title": title,
        "description": _meta_tag(html, "og:description"),
        "cover": _meta_tag(html, "og:image"),
        "webpage_url": final_url,
        "files": files,
    }
    if files:
        result["download"] = {"kind": "single", "url": files[0]}
        result["note"] = "Tautan file langsung ditemukan di halaman resmi MCPEDL."
    else:
        result["download"] = None
        result["limited"] = True
        result["note"] = ("TERBATAS: metadata (judul, deskripsi, cover) dari "
                          "halaman resmi MCPEDL. Daftar file unduhan dimuat "
                          "aplikasi web mereka via API internal — tidak "
                          "terekspos di HTML server-side. Buka webpage_url "
                          "di browser untuk mengunduh.")
    return ok(result)


# ============================================================================
# github — AKTIF: API resmi GitHub (repo info + arsip cabang default)
# ============================================================================
@router.get("/github")
async def download_github(url: str = Query(default="")):
    url = require_url(url)
    m = re.search(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", url)
    if not m:
        bad_request("URL bukan repo GitHub (format: https://github.com/owner/repo).")
    owner, repo = m.group(1), m.group(2).removesuffix(".git")

    async def _api(api_url: str):
        headers = {"User-Agent": "RuangNode-API",
                   "Accept": "application/vnd.github+json"}
        pat = os.environ.get("GITHUB_PAT")
        if pat:
            headers["Authorization"] = f"Bearer {pat}"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                         trust_env=False) as client:
                return await client.get(api_url, headers=headers)
        except httpx.HTTPError:
            upstream_fail("Gagal menghubungi API GitHub.")

    r = await _api(f"https://api.github.com/repos/{owner}/{repo}")
    if r.status_code == 404:
        upstream_fail("Repo GitHub tidak ditemukan (privat/nama salah).")
    if r.status_code == 403:
        upstream_fail("Rate limit API GitHub tercapai — coba lagi nanti.")
    if r.status_code != 200:
        upstream_fail(f"API GitHub menjawab HTTP {r.status_code}.")
    try:
        info = r.json()
    except ValueError:
        upstream_fail("Respons API GitHub tidak valid.")
    branch = info.get("default_branch") or "main"
    return ok({
        "full_name": info.get("full_name"),
        "description": info.get("description"),
        "owner": (info.get("owner") or {}).get("login"),
        "stars": info.get("stargazers_count"),
        "forks": info.get("forks_count"),
        "language": info.get("language"),
        "default_branch": branch,
        "license": (info.get("license") or {}).get("spdx_id"),
        "updated_at": info.get("updated_at"),
        "webpage_url": info.get("html_url"),
        "archives": {
            "zip": f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}",
            "tar": f"https://api.github.com/repos/{owner}/{repo}/tarball/{branch}",
        },
        "note": ("Info repo dari API resmi GitHub; arsip zip/tar cabang "
                 "default bisa diunduh langsung."),
    })


# ============================================================================
# applemusic — AKTIF: iTunes Lookup API resmi (metadata + preview 30 detik)
# ============================================================================
@router.get("/applemusic")
async def download_applemusic(url: str = Query(default="")):
    url = require_url(url)
    track_id = album_id = None
    if "music.apple.com" in url or "itunes.apple.com" in url:
        m = re.search(r"/song/(?:[^/]+/)?(\d+)", url)
        if m:
            track_id = m.group(1)
        else:
            m = re.search(r"[?&]i=(\d+)", url)
            if m:
                track_id = m.group(1)
        if not track_id:
            m = re.search(r"/album/(?:[^/]+/)?(\d+)", url)
            if m:
                album_id = m.group(1)
    if not track_id and not album_id:
        bad_request("ID lagu/album Apple Music tidak ditemukan di URL "
                    "(format: https://music.apple.com/xx/song/.../123456).")

    async def _lookup(params: dict):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                         trust_env=False) as client:
                return await client.get("https://itunes.apple.com/lookup",
                                        params=params,
                                        headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]})
        except httpx.HTTPError:
            upstream_fail("Gagal menghubungi iTunes Lookup API.")

    if track_id:
        r = await _lookup({"id": track_id, "entity": "song"})
    else:
        r = await _lookup({"id": album_id, "entity": "album"})
    if r.status_code != 200:
        upstream_fail(f"iTunes Lookup API menjawab HTTP {r.status_code}.")
    try:
        d = r.json()
    except ValueError:
        upstream_fail("Respons iTunes Lookup API tidak valid.")
    results = d.get("results") or []
    item = next((x for x in results
                 if x.get("wrapperType") == ("track" if track_id else "collection")),
                results[0] if results else None)
    if not item:
        upstream_fail("Lagu/album tidak ditemukan di katalog Apple Music.")
    return ok({
        "type": "song" if track_id else "album",
        "track": item.get("trackName"),
        "collection": item.get("collectionName"),
        "artist": item.get("artistName"),
        "genre": item.get("primaryGenreName"),
        "release_date": item.get("releaseDate"),
        "duration_ms": item.get("trackTimeMillis"),
        "artwork": (item.get("artworkUrl100") or "").replace("100x100bb", "1200x1200bb"),
        "preview_url": item.get("previewUrl"),
        "apple_music_url": item.get("trackViewUrl") or item.get("collectionViewUrl"),
        "note": ("JUJUR: lagu penuh Apple Music ber-DRM (FairPlay) dan tidak "
                 "bisa diunduh oleh API mana pun. Yang tersedia dari API "
                 "resmi iTunes: metadata lengkap + preview resmi 30 detik "
                 "(preview_url)."),
    })
