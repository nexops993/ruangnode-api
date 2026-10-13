# RuangNode API

REST API pribadi Adit — payung multi-modul (Download, AI, Tools, Stalk, Pterodactyl, RE, Info, Maker).
Fase saat ini: **website + gerbang Access Token** (modul-modul endpoint dibangun menyusul per kategori sampai tuntas).

## Arsitektur keamanan

- Seluruh halaman web terkunci di balik layar kunci; tanpa cookie sesi valid tidak ada isi yang terlihat.
- Semua route `/api/*` (kecuali `/api/health`) wajib Access Token via header `X-Access-Token`, `Authorization: Bearer <token>`, atau query `?token=`.
- Cookie sesi ditandatangani HMAC-SHA256 (secret server-side), berlaku 7 hari, `httpOnly`.
- Envelope JSON API seragam: `{"status": true/false, "creator": "RuangNode", "result": ...}`.

## Menjalankan lokal

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
ACCESS_TOKEN=dev-token SESSION_SECRET=dev-secret \
  uvicorn app.main:app --host 127.0.0.1 --port 8090 --reload
```

## Deploy (VPS)

```bash
# di VPS, sebagai root
git clone https://github.com/nexops993/ruangnode-api.git /home/adit/ruangnode-api
cd /home/adit/ruangnode-api
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

mkdir -p /etc/ruangnode-api
printf 'ACCESS_TOKEN=%s\n' "$(openssl rand -hex 24)" > /etc/ruangnode-api/env
printf 'SESSION_SECRET=%s\n' "$(openssl rand -hex 32)" >> /etc/ruangnode-api/env
chmod 600 /etc/ruangnode-api/env
chown root:root /etc/ruangnode-api/env
```

systemd unit `ruangnode-api` (lihat `deploy/ruangnode-api.service`), lalu:

```bash
systemctl daemon-reload && systemctl enable --now ruangnode-api
```

Publik via tunnel Cloudflare `iptv`: hostname `api.ruangnode.me` → `http://localhost:8090`.
