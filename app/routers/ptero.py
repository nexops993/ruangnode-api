"""Kategori Pterodactyl — klien resmi untuk Pterodactyl Panel API.

18 endpoint: 12 Application API (admin, apikeyptero) + 6 Client API
(capikeyptero). Pemanggil memasok domain panel + kunci API panel milik
mereka sendiri; server kita meneruskan panggilan ke API RESMI Pterodactyl
di panel pemanggil dan menormalkan responsnya.

Referensi: docs resmi Pterodactyl (pterodactyl.io) + pola klien open source
(mis. gigabait93/pterodactyl-api, sudeep-alt/PteroAdmin, ho3einwave/
pterodactyl-ts). BUKAN agregator pihak ketiga.

Kontrak API resmi yang dipakai:
- Base Application: {domain}/api/application — Auth: Authorization: Bearer <ptla_...>
- Base Client:      {domain}/api/client       — Auth: Authorization: Bearer <ptlc_...>
- Header: Accept: application/vnd.pterodactyl.v1+json
- Objek: {"object": "user"|"server"|"list", "attributes": {...}, "data": [...]}
- Error: {"errors": [{"code": ..., "status": ..., "detail": ...}]}
- suspend/unsuspend/delete/power/reinstall → HTTP 204 tanpa body.
"""

import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ..common import bad_request, ok, upstream_fail
from ..security import require_api_token

router = APIRouter(prefix="/api/pterodactyl", dependencies=[Depends(require_api_token)])

ACCEPT_V1 = "application/vnd.pterodactyl.v1+json"
MAX_MASS = 100  # batas keamanan operasi massal per panggilan


# ----------------------------------------------------------------------------
# Bantuan umum
# ----------------------------------------------------------------------------
def _require(value: str | None, name: str) -> str:
    value = (value or "").strip()
    if not value:
        bad_request(f"Parameter {name} wajib diisi.")
    return value


def _norm_domain(domain: str | None) -> str:
    domain = _require(domain, "domain")
    if not re.match(r"^https?://", domain):
        bad_request("Parameter domain tidak valid (harus diawali http:// atau https://).")
    return domain.rstrip("/")


def _panel_headers(key: str) -> dict:
    # Kunci panel tidak pernah dicetak/di-log; hanya diteruskan ke panel.
    return {
        "Authorization": f"Bearer {key}",
        "Accept": ACCEPT_V1,
        "Content-Type": "application/json",
    }


async def _panel(method: str, url: str, key: str, payload: dict | None = None,
                 timeout: float = 30.0) -> tuple[int, object | None]:
    """Panggil panel; kembalikan (http_status, body_json_atau_None).

    Gagal jaringan/DNS/timeout -> 502 jujur. Status panel apa pun
    (termasuk 4xx/5xx) dikembalikan apa adanya untuk dinormalkan pemanggil.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                     trust_env=False) as client:
            if method == "GET":
                resp = await client.get(url, headers=_panel_headers(key))
            elif method == "POST":
                resp = await client.post(url, headers=_panel_headers(key), json=payload or {})
            elif method == "PATCH":
                resp = await client.patch(url, headers=_panel_headers(key), json=payload or {})
            elif method == "DELETE":
                resp = await client.delete(url, headers=_panel_headers(key))
            else:
                bad_request(f"Metode internal tidak dikenal: {method}.")
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, None
    except (httpx.ConnectError, httpx.TimeoutException):
        upstream_fail("Panel tidak terjangkau dari server (DNS/koneksi/timeout).")
    except httpx.HTTPError:
        upstream_fail("Gagal berkomunikasi dengan panel.")


def _panel_error(status: int, body: object | None) -> str:
    """Ambil pesan error panel secara aman (detail saja, tanpa dump mentah)."""
    detail = ""
    if isinstance(body, dict):
        errs = body.get("errors")
        if isinstance(errs, list) and errs:
            first = errs[0]
            if isinstance(first, dict):
                detail = str(first.get("detail") or "")
    detail = detail.strip()[:300]
    return detail


def _fail_or_ok(status: int, body: object | None, success: dict,
                action: str) -> dict:
    """Normalisasi respons panel: 2xx -> ok(), selain itu -> panel error jujur."""
    if 200 <= status < 300:
        return ok(success)
    msg = _panel_error(status, body)
    if status == 401:
        raise HTTPException(status_code=401,
                            detail=f"Panel menolak kunci API (HTTP 401). {msg}")
    if status == 404:
        raise HTTPException(status_code=404,
                            detail=f"Panel membalas HTTP 404 (tidak ditemukan). {msg}")
    raise HTTPException(status_code=status if status < 600 else 502,
                        detail=f"Panel membalas HTTP {status}: {msg or action}")


def _items(body: object | None) -> list:
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return body["data"]
    return []


def _attrs(item: dict) -> dict:
    if isinstance(item, dict) and isinstance(item.get("attributes"), dict):
        return item["attributes"]
    return {}


async def _app(method: str, domain: str, key: str, path: str,
               payload: dict | None = None, timeout: float = 30.0):
    return await _panel(method, f"{domain}/api/application{path}", key,
                        payload, timeout)


async def _client(method: str, domain: str, key: str, path: str,
                  payload: dict | None = None, timeout: float = 30.0):
    return await _panel(method, f"{domain}/api/client{path}", key,
                        payload, timeout)


# ----------------------------------------------------------------------------
# 1. statuspanel — panel terjangkau + info dasar
# ----------------------------------------------------------------------------
@router.get("/statuspanel")
async def statuspanel(domain: str = Query(default=""), apikeyptero: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    s_status, s_body = await _app("GET", domain, key, "/servers?per_page=1")
    if not (200 <= s_status < 300):
        return _fail_or_ok(s_status, s_body, {}, "Cek status panel.")
    u_status, u_body = await _app("GET", domain, key, "/users?per_page=1")

    def total(body):
        if isinstance(body, dict):
            pg = body.get("meta", {}).get("pagination", {})
            if isinstance(pg.get("total"), int):
                return pg["total"]
        return len(_items(body))

    result = {"reachable": True, "servers_total": total(s_body)}
    if 200 <= u_status < 300:
        result["users_total"] = total(u_body)
    return ok(result)


# ----------------------------------------------------------------------------
# 2. listadmin — daftar user root_admin
# ----------------------------------------------------------------------------
@router.get("/listadmin")
async def listadmin(domain: str = Query(default=""), apikeyptero: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    status, body = await _app("GET", domain, key, "/users?per_page=100")
    admins = []
    for item in _items(body):
        a = _attrs(item)
        if a.get("root_admin") is True:
            admins.append({"id": a.get("id"), "username": a.get("username"),
                           "email": a.get("email")})
    return _fail_or_ok(status, body, {"admins": admins, "count": len(admins)},
                       "Ambil daftar admin.")


# ----------------------------------------------------------------------------
# 3. createadmin — buat user root_admin (docs: POST /api/application/users)
# ----------------------------------------------------------------------------
@router.get("/createadmin")
async def createadmin(
    domain: str = Query(default=""),
    apikeyptero: str = Query(default=""),
    nameuser: str = Query(default=""),
    password: str = Query(default=""),
    email: str = Query(default=""),
):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    username = _require(nameuser, "nameuser")
    passwd = _require(password, "password")
    mail = email.strip() or f"{username}@ruangnode.local"
    payload = {
        "email": mail,
        "username": username,
        "first_name": username,
        "last_name": "Admin",
        "password": passwd,
        "root_admin": True,
        "language": "en",
    }
    status, body = await _app("POST", domain, key, "/users", payload)
    a = _attrs(body if isinstance(body, dict) else {})
    token = (body.get("meta", {}) or {}).get("token") if isinstance(body, dict) else None
    return _fail_or_ok(
        status, body,
        {"id": a.get("id"), "username": a.get("username"), "email": a.get("email"),
         "root_admin": a.get("root_admin"), "client_key": token},
        "Buat admin baru.")


# ----------------------------------------------------------------------------
# 4. setadmin — naikkan user menjadi root_admin (docs: PATCH)
# ----------------------------------------------------------------------------
@router.get("/setadmin")
async def setadmin(domain: str = Query(default=""), apikeyptero: str = Query(default=""),
                   userid: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    uid = _require(userid, "userid")
    g_status, g_body = await _app("GET", domain, key, f"/users/{uid}")
    if not (200 <= g_status < 300):
        return _fail_or_ok(g_status, g_body, {}, "Ambil data user.")
    cur = _attrs(g_body if isinstance(g_body, dict) else {})
    payload = {
        "email": cur.get("email"),
        "username": cur.get("username"),
        "first_name": cur.get("first_name") or (cur.get("username") or "User"),
        "last_name": cur.get("last_name") or "User",
        "root_admin": True,
        "language": cur.get("language") or "en",
    }
    p_status, p_body = await _app("PATCH", domain, key, f"/users/{uid}", payload)
    new = _attrs(p_body if isinstance(p_body, dict) else {})
    return _fail_or_ok(
        p_status, p_body,
        {"id": new.get("id"), "username": new.get("username"),
         "root_admin_before": cur.get("root_admin"),
         "root_admin_after": new.get("root_admin")},
        "Ubah user menjadi admin.")


# ----------------------------------------------------------------------------
# 5. deleteadmin — hapus user admin
# ----------------------------------------------------------------------------
@router.get("/deleteadmin")
async def deleteadmin(domain: str = Query(default=""), apikeyptero: str = Query(default=""),
                      userid: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    uid = _require(userid, "userid")
    status, body = await _app("DELETE", domain, key, f"/users/{uid}")
    return _fail_or_ok(status, body, {"deleted": True, "userid": uid},
                       "Hapus user.")


# ----------------------------------------------------------------------------
# 6. create — buat server (docs: POST /api/application/servers)
# ----------------------------------------------------------------------------
@router.get("/create")
async def create_server(
    domain: str = Query(default=""),
    apikeyptero: str = Query(default=""),
    nameserver: str = Query(default=""),
    disk: int = Query(default=5120),
    cpu: int = Query(default=100),
    memory: int = Query(default=1024),
):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    name = _require(nameserver, "nameserver")

    # user: akun pertama di panel (pemilik/detected)
    u_status, u_body = await _app("GET", domain, key, "/users?per_page=1")
    users = _items(u_body)
    if not (200 <= u_status < 300) or not users:
        return _fail_or_ok(u_status if not users else 422, u_body, {},
                           "Deteksi user pemilik untuk server baru.")
    user_id = _attrs(users[0]).get("id")

    # allocation: alokasi pertama yang belum terpakai di node pertama
    n_status, n_body = await _app("GET", domain, key, "/nodes?per_page=1")
    nodes = _items(n_body)
    if not (200 <= n_status < 300) or not nodes:
        return _fail_or_ok(n_status if not nodes else 422, n_body, {},
                           "Deteksi node untuk server baru.")
    node_id = _attrs(nodes[0]).get("id")
    a_status, a_body = await _app("GET", domain, key, f"/nodes/{node_id}/allocations")
    allocs = [x for x in _items(a_body) if _attrs(x).get("assigned") is False]
    if not (200 <= a_status < 300) or not allocs:
        return _fail_or_ok(a_status if not allocs else 422, a_body, {},
                           "Cari alokasi kosong untuk server baru.")
    alloc_id = _attrs(allocs[0]).get("id")

    payload = {
        "name": name,
        "user": user_id,
        # egg Nest Minecraft (Paper) yang umum di instalasi default
        "egg": 5,
        "docker_image": "ghcr.io/pterodactyl/yolks:java_21",
        "startup": "java -Xms128M -Xmx{{SERVER_MEMORY}}M -jar {{SERVER_JARFILE}}",
        "environment": {"SERVER_JARFILE": "server.jar"},
        "limits": {"memory": memory, "swap": 0, "disk": disk, "io": 500, "cpu": cpu},
        "feature_limits": {"databases": 0, "allocations": 1, "backups": 1},
        "allocation": {"default": alloc_id},
        "start_on_completion": False,
    }
    status, body = await _app("POST", domain, key, "/servers", payload, timeout=60.0)
    a = _attrs(body if isinstance(body, dict) else {})
    return _fail_or_ok(
        status, body,
        {"id": a.get("id"), "uuid": a.get("uuid"), "identifier": a.get("identifier"),
         "name": a.get("name"),
         "note": "Default: egg Paper Minecraft (id 5), docker java_21. "
                 "Bila panel menolak, sesuaikan egg/alokasi di panel."},
        "Buat server baru.")


# ----------------------------------------------------------------------------
# 7-10. hapus / suspend / unsuspend / cek server (admin)
# ----------------------------------------------------------------------------
@router.get("/deleteserver")
async def deleteserver(domain: str = Query(default=""), apikeyptero: str = Query(default=""),
                       id: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    sid = _require(id, "id")
    status, body = await _app("DELETE", domain, key, f"/servers/{sid}")
    return _fail_or_ok(status, body, {"deleted": True, "server_id": sid},
                       "Hapus server.")


@router.get("/suspend")
async def suspend(domain: str = Query(default=""), apikeyptero: str = Query(default=""),
                  id: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    sid = _require(id, "id")
    status, body = await _app("POST", domain, key, f"/servers/{sid}/suspend")
    return _fail_or_ok(status, body, {"suspended": True, "server_id": sid},
                       "Suspend server.")


@router.get("/unsuspend")
async def unsuspend(domain: str = Query(default=""), apikeyptero: str = Query(default=""),
                    id: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    sid = _require(id, "id")
    status, body = await _app("POST", domain, key, f"/servers/{sid}/unsuspend")
    return _fail_or_ok(status, body, {"suspended": False, "server_id": sid},
                       "Unsuspend server.")


@router.get("/checksuspend")
async def checksuspend(domain: str = Query(default=""), apikeyptero: str = Query(default=""),
                       id: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    sid = _require(id, "id")
    status, body = await _app("GET", domain, key, f"/servers/{sid}")
    a = _attrs(body if isinstance(body, dict) else {})
    return _fail_or_ok(status, body,
                       {"id": a.get("id"), "name": a.get("name"),
                        "suspended": bool(a.get("suspended"))},
                       "Cek status suspend server.")


# ----------------------------------------------------------------------------
# 11-12. operasi massal (dibatasi MAX_MASS per panggilan)
# ----------------------------------------------------------------------------
async def _delete_all(kind: str, domain: str, key: str) -> dict:
    if kind == "servers":
        status, body = await _app("GET", domain, key, f"/servers?per_page={MAX_MASS}", timeout=60.0)
    else:
        status, body = await _app("GET", domain, key, f"/users?per_page={MAX_MASS}", timeout=60.0)
    if not (200 <= status < 300):
        return _fail_or_ok(status, body, {}, "Ambil daftar untuk hapus massal.")
    items = _items(body)
    deleted, failed, skipped = [], [], []
    for item in items[:MAX_MASS]:
        a = _attrs(item)
        iid = a.get("id")
        if kind == "users" and a.get("root_admin") is True:
            skipped.append({"id": iid, "username": a.get("username"),
                            "reason": "root_admin dilewati"})
            continue
        d_status, d_body = await _app("DELETE", domain, key, f"/{kind}/{iid}")
        if 200 <= d_status < 300:
            deleted.append(iid)
        else:
            failed.append({"id": iid, "panel_status": d_status,
                           "panel_message": _panel_error(d_status, d_body)})
    result = {"requested": len(items), "deleted": len(deleted),
              "failed": failed, "deleted_ids": deleted}
    if skipped:
        result["skipped"] = skipped
    if len(items) > MAX_MASS:
        result["note"] = (f"Dibatasi {MAX_MASS} item per panggilan; "
                          f"sisa {len(items) - MAX_MASS} belum diproses.")
    return ok(result)


@router.get("/deleteallserver")
async def deleteallserver(domain: str = Query(default=""), apikeyptero: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    return await _delete_all("servers", domain, key)


@router.get("/deletealluser")
async def deletealluser(domain: str = Query(default=""), apikeyptero: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(apikeyptero, "apikeyptero")
    return await _delete_all("users", domain, key)


# ----------------------------------------------------------------------------
# 13-18. Client API (capikeyptero)
# ----------------------------------------------------------------------------
@router.get("/listserver")
async def listserver(domain: str = Query(default=""), capikeyptero: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(capikeyptero, "capikeyptero")
    status, body = await _client("GET", domain, key, "")
    out = []
    for item in _items(body):
        a = _attrs(item)
        out.append({"identifier": a.get("identifier"), "name": a.get("name"),
                    "description": a.get("description"),
                    "is_owner": a.get("server_owner")})
    return _fail_or_ok(status, body, {"servers": out, "count": len(out)},
                       "Ambil daftar server client.")


@router.get("/statusserver")
async def statusserver(domain: str = Query(default=""), capikeyptero: str = Query(default=""),
                       id: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(capikeyptero, "capikeyptero")
    sid = _require(id, "id")
    status, body = await _client("GET", domain, key, f"/servers/{sid}/resources")
    a = body.get("attributes", {}) if isinstance(body, dict) else {}
    return _fail_or_ok(status, body,
                       {"identifier": sid, "state": a.get("current_state"),
                        "memory_mb": round((a.get("memory_bytes") or 0) / 1048576, 1),
                        "cpu_percent": a.get("cpu_absolute"),
                        "disk_bytes": a.get("disk_bytes"),
                        "uptime_ms": a.get("uptime")},
                       "Ambil status server.")


async def _power(domain: str, key: str, sid: str, signal: str) -> dict:
    status, body = await _client("POST", domain, key, f"/servers/{sid}/power",
                                {"signal": signal})
    return _fail_or_ok(status, body,
                       {"identifier": sid, "signal": signal, "sent": True},
                       f"Kirim sinyal power {signal}.")


@router.get("/onserver")
async def onserver(domain: str = Query(default=""), capikeyptero: str = Query(default=""),
                   id: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(capikeyptero, "capikeyptero")
    return await _power(domain, key, _require(id, "id"), "start")


@router.get("/offserver")
async def offserver(domain: str = Query(default=""), capikeyptero: str = Query(default=""),
                    id: str = Query(default="")):
    # "stop" dipilih sebagai default aman (graceful), bukan "kill".
    domain = _norm_domain(domain)
    key = _require(capikeyptero, "capikeyptero")
    return await _power(domain, key, _require(id, "id"), "stop")


@router.get("/restartserver")
async def restartserver(domain: str = Query(default=""), capikeyptero: str = Query(default=""),
                        id: str = Query(default="")):
    domain = _norm_domain(domain)
    key = _require(capikeyptero, "capikeyptero")
    return await _power(domain, key, _require(id, "id"), "restart")


@router.get("/clearserver")
async def clearserver(domain: str = Query(default=""), capikeyptero: str = Query(default=""),
                      id: str = Query(default="")):
    # Makna "clear" terdekat di Client API: reinstall server.
    domain = _norm_domain(domain)
    key = _require(capikeyptero, "capikeyptero")
    sid = _require(id, "id")
    status, body = await _client("POST", domain, key, f"/servers/{sid}/settings/reinstall")
    return _fail_or_ok(status, body, {"identifier": sid, "reinstalled": True},
                       "Reinstall server.")
