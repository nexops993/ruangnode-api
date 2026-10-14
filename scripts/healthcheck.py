#!/usr/bin/env python3
"""Health-check berkala RuangNode API + mesin unduhan.

Dijalankan systemd timer tiap 15 menit sebagai root (butuh baca
/etc/ruangnode-api/env). Menulis app/static/health.json yang dibaca
dashboard — tidak pernah melempar, kegagalan dicatat sebagai ok:false.
"""

import datetime
import json
import os
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8090"
COBALT = "http://127.0.0.1:9000"
ENV_FILE = "/etc/ruangnode-api/env"
DEST = "/home/adit/ruangnode-api/app/static/health.json"
WIB = datetime.timezone(datetime.timedelta(hours=7))

# URL uji tetap (publik, kecil, stabil).
YT_SAMPLE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def load_env(path: str) -> dict:
    d = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


def http_json(url: str, headers: dict | None = None, timeout: float = 20.0,
              params: dict | None = None):
    """Kembalikan (http_status | None, body, ms). HTTP error tetap dibaca."""
    t0 = time.time()
    full = url
    if params:
        full += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(full, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e), int((time.time() - t0) * 1000)
        except Exception:  # noqa: BLE001
            return e.code, e.read(200), int((time.time() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:140], int((time.time() - t0) * 1000)


def main() -> None:
    env = load_env(ENV_FILE)
    token = env.get("ACCESS_TOKEN") or env.get("RUANGNODE_API_ACCESS_TOKEN", "")
    ah = {"X-Access-Token": token}

    now = datetime.datetime.now(WIB)
    out = {
        "checked_at": now.isoformat(),
        "checked_at_display": now.strftime("%d %b %Y %H:%M WIB"),
        "engines": {},
        "checks": [],
    }

    # yt-dlp (versi library di venv app)
    try:
        import yt_dlp  # noqa: PLC0415
        out["engines"]["ytdlp"] = {
            "ok": True, "version": yt_dlp.version.__version__,
        }
    except Exception as e:  # noqa: BLE001
        out["engines"]["ytdlp"] = {"ok": False, "version": str(e)[:60]}

    # Cobalt langsung
    status, body, ms = http_json(COBALT + "/", timeout=10.0)
    if status == 200 and isinstance(body, dict):
        cob = body.get("cobalt") or {}
        out["engines"]["cobalt"] = {"ok": True,
                                    "version": str(cob.get("version"))}
    else:
        out["engines"]["cobalt"] = {
            "ok": False, "version": str(body)[:60] if body else "no response",
        }
    out["checks"].append({"name": "cobalt", "ok": out["engines"]["cobalt"]["ok"],
                          "ms": ms})

    # API hidup
    status, _, ms = http_json(BASE + "/api/health", timeout=15.0)
    out["checks"].append({"name": "api", "ok": status == 200, "ms": ms})

    # Wakil endpoint unduhan: ytmp4 YouTube (harus berhasil via yt-dlp/cobalt)
    status, body, ms = http_json(BASE + "/api/download/ytmp4", headers=ah,
                                 timeout=150.0, params={"url": YT_SAMPLE})
    engine_ok = False
    if status == 200 and isinstance(body, dict):
        res = body.get("result") or {}
        engine_ok = res.get("engine") in ("yt-dlp", "cobalt") and bool(
            res.get("download"))
    out["checks"].append({"name": "ytmp4 (YouTube)", "ok": engine_ok, "ms": ms})

    # Wakil endpoint lokal murni (tanpa upstream)
    status, _, ms = http_json(BASE + "/api/tools/text2base64", headers=ah,
                              timeout=20.0, params={"text": "cek"})
    out["checks"].append({"name": "text2base64", "ok": status == 200, "ms": ms})

    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    tmp = DEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, DEST)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
