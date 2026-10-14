"""Kategori RE — reverse engineering APK/IPA.

12 endpoint: apk-sniffer, manifest-decoder, apk-secrets, apk-signer,
blutter, flutter, reactnative, js-deobfuscator, so-strings, ipa-strings,
ssl-pinning-finder, unity-il2cpp.

Sifat: endpoint menerima URL berkas milik pemanggil (apk=/ipa=), server
mengunduh ke direktori temp (batas 250 MB), menganalisis LOKAL dengan
tool open source (androguard, strings, jsbeautifier, keytool/jarsigner),
mengembalikan hasil terstruktur, lalu menghapus berkas temp.
"""

import asyncio
import io
import os
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..common import (
    DEFAULT_HEADERS,
    bad_request,
    ok,
    require_url,
    safe_filename,
    upstream_fail,
)
from ..security import require_api_token

try:  # androguard memakai loguru yang sangat berisik
    from loguru import logger as _ag_log
    _ag_log.remove()
except Exception:
    pass

router = APIRouter(prefix="/api/re", dependencies=[Depends(require_api_token)])

UA = DEFAULT_HEADERS["User-Agent"]
MAX_FILE_BYTES = 250 * 1024 * 1024
_NS_ANDROID = "http://schemas.android.com/apk/res/android"

# Kunci debug persisten (dibuat sekali saat pertama dipakai)
KEYS_DIR = os.environ.get("RE_KEYS_DIR", "/home/adit/ruangnode-api/keys")
DEBUG_KEYSTORE = os.path.join(KEYS_DIR, "debug.keystore")
_DEBUG_ALIAS = "androiddebugkey"
_DEBUG_PASS = "android"


# ----------------------------------------------------------------------------
# Bantuan umum
# ----------------------------------------------------------------------------
def _need(text: str | None, name: str) -> str:
    text = (text or "").strip()
    if not text:
        bad_request(f"Parameter {name} wajib diisi.")
    return text


def _limited_note(result: dict, note: str) -> dict:
    result["limited"] = True
    result["note"] = note
    return result


def _cap(items: list, n: int) -> dict:
    return {"total": len(items), "items": items[:n]}


def _redact(secret: str) -> str:
    """Sembunyikan nilai rahasia: hanya awal+akhir yang terlihat."""
    s = secret.strip()
    if len(s) <= 10:
        return s[:2] + "...(teredaksi)"
    return s[:6] + "..." + s[-4:]


async def _fetch_archive(url: str, suffix: str) -> tuple[str, str]:
    """Unduh arsip (APK/IPA = zip) ke temp dir. Kembalikan (tmpdir, path)."""
    url = require_url(url)
    tmpdir = tempfile.mkdtemp(prefix="rn-re-")
    dest = os.path.join(tmpdir, "paket" + suffix)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=120.0, trust_env=False
        ) as client:
            async with client.stream("GET", url, headers={"User-Agent": UA}) as resp:
                if resp.status_code != 200:
                    raise upstream_fail(
                        f"Sumber berkas menolak request (HTTP {resp.status_code})."
                    )
                total = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(1024 * 64):
                        total += len(chunk)
                        if total > MAX_FILE_BYTES:
                            raise bad_request(
                                "Ukuran berkas melebihi batas 250 MB."
                            )
                        f.write(chunk)
        if total == 0:
            raise upstream_fail("Berkas yang diunduh kosong.")
        with open(dest, "rb") as f:
            magic = f.read(4)
        if magic != b"PK\x03\x04":
            raise bad_request("Berkas bukan arsip ZIP yang valid (APK/IPA harus ZIP).")
        return tmpdir, dest
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _cleanup(tmpdir: str) -> None:
    shutil.rmtree(tmpdir, ignore_errors=True)


def _apk_object(path: str):
    from androguard.core.apk import APK
    return APK(path)


def _dex_strings(apk, limit: int = 300_000) -> list:
    """Semua string dari semua file .dex (pola androguard 4.x)."""
    from androguard.core.dex import DEX
    out: list[str] = []
    for name in apk.get_files():
        if not name.endswith(".dex"):
            continue
        try:
            dex = DEX(apk.get_file(name))
            for s in dex.get_strings():
                out.append(s)
                if len(out) >= limit:
                    return out
        except Exception:
            continue
    return out


def _attr(elem, name: str):
    return elem.get("{%s}%s" % (_NS_ANDROID, name))


# ----------------------------------------------------------------------------
# 1. apk-sniffer — profil APK via androguard
# ----------------------------------------------------------------------------
@router.get("/apk-sniffer")
async def re_apk_sniffer(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            files = a.get_files()
            sos = [f for f in files if f.endswith(".so")]
            abis = sorted({f.split("/")[1] for f in sos if f.count("/") >= 2})
            mx = a.get_android_manifest_xml()
            app_elem = mx.find("application")
            debuggable = (
                _attr(app_elem, "debuggable") == "true" if app_elem is not None else False
            )
            size = os.path.getsize(path)
            return {
                "package": a.get_package(),
                "version_code": a.get_androidversion_code(),
                "version_name": a.get_androidversion_name(),
                "min_sdk": a.get_min_sdk_version(),
                "target_sdk": a.get_target_sdk_version(),
                "size_bytes": size,
                "debuggable": debuggable,
                "permissions": _cap(sorted(a.get_permissions()), 60),
                "activities": _cap(a.get_activities(), 40),
                "services": _cap(a.get_services(), 40),
                "receivers": _cap(a.get_receivers(), 40),
                "providers": _cap(a.get_providers(), 40),
                "dex_files": [f for f in files if f.endswith(".dex")],
                "native_libs": {"count": len(sos), "abis": abis,
                                "sample": sos[:12]},
                "signature": {
                    "signed_v1": bool(a.is_signed_v1()),
                    "signed_v2": bool(a.is_signed_v2()),
                    "signature_files": a.get_signature_names(),
                },
                "note": "Analisis lokal via androguard di server.",
            }
        return ok(await asyncio.to_thread(work))
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal menganalisis APK: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 2. manifest-decoder — AndroidManifest.xml -> JSON + deeplink
# ----------------------------------------------------------------------------
def _component_info(a, kind: str, names: list) -> list:
    out = []
    for name in names[:80]:
        info = {"name": name}
        try:
            filts = a.get_intent_filters(kind, name) or {}
            info["actions"] = list(filts.get("action", []))
            info["categories"] = list(filts.get("category", []))
            data = filts.get("data", []) or []
            datas = []
            for d in data[:10]:
                if isinstance(d, dict):
                    datas.append({
                        "scheme": d.get("scheme"),
                        "host": d.get("host"),
                        "port": d.get("port"),
                        "path": d.get("path"),
                    })
                else:
                    datas.append({"raw": str(d)[:160]})
            info["data"] = datas
        except Exception:
            info["actions"] = []
            info["categories"] = []
            info["data"] = []
        out.append(info)
    return out


@router.get("/manifest-decoder")
async def re_manifest_decoder(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            acts = _component_info(a, "activity", a.get_activities())
            svcs = _component_info(a, "service", a.get_services())
            rcvs = _component_info(a, "receiver", a.get_receivers())
            provs = _component_info(a, "provider", a.get_providers())
            # deeplink: VIEW + BROWSABLE pada activities
            deeplinks = []
            for comp in acts:
                if ("android.intent.action.VIEW" in comp["actions"]
                        and "android.intent.category.BROWSABLE" in comp["categories"]):
                    for d in comp["data"]:
                        deeplinks.append({
                            "component": comp["name"],
                            "scheme": d.get("scheme"),
                            "host": d.get("host"),
                            "path": d.get("path"),
                        })
            return {
                "package": a.get_package(),
                "version_name": a.get_androidversion_name(),
                "permissions": sorted(a.get_permissions()),
                "activities": acts,
                "services": svcs,
                "receivers": rcvs,
                "providers": provs,
                "deeplinks": deeplinks,
                "note": "AndroidManifest.xml didekode lokal via androguard.",
            }
        return ok(await asyncio.to_thread(work))
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal mendekode manifest: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 3. apk-secrets — pemindaian rahasia ala APKLeaks (teredaksi)
# ----------------------------------------------------------------------------
SECRET_PATTERNS = [
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Firebase Realtime DB", re.compile(r"https://[a-z0-9-]+\.firebaseio\.com")),
    ("Firebase App", re.compile(r"https?://[a-z0-9-]+\.firebaseapp\.com")),
    ("Google OAuth Client", re.compile(
        r"[0-9]+-[0-9A-Za-z_]{32}\.apps\.googleusercontent\.com")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,80}\b")),
    ("JWT", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("Private Key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("URL berkredensial", re.compile(
        r"https?://[^/\s:@]+:[^/\s@]{2,}@[^\s\"']+")),
    ("Penugasan rahasia", re.compile(
        r"(?i)\b(api[_-]?key|secret|passwd|password|pwd|access[_-]?token|"
        r"client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{6,160})[\"']")),
]

_TEXT_EXT = (".xml", ".json", ".txt", ".properties", ".js", ".ts",
            ".html", ".yml", ".yaml", ".cfg", ".ini")


def _scan_text_for_secrets(text: str, origin: str, findings: list,
                           seen: set, cap: int):
    for label, pat in SECRET_PATTERNS:
        if len(findings) >= cap:
            return
        for m in pat.finditer(text):
            if len(findings) >= cap:
                return
            if m.lastindex and m.lastindex >= 2:
                value, key_name = m.group(2), m.group(1)
                shown = f"{key_name} = {_redact(value)}"
            else:
                shown = _redact(m.group(0))
            key = (label, shown)
            if key in seen:
                continue
            seen.add(key)
            findings.append({"type": label, "value": shown, "source": origin})


@router.get("/apk-secrets")
async def re_apk_secrets(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            findings: list[dict] = []
            seen: set = set()
            cap = 120
            # 1) string dex
            for s in _dex_strings(a):
                if len(findings) >= cap:
                    break
                _scan_text_for_secrets(s, "classes*.dex", findings, seen, cap)
            # 2) berkas teks di dalam APK (maks 512 KB per berkas,
            #    anggaran total 30 MB agar semua berkas terpindai)
            text_budget = 30 * 1024 * 1024
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if len(findings) >= cap or text_budget <= 0:
                        break
                    name = info.filename
                    if not name.lower().endswith(_TEXT_EXT):
                        continue
                    if info.file_size > 512 * 1024:
                        continue
                    try:
                        raw = zf.read(name)
                    except Exception:
                        continue
                    text_budget -= len(raw)
                    text = raw.decode("utf-8", "ignore")
                    _scan_text_for_secrets(text, name, findings, seen, cap)
            return {
                "package": a.get_package(),
                "findings_count": len(findings),
                "findings": findings,
                "note": ("Pemindaian pola ala APKLeaks di string dex + berkas "
                         "teks APK. Nilai rahasia TEREDAKSI (awal+akhir saja) "
                         "dan tidak dikembalikan utuh."),
            }
        return ok(await asyncio.to_thread(work))
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal memindai rahasia: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 4. apk-signer — tanda tangani APK dengan kunci debug server
# ----------------------------------------------------------------------------
def _ensure_debug_keystore() -> None:
    os.makedirs(KEYS_DIR, exist_ok=True)
    if os.path.exists(DEBUG_KEYSTORE):
        return
    if shutil.which("keytool") is None:
        raise FileNotFoundError("keytool tidak tersedia di server.")
    dname = "CN=RuangNode Debug, OU=RE, O=RuangNode, L=Jakarta, C=ID"
    cmd = ["keytool", "-genkeypair", "-v",
           "-keystore", DEBUG_KEYSTORE,
           "-alias", _DEBUG_ALIAS,
           "-keyalg", "RSA", "-keysize", "2048",
           "-validity", "10950",
           "-storepass", _DEBUG_PASS, "-keypass", _DEBUG_PASS,
           "-dname", dname]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not os.path.exists(DEBUG_KEYSTORE):
        raise RuntimeError("keytool gagal membuat keystore: "
                           + (proc.stderr or "")[:300])
    os.chmod(DEBUG_KEYSTORE, 0o600)


def _cert_info() -> dict:
    cmd = ["keytool", "-list", "-v", "-keystore", DEBUG_KEYSTORE,
           "-storepass", _DEBUG_PASS, "-alias", _DEBUG_ALIAS]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    info = {"alias": _DEBUG_ALIAS}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        for prefix, key in (("Owner:", "owner"), ("Issuer:", "issuer"),
                            ("Valid from:", "valid"),
                            ("SHA256:", "sha256"), ("SHA1:", "sha1")):
            if line.startswith(prefix):
                info[key] = line[len(prefix):].strip()[:220]
    return info


@router.get("/apk-signer")
async def re_apk_signer(apk: str = Query(default="")):
    if shutil.which("keytool") is None or shutil.which("jarsigner") is None:
        return ok(_limited_note(
            {"signable": False},
            "Toolchain penandatangan (keytool/jarsigner) tidak tersedia di "
            "server ini, sehingga APK tidak dapat ditandatangani."))
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            _ensure_debug_keystore()
            signed = os.path.join(tmpdir, "signed.apk")
            shutil.copyfile(path, signed)
            cmd = ["jarsigner", "-keystore", DEBUG_KEYSTORE,
                   "-storepass", _DEBUG_PASS, "-keypass", _DEBUG_PASS,
                   "-signedjar", signed, signed, _DEBUG_ALIAS]
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=300)
            if proc.returncode != 0:
                raise RuntimeError("jarsigner gagal: "
                                   + (proc.stderr or proc.stdout or "")[:300])
            verify = subprocess.run(["jarsigner", "-verify", signed],
                                    capture_output=True, text=True, timeout=120)
            data = open(signed, "rb").read()
            a = _apk_object(path)
            fname = safe_filename((a.get_package() or "app") + "-signed.apk")
            return data, fname, _cert_info(), "jar verified" in (
                verify.stdout or "").lower()
        data, fname, cert, verified = await asyncio.to_thread(work)
        resp = Response(content=data,
                        media_type="application/vnd.android.package-archive")
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        resp.headers["X-RuangNode-Cert-SHA256"] = (
            cert.get("sha256", "")[:64])
        resp.headers["X-RuangNode-Signer"] = (
            "jarsigner v1 (JAR signature); tanpa zipalign — "
            "cocok untuk analisis/uji, bukan rilis Play Store")
        return resp
    except HTTPException:
        raise
    except (FileNotFoundError, RuntimeError) as e:
        return ok(_limited_note({"signable": False}, str(e)[:300]))
    except Exception as e:
        upstream_fail(f"Gagal menandatangani APK: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# Bantuan analisis framework (Flutter / React Native / Unity)
# ----------------------------------------------------------------------------
def _so_paths(apk, needle: str) -> list:
    return [f for f in apk.get_files()
            if f.startswith("lib/") and f.endswith(".so") and needle in f]


def _flutter_libs(apk) -> tuple[list, list]:
    """(libflutter.so paths, libapp.so paths)."""
    return _so_paths(apk, "libflutter.so"), _so_paths(apk, "libapp.so")


def _strings_of_file(path: str, min_len: int = 6) -> list:
    """strings murni-Python (fallback bila biner strings tak ada)."""
    try:
        data = open(path, "rb").read()
    except OSError:
        return []
    pattern = rb"[\x20-\x7e]{%d,}" % min_len
    out = []
    for m in re.finditer(pattern, data):
        try:
            out.append(m.group(0).decode("ascii"))
        except UnicodeDecodeError:
            continue
        if len(out) >= 200_000:
            break
    return out


def _system_strings(path: str, min_len: int = 6, cap: int = 120_000) -> list:
    if shutil.which("strings"):
        try:
            proc = subprocess.run(["strings", f"-n{min_len}", path],
                                  capture_output=True, timeout=180)
            lines = proc.stdout.decode("utf-8", "ignore").splitlines()
            return lines[:cap]
        except Exception:
            pass
    return _strings_of_file(path, min_len)


_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")
_INTERESTING_SO = re.compile(
    r"(?i)(error|failed|exception|Java_|jni|/data/|/system/|/sdcard/|"
    r"\.so\b|token|secret|key|pin|ssl|cert|aes|rsa|hmac|sha256|md5|"
    r"http)")


# ----------------------------------------------------------------------------
# 5. blutter — deteksi Flutter; subset analisis (Blutter tidak terinstal)
# ----------------------------------------------------------------------------
@router.get("/blutter")
async def re_blutter(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            flutter_so, app_so = _flutter_libs(a)
            if not flutter_so and not app_so:
                return ok({
                    "flutter_detected": False,
                    "package": a.get_package(),
                    "note": "Tidak ditemukan libflutter.so/libapp.so — "
                            "bukan aplikasi Flutter.",
                })
            extract = os.path.join(tmpdir, "x")
            os.makedirs(extract, exist_ok=True)
            dart_objs: list[str] = []
            urls: list[str] = []
            engine_ver = None
            if app_so:
                with zipfile.ZipFile(path) as zf:
                    zf.extract(app_so[0], extract)
                for s in _system_strings(os.path.join(extract, app_so[0])):
                    for u in _URL_RE.findall(s):
                        if u not in urls and len(urls) < 60:
                            urls.append(u[:220])
                    if re.match(r"^[A-Z][A-Za-z0-9_]*(::[A-Za-z0-9_]+)+$",
                                s) and len(dart_objs) < 80:
                        dart_objs.append(s[:160])
            if flutter_so:
                with zipfile.ZipFile(path) as zf:
                    zf.extract(flutter_so[0], extract)
                for s in _system_strings(os.path.join(extract, flutter_so[0])):
                    m = re.search(r"Dart VM version:\s*([0-9][\w.\-+]+)", s)
                    if m and not engine_ver:
                        engine_ver = m.group(1)
                    if engine_ver:
                        break
            res = {
                "flutter_detected": True,
                "package": a.get_package(),
                "libflutter_so": flutter_so,
                "libapp_so": app_so,
                "engine_version_hint": engine_ver,
                "dart_objects_sample": dart_objs,
                "urls_in_libapp": urls,
            }
            return ok(_limited_note(
                res,
                "Blutter (worawit/blutter) membutuhkan pipeline analisis "
                "Dart VM tersendiri yang tidak terinstal di server ini. "
                "Yang dikembalikan adalah subset analisis: info engine + "
                "objek Dart/URL dari strings libapp.so."))
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal analisis Blutter: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 6. flutter — analisis Flutter berbasis strings
# ----------------------------------------------------------------------------
_FLUTTER_PLUGIN_HINT = re.compile(
    r"([a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+/(?:[A-Za-z0-9_]+))")


@router.get("/flutter")
async def re_flutter(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            flutter_so, app_so = _flutter_libs(a)
            if not flutter_so and not app_so:
                return ok({
                    "flutter_detected": False,
                    "package": a.get_package(),
                    "note": "Bukan aplikasi Flutter (libflutter.so/libapp.so "
                            "tidak ditemukan).",
                })
            extract = os.path.join(tmpdir, "x")
            os.makedirs(extract, exist_ok=True)
            strings_all: list[str] = []
            with zipfile.ZipFile(path) as zf:
                for so in (flutter_so[:1] + app_so[:1]):
                    try:
                        zf.extract(so, extract)
                        strings_all += _system_strings(
                            os.path.join(extract, so))
                    except Exception:
                        continue
            engine_ver = None
            channels: list[str] = []
            urls: list[str] = []
            plugins: list[str] = []
            for s in strings_all:
                if not engine_ver:
                    m = re.search(r"Dart VM version:\s*([0-9][\w.\-+]+)", s)
                    if m:
                        engine_ver = m.group(1)
                if "plugins.flutter.io" in s and len(channels) < 60:
                    if s not in channels:
                        channels.append(s[:160])
                for u in _URL_RE.findall(s):
                    if u not in urls and len(urls) < 80:
                        urls.append(u[:220])
                if ("MethodChannel" in s or "EventChannel" in s) \
                        and len(plugins) < 60 and len(s) < 160:
                    if s not in plugins:
                        plugins.append(s)
            assets = [f for f in a.get_files()
                      if f.startswith("assets/flutter_assets/")][:120]
            return ok({
                "flutter_detected": True,
                "package": a.get_package(),
                "engine_version_hint": engine_ver,
                "assets_flutter": _cap(assets, 120),
                "method_channels_sample": channels,
                "plugins_sample": plugins,
                "urls": urls,
                "note": "Analisis strings libflutter.so/libapp.so + daftar "
                        "aset; versi engine berupa petunjuk dari string.",
            })
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal analisis Flutter: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 7. reactnative — deteksi & analisis React Native
# ----------------------------------------------------------------------------
@router.get("/reactnative")
async def re_reactnative(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            files = a.get_files()
            bundle = next(
                (f for f in files if f.endswith("index.android.bundle")), None)
            hermes = bool(_so_paths(a, "libhermes.so"))
            jsc = bool(_so_paths(a, "libjsc.so") or _so_paths(a, "libjscexecutor.so"))
            rn_jni = bool(_so_paths(a, "libreactnativejni.so"))
            detected = bool(bundle or hermes or rn_jni)
            if not detected:
                return ok({
                    "reactnative_detected": False,
                    "package": a.get_package(),
                    "note": "Tidak ditemukan index.android.bundle / "
                            "libhermes.so / libreactnativejni.so.",
                })
            res: dict = {
                "reactnative_detected": True,
                "package": a.get_package(),
                "js_engine": ("hermes" if hermes else
                              "jsc" if jsc else "tidak diketahui"),
                "has_bundle": bool(bundle),
                "libhermes": hermes,
                "libreactnativejni": rn_jni,
            }
            if bundle:
                with zipfile.ZipFile(path) as zf:
                    info = zf.getinfo(bundle)
                    res["bundle_size_bytes"] = info.file_size
                    head = zf.read(bundle)[:64]
                    res["hermes_bytecode"] = head.startswith(b"HBC")
                    data = zf.read(bundle)
                    if len(data) > 6 * 1024 * 1024:
                        data = data[:6 * 1024 * 1024]
                    text = data.decode("utf-8", "ignore")
                    urls, modules = [], []
                    for u in _URL_RE.findall(text):
                        if u not in urls and len(urls) < 80:
                            urls.append(u[:220])
                    for m in re.finditer(
                            r"__d\(\s*(?:function[^,]*,\s*)?(\d+|\"[^\"]{1,80}\")",
                            text):
                        if len(modules) < 80:
                            modules.append(m.group(1)[:80])
                    res["urls_in_bundle"] = urls
                    res["module_ids_sample"] = modules
            res["note"] = ("Bundle JS diekstrak dan dipindai polanya; "
                           "Hermes bytecode tidak didekompilasi penuh.")
            return ok(res)
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal analisis React Native: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 8. js-deobfuscator — beautify bundle JS dari APK
# ----------------------------------------------------------------------------
@router.get("/js-deobfuscator")
async def re_js_deobfuscator(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            files = a.get_files()
            bundle = next(
                (f for f in files if f.endswith("index.android.bundle")), None)
            if not bundle:
                cands = [f for f in files
                         if f.endswith(".js") and f.startswith("assets/")]
                bundle = cands[0] if cands else None
            if not bundle:
                return ok(_limited_note(
                    {"bundle_found": False, "package": a.get_package()},
                    "Tidak ditemukan bundle JS (index.android.bundle / *.js "
                    "di assets) pada APK ini."))
            with zipfile.ZipFile(path) as zf:
                raw = zf.read(bundle)
            if len(raw) > 4 * 1024 * 1024:
                return ok(_limited_note(
                    {"bundle_found": True, "bundle": bundle,
                     "size_bytes": len(raw)},
                    "Bundle melebihi 4 MB — tidak diproses agar server "
                    "tidak kehabisan memori."))
            text = raw.decode("utf-8", "ignore")
            import jsbeautifier
            pretty = jsbeautifier.beautify(text)
            before_lines = text.count("\n") + 1
            after_lines = pretty.count("\n") + 1
            return ok({
                "bundle_found": True,
                "bundle": bundle,
                "size_bytes": len(raw),
                "lines_before": before_lines,
                "lines_after": after_lines,
                "expansion_ratio": round(after_lines / max(before_lines, 1), 2),
                "snippet": pretty[:6000],
                "note": "Bundle JS dirapikan dengan jsbeautifier; cuplikan "
                        "6000 karakter pertama ditampilkan.",
            })
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal deobfuscate JS: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 9. so-strings — strings bermakna dari semua lib native
# ----------------------------------------------------------------------------
@router.get("/so-strings")
async def re_so_strings(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            sos = [f for f in a.get_files() if f.endswith(".so")]
            if not sos:
                return ok({
                    "package": a.get_package(),
                    "so_count": 0,
                    "note": "APK ini tidak menyertakan library native (.so).",
                })
            extract = os.path.join(tmpdir, "x")
            os.makedirs(extract, exist_ok=True)
            per_file = []
            total_hits = 0
            with zipfile.ZipFile(path) as zf:
                for so in sos[:30]:
                    try:
                        zf.extract(so, extract)
                    except Exception:
                        continue
                    hits = []
                    for s in _system_strings(os.path.join(extract, so)):
                        if len(hits) >= 60:
                            break
                        s = s.strip()
                        if len(s) < 6 or len(s) > 220:
                            continue
                        if _URL_RE.search(s) or _INTERESTING_SO.search(s):
                            if s not in hits:
                                hits.append(s)
                    total_hits += len(hits)
                    per_file.append({"file": so, "hits": hits})
                    if total_hits >= 400:
                        break
            return ok({
                "package": a.get_package(),
                "so_count": len(sos),
                "analyzed": len(per_file),
                "total_hits": total_hits,
                "files": per_file,
                "note": "strings dari tiap .so, disaring yang bermakna "
                        "(URL, path, pesan error, nama fungsi JNI, kripto).",
            })
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal analisis .so: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 10. ipa-strings — endpoint & string dari biner IPA
# ----------------------------------------------------------------------------
@router.get("/ipa-strings")
async def re_ipa_strings(ipa: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(ipa, "ipa"), ".ipa")
    try:
        def work():
            extract = os.path.join(tmpdir, "x")
            with zipfile.ZipFile(path) as zf:
                apps = [n for n in zf.namelist()
                        if n.startswith("Payload/") and n.count("/") == 2
                        and n.endswith(".app/")]
                if not apps:
                    apps = list({"/".join(n.split("/")[:3]) + "/"
                                 for n in zf.namelist()
                                 if n.startswith("Payload/")
                                 and n.split("/")[2].endswith(".app")})
                if not apps:
                    raise ValueError("Struktur IPA tidak valid: "
                                     "Payload/*.app tidak ditemukan.")
                zf.extractall(extract)
            app_dir = os.path.join(extract, apps[0])
            plist_path = os.path.join(app_dir, "Info.plist")
            if not os.path.exists(plist_path):
                raise ValueError("Info.plist tidak ditemukan di .app.")
            with open(plist_path, "rb") as f:
                info = plistlib.load(f)
            exe = info.get("CFBundleExecutable", "")
            binary_path = os.path.join(app_dir, exe) if exe else ""
            strings_out: list[str] = []
            urls: list[str] = []
            if binary_path and os.path.exists(binary_path):
                for s in _system_strings(binary_path):
                    s = s.strip()
                    if len(s) < 6 or len(s) > 240:
                        continue
                    if _URL_RE.search(s):
                        for u in _URL_RE.findall(s):
                            if u not in urls and len(urls) < 120:
                                urls.append(u[:220])
                    elif _INTERESTING_SO.search(s):
                        if len(strings_out) < 300 and s not in strings_out:
                            strings_out.append(s)
            url_types = []
            for ut in info.get("CFBundleURLTypes", []) or []:
                url_types.append({
                    "name": ut.get("CFBundleURLName"),
                    "schemes": ut.get("CFBundleURLSchemes"),
                })
            return ok({
                "bundle_id": info.get("CFBundleIdentifier"),
                "bundle_name": info.get("CFBundleName"),
                "version": info.get("CFBundleShortVersionString"),
                "build": info.get("CFBundleVersion"),
                "executable": exe,
                "binary_size_bytes": (os.path.getsize(binary_path)
                                      if binary_path
                                      and os.path.exists(binary_path) else 0),
                "url_schemes": url_types,
                "queried_schemes": info.get("LSApplicationQueriesSchemes"),
                "endpoints": urls,
                "interesting_strings": strings_out,
                "note": "Biner Mach-O di-strings; endpoint & string menarik "
                        "disaring polanya. Info.plist dibaca via plistlib.",
            })
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except ValueError as e:
        bad_request(str(e))
    except Exception as e:
        upstream_fail(f"Gagal analisis IPA: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 11. ssl-pinning-finder — indikator pinning
# ----------------------------------------------------------------------------
@router.get("/ssl-pinning-finder")
async def re_ssl_pinning_finder(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            indicators: list[dict] = []
            files = a.get_files()

            # 1) network_security_config.xml
            nsc = [f for f in files
                   if f.endswith("network_security_config.xml")]
            if nsc:
                try:
                    raw = a.get_file(nsc[0])
                    root = ET.fromstring(raw)
                    pins = [p.text.strip() for p in root.iter("pin")
                            if p.text and p.text.strip()]
                    cleartext = None
                    for dc in root.iter("domain-config"):
                        ct = dc.get("cleartextTrafficPermitted")
                        if ct is not None:
                            cleartext = ct
                    indicators.append({
                        "indicator": "network_security_config.xml",
                        "found": True,
                        "evidence": {
                            "file": nsc[0],
                            "pin_count": len(pins),
                            "pins_sample": [_redact(p) for p in pins[:6]],
                            "cleartext_permitted": cleartext,
                        },
                    })
                except Exception as e:
                    indicators.append({
                        "indicator": "network_security_config.xml",
                        "found": True,
                        "evidence": {"file": nsc[0],
                                     "parse_error": str(e)[:120]},
                    })
            else:
                indicators.append({
                    "indicator": "network_security_config.xml",
                    "found": False,
                    "evidence": {},
                })

            # 2) pola di string dex
            patterns = [
                ("CertificatePinner (OkHttp)", r"CertificatePinner"),
                ("SHA-256 pin", r"sha256/"),
                ("OkHttp3", r"okhttp3"),
                ("TrustManager kustom", r"X509TrustManager"),
                ("checkServerTrusted", r"checkServerTrusted"),
                ("HostnameVerifier kustom", r"HostnameVerifier"),
                ("TrustKit", r"TrustKit"),
                ("sebutan pinning", r"(?i)ssl.?pinning|certificate.?pinning"),
            ]
            dex_strs = _dex_strings(a)
            joined = "\n".join(dex_strs[:200_000])
            for label, pat in patterns:
                m = re.search(pat, joined)
                indicators.append({
                    "indicator": label,
                    "found": bool(m),
                    "evidence": ({"sample": m.group(0)[:120]}
                                 if m else {}),
                })

            found_any = any(i["found"] for i in indicators)
            return ok({
                "package": a.get_package(),
                "pinning_indicated": found_any,
                "indicators": indicators,
                "note": ("Konklusi dari kombinasi indikator; 'found' pada "
                         "indikator individual berarti pola terdeteksi di "
                         "string dex / konfigurasi."),
            })
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal mencari SSL pinning: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)


# ----------------------------------------------------------------------------
# 12. unity-il2cpp — deteksi & analisis Unity IL2CPP
# ----------------------------------------------------------------------------
@router.get("/unity-il2cpp")
async def re_unity_il2cpp(apk: str = Query(default="")):
    tmpdir, path = await _fetch_archive(_need(apk, "apk"), ".apk")
    try:
        def work():
            a = _apk_object(path)
            files = a.get_files()
            il2cpp_so = [f for f in files if f.endswith("libil2cpp.so")]
            meta = [f for f in files
                    if f.endswith("global-metadata.dat")]
            mono_so = [f for f in files
                       if "/libmono" in f and f.endswith(".so")]
            if not il2cpp_so and not meta:
                return ok({
                    "il2cpp_detected": False,
                    "package": a.get_package(),
                    "mono_detected": bool(mono_so),
                    "note": "libil2cpp.so / global-metadata.dat tidak "
                            "ditemukan — bukan game Unity IL2CPP.",
                })
            res: dict = {
                "il2cpp_detected": True,
                "package": a.get_package(),
                "libil2cpp_so": il2cpp_so,
                "metadata_file": meta[0] if meta else None,
                "mono_libs": mono_so[:8],
            }
            if meta:
                extract = os.path.join(tmpdir, "x")
                os.makedirs(extract, exist_ok=True)
                with zipfile.ZipFile(path) as zf:
                    zf.extract(meta[0], extract)
                mp = os.path.join(extract, meta[0])
                with open(mp, "rb") as f:
                    header = f.read(32)
                magic = header[:4]
                res["metadata_size_bytes"] = os.path.getsize(mp)
                res["magic_ok"] = (magic == b"\xaf\x1b\xb1\xfa")
                if len(header) >= 8:
                    res["metadata_version"] = struct.unpack("<i", header[4:8])[0]
                classes: list[str] = []
                for s in _strings_of_file(mp, 4):
                    if re.match(r"^[A-Z][\w.]*::[A-Za-z_][\w]*$", s):
                        if s not in classes and len(classes) < 80:
                            classes.append(s[:120])
                    if len(classes) >= 80:
                        break
                res["class_method_sample"] = classes
            res["note"] = ("Versi metadata dibaca dari header "
                           "global-metadata.dat; daftar kelas/metode dari "
                           "strings metadata.")
            return ok(res)
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except Exception as e:
        upstream_fail(f"Gagal analisis IL2CPP: {type(e).__name__}.")
    finally:
        _cleanup(tmpdir)
