"""Backend AI RuangNode — semuanya memakai infrastruktur milik pemilik.

- 9Router (api.pixelspace.my.id): gateway OpenAI-compatible dengan 581 model.
  dipakai untuk chat, persona, vision. API key via env NINE_ROUTER_API_KEY.
- Cloudflare Workers AI milik pemilik: untuk text-to-image (flux-1-schnell).
  Kredensial via env CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN.

Bukan agregator pihak ketiga: ini akun/gateway pemilik sendiri.
"""

import base64
import json
import os
from collections import OrderedDict

import httpx

from .common import upstream_fail

GATEWAY = "https://api.pixelspace.my.id"
CF_API = "https://api.cloudflare.com/client/v4"

# Model yang TERVERIFIKASI menjawab (diuji live 2026-10-14):
M_CHATGPT = "cl/openai/gpt-6.1-sol-pro"
M_MISTRAL = "cf/@cf/mistralai/mistral-small-3.1-24b-instruct"  # model Mistral asli, disajikan via Cloudflare
M_QWEN = "cl/qwen/qwen3.8-max-prime"
M_CLAUDE = "cl/anthropic/claude-sonnet-5.5"
M_HAIKU = "cl/anthropic/claude-haiku-5.5"
M_NOVA = "cl/google/gemma-3-27b-it"
M_VISION = "cl/qwen/qwen2.5-vl-72b-instruct"

CF_IMG_MODEL = "@cf/black-forest-labs/flux-1-schnell"


def _nine_key() -> str:
    key = os.environ.get("NINE_ROUTER_API_KEY", "")
    if not key:
        upstream_fail("Env NINE_ROUTER_API_KEY belum di-set di server.")
    return key


def _cf_creds() -> tuple[str, str]:
    aid = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not aid or not token:
        upstream_fail("Env CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN belum di-set di server.")
    return aid, token


async def nine_chat(messages: list[dict], model: str, max_tokens: int = 1024,
                    timeout: float = 120.0) -> str:
    """Chat completions via gateway 9Router milik pemilik."""
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(
                f"{GATEWAY}/v1/chat/completions",
                headers={"Authorization": f"Bearer {_nine_key()}",
                         "Content-Type": "application/json"},
                json={"model": model, "messages": messages,
                      "max_tokens": max_tokens},
            )
    except httpx.HTTPError:
        upstream_fail("Gateway 9Router tidak dapat dihubungi.")
    if resp.status_code != 200:
        upstream_fail(f"Gateway 9Router membalas HTTP {resp.status_code}.")
    try:
        text = resp.text.strip()
        # 9Router menempelkan suffix SSE "data: [DONE]" setelah objek JSON
        # (kadang langsung menempel tanpa newline, kadang dengan newline).
        idx = text.rfind("data: [DONE]")
        if idx != -1:
            text = text[:idx].rstrip()
        data = json.loads(text)
        # 9Router membungkus respons OpenAI dalam kunci "data".
        payload = data.get("data", data)
        return payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError, AttributeError):
        upstream_fail("Respons gateway 9Router tidak terduga.")


async def nine_vision(image_b64: str, prompt: str, model: str = M_VISION,
                      max_tokens: int = 512) -> str:
    """Chat dengan input gambar (vision) via gateway 9Router."""
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ],
    }]
    return await nine_chat(messages, model=model, max_tokens=max_tokens,
                           timeout=150.0)


async def cf_run_image(prompt: str, model: str = CF_IMG_MODEL,
                       timeout: float = 180.0) -> bytes:
    """Text-to-image via Cloudflare Workers AI milik pemilik. -> JPEG bytes."""
    aid, token = _cf_creds()
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(
                f"{CF_API}/accounts/{aid}/ai/run/{model}",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"prompt": prompt},
            )
    except httpx.HTTPError:
        upstream_fail("Cloudflare Workers AI tidak dapat dihubungi.")
    if resp.status_code != 200:
        upstream_fail(f"Cloudflare Workers AI membalas HTTP {resp.status_code}.")
    try:
        data = resp.json()
        if not data.get("success"):
            upstream_fail("Cloudflare Workers AI gagal memproses permintaan.")
        raw = base64.b64decode(data["result"]["image"])
    except (KeyError, ValueError, TypeError):
        upstream_fail("Respons gambar Cloudflare tidak terduga.")
    if not raw.startswith(b"\xff\xd8"):
        upstream_fail("Hasil gambar Cloudflare bukan berkas JPEG valid.")
    return raw


# ----------------------------------------------------------------------------
# Sesi chat sederhana (in-memory, dibatasi agar tidak bocor memori)
# ----------------------------------------------------------------------------
class SessionStore:
    def __init__(self, max_sessions: int = 200, max_messages: int = 24):
        self._store: "OrderedDict[str, list[dict]]" = OrderedDict()
        self.max_sessions = max_sessions
        self.max_messages = max_messages

    def get(self, session_id: str) -> list[dict]:
        msgs = self._store.get(session_id, [])
        self._store[session_id] = msgs
        self._store.move_to_end(session_id)
        while len(self._store) > self.max_sessions:
            self._store.popitem(last=False)
        return msgs

    def add(self, session_id: str, role: str, content: str) -> None:
        msgs = self.get(session_id)
        msgs.append({"role": role, "content": content})
        del msgs[: max(0, len(msgs) - self.max_messages)]

    def build(self, session_id: str, system: str,
              user_msg: str) -> list[dict]:
        msgs = [{"role": "system", "content": system}]
        msgs.extend(self.get(session_id))
        msgs.append({"role": "user", "content": user_msg})
        return msgs


SESSIONS = SessionStore()
