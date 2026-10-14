"""Kategori Stalk — intip profil publik.

26 endpoint: githubuser, npm, bluesky, genshin, steam, telegram, youtube,
kick, twitch, threads, twitter, facebook, pinterest, discord, whatsapp,
tiktok, instagram, mlbb, codm, freefire, valorant, pubg, reddit, linkedin,
spotify, roblox.

Prinsip: API resmi platform / halaman publik resmi platform / library lokal
di server kita (yt-dlp, Pillow) / endpoint publik milik layanan top-up resmi
(Codashop, GoPay Games) untuk cek akun game. BUKAN API agregator pihak
ketiga dan BUKAN menembak API stalk milik orang lain.
"""

import asyncio
import datetime
import html as ihtml
import json
import os
import random
import re

import httpx
import yt_dlp
from fastapi import APIRouter, Depends, Query

from .. import tiktok_sign
from ..common import (
    DEFAULT_HEADERS,
    bad_request,
    fetch_text,
    ok,
    upstream_fail,
)
from ..security import require_api_token

router = APIRouter(prefix="/api/stalk", dependencies=[Depends(require_api_token)])

BROWSER_UA = DEFAULT_HEADERS["User-Agent"]

NAV_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

JSON_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "application/json"}


# ----------------------------------------------------------------------------
# Bantuan umum
# ----------------------------------------------------------------------------
def _og_meta(html_text: str) -> dict:
    """Ambil semua meta og:* (tahan terhadap urutan atribut)."""
    out = {}
    for m in re.finditer(
        r'<meta\s+[^>]*?property="og:([^"]+)"[^>]*?content="([^"]*)"', html_text
    ):
        out[m.group(1)] = ihtml.unescape(m.group(2))
    for m in re.finditer(
        r'<meta\s+[^>]*?content="([^"]*)"[^>]*?property="og:([^"]+)"', html_text
    ):
        out[m.group(2)] = ihtml.unescape(m.group(1))
    return out


def limited(result: dict, note: str) -> dict:
    """Hasil sebagian + catatan jujur (bukan error)."""
    result = dict(result)
    result["limited"] = True
    result["note"] = note
    return ok(result)


async def _get_json(url: str, headers: dict | None = None,
                    timeout: float = 30.0, **kwargs) -> tuple[int, dict | list | None]:
    h = dict(JSON_HEADERS)
    if headers:
        h.update(headers)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                     trust_env=False) as client:
            resp = await client.get(url, headers=h, **kwargs)
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, None
    except httpx.HTTPError:
        return 0, None


def _ytdl_opts(cookie_env: str | None = None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 2}
    if cookie_env:
        path = os.environ.get(cookie_env)
        if path:
            opts["cookiefile"] = path
    return opts


def _extract_sync(url: str, cookie_env: str | None = None) -> dict:
    with yt_dlp.YoutubeDL(_ytdl_opts(cookie_env)) as ydl:
        return ydl.extract_info(url, download=False, process=False)


async def _extract_or_none(url: str, cookie_env: str | None = None):
    try:
        return await asyncio.to_thread(_extract_sync, url, cookie_env), ""
    except yt_dlp.utils.DownloadError as e:
        return None, str(e)[:240]
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}."


def _require(value: str | None, name: str) -> str:
    value = (value or "").strip()
    if not value:
        bad_request(f"Parameter {name} wajib diisi.")
    return value


# ----------------------------------------------------------------------------
# 1. githubuser — API resmi GitHub (+ PAT bila ada)
# ----------------------------------------------------------------------------
def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "RuangNode-API"}
    pat = os.environ.get("GITHUB_PAT")
    if pat:
        h["Authorization"] = f"Bearer {pat}"
    return h


@router.get("/githubuser")
async def stalk_githubuser(username: str = Query(default="")):
    username = _require(username, "username")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", username):
        bad_request("Format username GitHub tidak valid.")
    code, data = await _get_json(f"https://api.github.com/users/{username}",
                                 headers=_gh_headers())
    if code == 404 or (isinstance(data, dict) and data.get("message") == "Not Found"):
        upstream_fail("Pengguna GitHub tidak ditemukan.")
    if code != 200 or not isinstance(data, dict):
        upstream_fail(f"GitHub API menolak request (HTTP {code or 'timeout'}).")
    code2, repos = await _get_json(
        f"https://api.github.com/users/{username}/repos?per_page=100&sort=pushed",
        headers=_gh_headers())
    top = []
    if code2 == 200 and isinstance(repos, list):
        for r in sorted(repos, key=lambda x: x.get("stargazers_count", 0),
                        reverse=True)[:5]:
            top.append({
                "name": r.get("full_name"), "stars": r.get("stargazers_count"),
                "language": r.get("language"), "url": r.get("html_url"),
            })
    return ok({
        "login": data.get("login"), "name": data.get("name"),
        "avatar": data.get("avatar_url"), "bio": data.get("bio"),
        "company": data.get("company"), "location": data.get("location"),
        "blog": data.get("blog"), "followers": data.get("followers"),
        "following": data.get("following"), "public_repos": data.get("public_repos"),
        "created_at": data.get("created_at"), "profile": data.get("html_url"),
        "top_repos": top,
    })


# ----------------------------------------------------------------------------
# 2. npm — registry resmi npmjs
# ----------------------------------------------------------------------------
@router.get("/npm")
async def stalk_npm(package: str = Query(default="")):
    package = _require(package, "package").lower()
    if not re.fullmatch(r"(@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*", package):
        bad_request("Format nama paket npm tidak valid.")
    code, data = await _get_json(f"https://registry.npmjs.org/{package}/latest")
    if code == 404:
        upstream_fail("Paket npm tidak ditemukan.")
    if code != 200 or not isinstance(data, dict):
        upstream_fail("Registry npm menolak request.")
    author = data.get("author")
    return ok({
        "name": data.get("name"), "version": data.get("version"),
        "description": data.get("description"),
        "homepage": data.get("homepage"), "license": data.get("license"),
        "author": author.get("name") if isinstance(author, dict) else author,
        "repository": (data.get("repository") or {}).get("url") if isinstance(data.get("repository"), dict) else None,
        "maintainers": len(data.get("maintainers") or []),
        "npm_url": f"https://www.npmjs.com/package/{data.get('name')}",
    })


# ----------------------------------------------------------------------------
# 3. bluesky — AT Protocol publik
# ----------------------------------------------------------------------------
@router.get("/bluesky")
async def stalk_bluesky(user: str = Query(default="")):
    user = _require(user, "user").lstrip("@")
    code, data = await _get_json(
        "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile",
        params={"actor": user})
    if code == 400 or (isinstance(data, dict) and data.get("error") == "InvalidRequest"):
        upstream_fail("Akun Bluesky tidak ditemukan.")
    if code != 200 or not isinstance(data, dict):
        upstream_fail("API Bluesky menolak request.")
    return ok({
        "handle": data.get("handle"), "display_name": data.get("displayName"),
        "description": data.get("description"), "avatar": data.get("avatar"),
        "banner": data.get("banner"), "followers": data.get("followersCount"),
        "following": data.get("followsCount"), "posts": data.get("postsCount"),
        "joined": data.get("createdAt"), "profile": f"https://bsky.app/profile/{data.get('handle')}",
    })


# ----------------------------------------------------------------------------
# 4. genshin — Enka.network (API publik)
# ----------------------------------------------------------------------------
@router.get("/genshin")
async def stalk_genshin(uid: str = Query(default="")):
    uid = _require(uid, "uid")
    if not re.fullmatch(r"\d{9}", uid):
        bad_request("UID Genshin terdiri dari 9 digit angka.")
    code, data = await _get_json(f"https://enka.network/api/uid/{uid}")
    if code == 404:
        upstream_fail("UID tidak ditemukan atau profil disembunyikan pemain.")
    if code != 200 or not isinstance(data, dict):
        upstream_fail("Enka.network menolak request.")
    pi = data.get("playerInfo") or {}
    chars = []
    for a in data.get("avatarInfoList") or []:
        chars.append({"avatar_id": a.get("avatarId"), "level": a.get("level")})
    return ok({
        "uid": uid, "nickname": pi.get("nickname"), "level": pi.get("level"),
        "signature": pi.get("signature"), "world_level": pi.get("worldLevel"),
        "achievements": pi.get("finishAchievementNum"),
        "abyss_floor": (pi.get("towerFloorIndex"), pi.get("towerLevelIndex")),
        "showcase_count": len(chars), "showcase": chars,
    })


# ----------------------------------------------------------------------------
# 5. steam — halaman profil publik (varian XML resmi)
# ----------------------------------------------------------------------------
@router.get("/steam")
async def stalk_steam(username: str = Query(default="")):
    username = _require(username, "username").strip().rstrip("/")
    if re.fullmatch(r"\d{17}", username):
        url = f"https://steamcommunity.com/profiles/{username}/?xml=1"
    else:
        url = f"https://steamcommunity.com/id/{username}/?xml=1"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                     trust_env=False) as client:
            resp = await client.get(url, headers={"User-Agent": BROWSER_UA})
    except httpx.HTTPError:
        upstream_fail("Gagal menghubungi Steam.")
    if resp.status_code == 404 or "<error>" in resp.text[:500]:
        upstream_fail("Profil Steam tidak ditemukan.")
    if resp.status_code != 200:
        upstream_fail(f"Steam menolak request (HTTP {resp.status_code}).")
    t = resp.text
    def tag(name: str) -> str | None:
        m = re.search(rf"<{name}>(.*?)</{name}>", t, re.S)
        if not m:
            return None
        v = m.group(1).strip()
        cm = re.match(r"<!\[CDATA\[(.*)\]\]>$", v, re.S)
        return ihtml.unescape(cm.group(1) if cm else v)
    if not tag("steamID64"):
        upstream_fail("Profil Steam tidak ditemukan atau privat.")
    return ok({
        "steam_id64": tag("steamID64"), "name": tag("steamID"),
        "avatar": tag("avatarFull"), "state": tag("onlineState"),
        "visibility": tag("visibilityState"),
        "member_since": tag("memberSince"),
        "profile": f"https://steamcommunity.com/profiles/{tag('steamID64')}/",
    })


# ----------------------------------------------------------------------------
# 6. telegram — halaman publik t.me (meta resmi)
# ----------------------------------------------------------------------------
@router.get("/telegram")
async def stalk_telegram(username: str = Query(default="")):
    username = _require(username, "username").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        bad_request("Format username Telegram tidak valid.")
    html_text, final = await fetch_text(f"https://t.me/{username}", headers=NAV_HEADERS)
    og = _og_meta(html_text)
    if not og.get("title"):
        upstream_fail("Username Telegram tidak ditemukan atau privat.")
    return ok({
        "username": username, "title": og.get("title"),
        "description": og.get("description"), "photo": og.get("image"),
        "url": final,
    })


# ----------------------------------------------------------------------------
# 7. youtube — yt-dlp (library lokal, pola grup media_dl)
# ----------------------------------------------------------------------------
@router.get("/youtube")
async def stalk_youtube(username: str = Query(default="")):
    username = _require(username, "username")
    if re.match(r"^https?://", username):
        url = username
    elif username.startswith("@"):
        url = f"https://www.youtube.com/{username}"
    elif re.fullmatch(r"[A-Za-z0-9_-]{24}", username):
        url = f"https://www.youtube.com/channel/{username}"
    else:
        url = f"https://www.youtube.com/@{username}"
    info, err = await _extract_or_none(url)
    if not info:
        upstream_fail("Channel YouTube tidak ditemukan atau diblokir.")
    return ok({
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id") or info.get("id"),
        "description": (info.get("description") or "")[:500],
        "thumbnail": info.get("thumbnail"),
        "subscribers": info.get("subscriber_count") or info.get("channel_follower_count"),
        "url": info.get("channel_url") or url,
    })


# ----------------------------------------------------------------------------
# 8. kick — API publik kick.com
# ----------------------------------------------------------------------------
@router.get("/kick")
async def stalk_kick(username: str = Query(default="")):
    username = _require(username, "username").lower()
    code, data = await _get_json(f"https://kick.com/api/v2/channels/{username}")
    if code == 404 or (isinstance(data, dict) and data.get("error")):
        upstream_fail("Channel Kick tidak ditemukan.")
    if code != 200 or not isinstance(data, dict):
        upstream_fail("API Kick menolak request.")
    user = data.get("user") or {}
    live = data.get("livestream")
    return ok({
        "username": user.get("username"), "bio": data.get("user_bio"),
        "avatar": user.get("profile_pic"), "banner": data.get("banner_image"),
        "followers": data.get("followers_count"),
        "created_at": user.get("created_at"),
        "is_live": bool(live),
        "live_title": (live or {}).get("session_title"),
        "live_viewers": (live or {}).get("viewer_count"),
        "profile": f"https://kick.com/{username}",
    })


# ----------------------------------------------------------------------------
# 9-12. twitch / threads / twitter / facebook — meta publik halaman resmi
# ----------------------------------------------------------------------------
async def _meta_profile(platform: str, url: str) -> dict:
    html_text, final = await fetch_text(url, headers=NAV_HEADERS)
    og = _og_meta(html_text)
    if not og.get("title"):
        upstream_fail(f"Profil {platform} tidak ditemukan atau tidak bisa dibaca dari server.")
    return {
        "title": og.get("title"), "description": og.get("description"),
        "image": og.get("image"), "url": final,
    }


@router.get("/twitch")
async def stalk_twitch(username: str = Query(default="")):
    username = _require(username, "username").lstrip("@")
    return ok(await _meta_profile("Twitch", f"https://www.twitch.tv/{username}"))


@router.get("/threads")
async def stalk_threads(user: str = Query(default="")):
    user = _require(user, "user").lstrip("@")
    return ok(await _meta_profile("Threads", f"https://www.threads.com/@{user}?hl=en"))


@router.get("/twitter")
async def stalk_twitter(username: str = Query(default="")):
    username = _require(username, "username").lstrip("@")
    try:
        return ok(await _meta_profile("X (Twitter)", f"https://x.com/{username}"))
    except Exception:  # noqa: BLE001
        # X sering menolak IP datacenter (403) meski kadang lolos.
        html_text, final = await fetch_text(f"https://x.com/{username}",
                                            headers=NAV_HEADERS)
        og = _og_meta(html_text)
        if og.get("title"):
            return ok({"title": og.get("title"), "description": og.get("description"),
                       "image": og.get("image"), "url": final})
        return limited(
            {"username": username},
            "X (Twitter) membatasi akses profil dari IP server; coba lagi nanti "
            "atau isi cookie sesi bila tersedia.")


@router.get("/facebook")
async def stalk_facebook(username: str = Query(default="")):
    username = _require(username, "username").lstrip("@")
    return ok(await _meta_profile("Facebook", f"https://www.facebook.com/{username}"))


# ----------------------------------------------------------------------------
# 13. pinterest — data tertanam halaman profil publik
# ----------------------------------------------------------------------------
@router.get("/pinterest")
async def stalk_pinterest(username: str = Query(default="")):
    username = _require(username, "username").lstrip("@")
    html_text, final = await fetch_text(f"https://www.pinterest.com/{username}/",
                                        headers=NAV_HEADERS)
    if '"username":"' + username + '"' not in html_text.replace(" ", ""):
        # toleran: cek longgar
        if username.lower() not in html_text.lower():
            upstream_fail("Profil Pinterest tidak ditemukan.")
    t = html_text
    ux = re.escape(username)
    name = (re.search(r'"full_name":"([^"]{1,80})","username":"' + ux + r'"', t)
            or re.search(r'"username":"' + ux + r'","full_name":"([^"]{1,80})"', t))
    if not name:
        for m in re.finditer(r'"username":"' + ux + r'"', t):
            win = t[max(0, m.start() - 500):m.start()]
            vm = re.search(r'"verified_identity":\{"name":"([^"]{1,80})"', win)
            if vm:
                name = vm
                break
    followers = re.search(r'"username":"' + ux + r'"[^}]{0,600}?"follower_count":(\d+)', t)
    avatar = None
    for m in re.finditer(r'"username":"' + ux + r'"', t):
        win = t[max(0, m.start() - 600):m.start() + 600]
        im = re.search(r'"image_(?:large|medium|small|original)_url":"([^"]+)"', win)
        if im:
            avatar = im.group(1).replace("\\u0026", "&")
            break
    if not name and not followers:
        upstream_fail("Data profil Pinterest tidak bisa dibaca dari server.")
    return ok({
        "username": username,
        "name": ihtml.unescape(name.group(1)) if name else None,
        "followers": int(followers.group(1)) if followers else None,
        "avatar": avatar,
        "url": final,
    })


# ----------------------------------------------------------------------------
# 14. discord — decode snowflake (data publik murni dari ID)
# ----------------------------------------------------------------------------
@router.get("/discord")
async def stalk_discord(id: str = Query(default="")):
    id = _require(id, "id")
    if not re.fullmatch(r"\d{17,20}", id):
        bad_request("Discord user ID terdiri dari 17-20 digit angka.")
    created_ms = (int(id) >> 22) + 1420070400000
    created = datetime.datetime.fromtimestamp(created_ms / 1000,
                                              tz=datetime.timezone.utc)
    return limited({
        "user_id": id,
        "account_created": created.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }, "Hanya tanggal pembuatan akun yang bisa dihitung dari ID secara publik; "
       "nama dan foto profil membutuhkan bot Discord yang berteman dengan akun tersebut.")


# ----------------------------------------------------------------------------
# 15. whatsapp — normalisasi + tautan wa.me
# ----------------------------------------------------------------------------
@router.get("/whatsapp")
async def stalk_whatsapp(phone: str = Query(default="")):
    phone = _require(phone, "phone")
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    if not re.fullmatch(r"\d{8,16}", digits):
        bad_request("Nomor telepon tidak valid.")
    return limited({
        "phone_international": "+" + digits,
        "wa_me": f"https://wa.me/{digits}",
    }, "Tidak ada API publik WhatsApp untuk melihat profil nomor tanpa sesi WhatsApp; "
       "foto dan nama profil hanya bisa dibaca lewat akun WhatsApp yang sudah login.")


# ----------------------------------------------------------------------------
# 16. tiktok — API web internal bertanda tangan (modul tiktok_sign.py)
# ----------------------------------------------------------------------------
def _tiktok_signed_pairs(unique_id: str) -> list:
    return [
        ("aid", "1988"), ("app_language", "en"), ("app_name", "tiktok_web"),
        ("browser_language", "en-US"), ("browser_name", "Mozilla"),
        ("browser_online", "true"), ("browser_platform", "Win32"),
        ("browser_version", "5.0 (Windows)"), ("channel", "tiktok_web"),
        ("cookie_enabled", "true"),
        ("device_id", str(random.randint(10**18, 10**19 - 1))),
        ("device_platform", "web_pc"), ("focus_state", "true"),
        ("from_page", "user"), ("history_len", "4"), ("is_fullscreen", "false"),
        ("is_page_visible", "true"), ("language", "en"), ("os", "windows"),
        ("priority_region", "US"), ("referer", ""), ("region", "US"),
        ("root_referer", "https://www.tiktok.com/"),
        ("screen_height", "1080"), ("screen_width", "1920"),
        ("tz_name", "America/Los_Angeles"), ("webcast_language", "en"),
        ("uniqueId", unique_id),
    ]


async def _tiktok_user_signed(username: str) -> tuple[dict | None, dict]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                     trust_env=False) as client:
            try:
                await client.get("https://www.tiktok.com/", headers=NAV_HEADERS)
            except httpx.HTTPError:
                pass
            cookie_header = "; ".join(f"{ck.name}={ck.value}"
                                     for ck in client.cookies.jar)
            m = re.search(r"msToken=([^;]+)", cookie_header)
            ms_token = m.group(1) if m else ""
            signed_query, _ = tiktok_sign.sign(
                _tiktok_signed_pairs(username), BROWSER_UA, ms_token=ms_token)
            headers = {**NAV_HEADERS, "Accept": "application/json, text/plain, */*",
                       "Referer": "https://www.tiktok.com/"}
            if cookie_header:
                headers["Cookie"] = cookie_header
            resp = await client.get(
                "https://www.tiktok.com/api/user/detail/?" + signed_query,
                headers=headers)
            if resp.status_code != 200 or not resp.content:
                return None
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None, {}
    if data.get("statusCode") not in (0, None):
        return None, {}
    info = data.get("userInfo") or {}
    return info.get("user"), info.get("stats") or {}


@router.get("/tiktok")
async def stalk_tiktok(username: str = Query(default="")):
    username = _require(username, "username").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.]{2,24}", username):
        bad_request("Format username TikTok tidak valid.")
    user, stats = await _tiktok_user_signed(username)
    if not user or not user.get("uniqueId"):
        return limited({"username": username},
                       "Data profil TikTok dibatasi dari IP server (halaman profil "
                       "menyajikan shell kosong, API butuh token sesi browser).")
    return ok({
        "username": user.get("uniqueId"), "nickname": user.get("nickname"),
        "verified": user.get("verified"), "private": user.get("privateAccount"),
        "signature": user.get("signature"),
        "avatar": user.get("avatarLarger") or user.get("avatarMedium"),
        "stats": {k: stats.get(k) for k in
                  ("followerCount", "followingCount", "heartCount", "videoCount", "diggCount")},
        "profile": f"https://www.tiktok.com/@{user.get('uniqueId')}",
    })


# ----------------------------------------------------------------------------
# 17. instagram — yt-dlp + hook cookie (pola social_dl)
# ----------------------------------------------------------------------------
@router.get("/instagram")
async def stalk_instagram(username: str = Query(default="")):
    username = _require(username, "username").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        bad_request("Format username Instagram tidak valid.")
    info, err = await _extract_or_none(f"https://www.instagram.com/{username}/",
                                       cookie_env="IG_COOKIES")
    if not info:
        return limited(
            {"username": username},
            "Instagram membatasi akses profil dari IP server (HTTP 429); "
            "isi IG_COOKIES dengan cookie browser agar endpoint ini aktif penuh.")
    return ok({
        "username": info.get("uploader_id") or username,
        "name": info.get("uploader"),
        "bio": (info.get("description") or "")[:500],
        "avatar": info.get("thumbnail"),
        "followers": info.get("follower_count"),
        "following": info.get("followee_count"),
        "posts": info.get("playlist_count"),
        "url": f"https://www.instagram.com/{username}/",
    })


# ----------------------------------------------------------------------------
# Cek akun game — via endpoint publik layanan top-up resmi
# (teknik didokumentasikan repo OSS triyatna/php-valid-game, MIT)
# ----------------------------------------------------------------------------
CODASHOP_URL = "https://order-sg.codashop.com/initPayment.action"
GOPAY_URL = "https://gopay.co.id/games/v1/order/user-account"

_CODASHOP_GAMES = {
    "mlbb": {"id": "5199", "price": "68543.0000",
             "voucherTypeName": "MOBILE_LEGENDS", "zone": True},
    "codm": {"id": "270251", "price": "20000.0000",
             "voucherTypeName": "CALL_OF_DUTY", "zone": False},
    "freefire": {"id": "270288", "price": "200000.0000",
                 "voucherTypeName": "FREEFIRE", "zone": False},
    "valorant": {"id": "950525", "price": "75000.0000",
                 "voucherTypeName": "VALORANT", "zone": "0"},
}


async def _codashop_check(game_key: str, user_id: str, zone_id: str | None,
                          riot_id: str | None = None) -> dict:
    cfg = _CODASHOP_GAMES[game_key]
    zone = cfg["zone"]
    payload = {
        "voucherPricePoint.id": cfg["id"],
        "voucherPricePoint.price": cfg["price"],
        "voucherPricePoint.variablePrice": "0",
        "user.userId": riot_id or user_id,
        "user.zoneId": (zone_id if zone is True else (zone if zone else "")) or "",
        "voucherTypeName": cfg["voucherTypeName"],
        "shopLang": "id_ID",
    }
    if game_key == "valorant":
        payload["userVariablePrice"] = "0"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                     trust_env=False) as client:
            resp = await client.post(CODASHOP_URL, data=payload,
                                     headers={"User-Agent": BROWSER_UA,
                                              "Accept": "application/json"})
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        upstream_fail("Layanan verifikasi akun tidak merespons.")
    if data.get("errorCode") == "":
        fields = data.get("confirmationFields") or {}
        nick = fields.get("username")
        if not nick and isinstance(fields.get("roles"), list) and fields["roles"]:
            nick = fields["roles"][0].get("role")
        return {"valid": True, "nickname": nick}
    return {"valid": False, "message": str(data.get("errorMsg") or "Akun tidak ditemukan.").strip()}


async def _gopay_check(game_code: str, user_id: str) -> dict:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                     trust_env=False) as client:
            resp = await client.post(
                GOPAY_URL,
                json={"code": game_code,
                      "data": {"userId": str(user_id), "zoneId": ""}},
                headers={"User-Agent": BROWSER_UA, "Content-Type": "application/json"})
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        upstream_fail("Layanan verifikasi akun tidak merespons.")
    if resp.status_code == 200 and data.get("success") is True:
        return {"valid": True,
                "nickname": (data.get("data") or {}).get("userAccount")}
    msg = str(data.get("message") or "Akun tidak ditemukan.").strip()
    return {"valid": False, "message": msg}


# 18. mlbb
@router.get("/mlbb")
async def stalk_mlbb(user_id: str = Query(default=""),
                     zone_id: str = Query(default="")):
    user_id = _require(user_id, "user_id")
    zone_id = _require(zone_id, "zone_id")
    if not re.fullmatch(r"\d{5,12}", user_id) or not re.fullmatch(r"\d{3,6}", zone_id):
        bad_request("user_id dan zone_id harus berupa angka (contoh: user_id=12345678&zone_id=1234).")
    res = await _codashop_check("mlbb", user_id, zone_id)
    return ok({"game": "Mobile Legends", "user_id": user_id, "zone_id": zone_id, **res})


# 19. codm
@router.get("/codm")
async def stalk_codm(user: str = Query(default="")):
    user = _require(user, "user")
    if not re.fullmatch(r"\d{5,15}", user):
        bad_request("UID CODM harus berupa angka.")
    res = await _codashop_check("codm", user, None)
    return ok({"game": "Call of Duty Mobile", "user_id": user, **res})


# 20. freefire
@router.get("/freefire")
async def stalk_freefire(uid: str = Query(default="")):
    uid = _require(uid, "uid")
    if not re.fullmatch(r"\d{5,15}", uid):
        bad_request("UID Free Fire harus berupa angka.")
    res = await _codashop_check("freefire", uid, None)
    return ok({"game": "Free Fire", "uid": uid, **res})


# 21. valorant
@router.get("/valorant")
async def stalk_valorant(name: str = Query(default=""),
                         tag: str = Query(default="")):
    name = _require(name, "name")
    tag = _require(tag, "tag")
    if not re.fullmatch(r"[A-Za-z0-9 ]{3,16}", name) or not re.fullmatch(r"[A-Za-z0-9]{3,5}", tag):
        bad_request("Format Riot ID tidak valid (contoh: name=Pemain&tag=1234).")
    res = await _codashop_check("valorant", name, None, riot_id=f"{name}#{tag}")
    return ok({"game": "Valorant", "riot_id": f"{name}#{tag}", **res})


# 22. pubg
@router.get("/pubg")
async def stalk_pubg(user: str = Query(default="")):
    user = _require(user, "user")
    if not re.fullmatch(r"\d{5,12}", user):
        bad_request("ID pemain PUBG harus berupa angka.")
    res = await _gopay_check("PUBG_ID", user)
    return ok({"game": "PUBG Mobile", "user_id": user, **res})


# ----------------------------------------------------------------------------
# 23-26. reddit / linkedin / spotify / roblox
# ----------------------------------------------------------------------------
@router.get("/reddit")
async def stalk_reddit(username: str = Query(default="")):
    username = _require(username, "username").lstrip("u/")
    code, data = await _get_json(f"https://www.reddit.com/user/{username}/about.json")
    if code == 404:
        upstream_fail("Akun Reddit tidak ditemukan.")
    if code != 200 or not isinstance(data, dict) or "data" not in data:
        upstream_fail("Reddit menolak request dari IP server (HTTP 403).")
    d = data["data"]
    return ok({
        "username": d.get("name"), "karma": d.get("total_karma"),
        "created": datetime.datetime.fromtimestamp(
            d.get("created_utc", 0), tz=datetime.timezone.utc).strftime("%Y-%m-%d"),
        "is_employee": d.get("is_employee"),
        "avatar": (d.get("icon_img") or "").split("?")[0] or None,
        "profile": f"https://www.reddit.com/user/{d.get('name')}/",
    })


@router.get("/linkedin")
async def stalk_linkedin(username: str = Query(default="")):
    username = _require(username, "username")
    try:
        html_text, final = await fetch_text(
            f"https://www.linkedin.com/in/{username}", headers=NAV_HEADERS)
    except Exception:  # noqa: BLE001
        upstream_fail("LinkedIn memblokir akses dari IP server (butuh login).")
    og = _og_meta(html_text)
    if not og.get("title") or "linkedin" in (og.get("title") or "").lower() and len(og.get("title") or "") < 12:
        upstream_fail("LinkedIn memblokir akses dari IP server (butuh login).")
    return ok({
        "title": og.get("title"), "description": og.get("description"),
        "image": og.get("image"), "url": final,
    })


@router.get("/spotify")
async def stalk_spotify(username: str = Query(default="")):
    username = _require(username, "username")
    for kind in ("user", "artist"):
        try:
            html_text, final = await fetch_text(
                f"https://open.spotify.com/{kind}/{username}", headers=NAV_HEADERS)
        except Exception:  # noqa: BLE001
            continue
        og = _og_meta(html_text)
        if og.get("title"):
            return ok({
                "kind": kind, "title": og.get("title"),
                "description": og.get("description"), "image": og.get("image"),
                "url": final,
            })
    upstream_fail("Profil Spotify tidak ditemukan atau tidak bisa dibaca dari server.")


@router.get("/roblox")
async def stalk_roblox(username: str = Query(default="")):
    username = _require(username, "username")
    if re.fullmatch(r"\d{5,12}", username):
        user_id = int(username)
    else:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0,
                                         trust_env=False) as client:
                resp = await client.post(
                    "https://users.roblox.com/v1/usernames/users",
                    json={"usernames": [username], "excludeBannedUsers": False},
                    headers={"User-Agent": BROWSER_UA,
                             "Content-Type": "application/json"})
                data = resp.json()
        except httpx.HTTPError:
            upstream_fail("API Roblox tidak merespons dari IP server.")
        found = (data.get("data") or [])
        if not found:
            upstream_fail("Username Roblox tidak ditemukan.")
        user_id = found[0]["id"]
    code, data = await _get_json(f"https://users.roblox.com/v1/users/{user_id}",
                                 timeout=15.0)
    if code != 200 or not isinstance(data, dict):
        upstream_fail("API Roblox tidak merespons dari IP server.")
    code2, av = await _get_json(
        "https://thumbnails.roblox.com/v1/users/avatar-headshot",
        params={"userIds": user_id, "size": "420x420", "format": "Png",
                "isCircular": "false"}, timeout=15.0)
    avatar = None
    if code2 == 200 and isinstance(av, dict) and av.get("data"):
        avatar = av["data"][0].get("imageUrl")
    return ok({
        "user_id": data.get("id"), "username": data.get("name"),
        "display_name": data.get("displayName"),
        "description": (data.get("description") or "")[:500],
        "created": data.get("created"), "avatar": avatar,
        "profile": f"https://www.roblox.com/users/{data.get('id')}/profile",
    })
