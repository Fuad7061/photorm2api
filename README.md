# Photoroom 2API — Self-hosted Gateway + Dashboard

Reverse-engineered Photoroom (`v2026.37.01`) capabilities exposed as an OpenAI-compatible HTTP API, with a full admin dashboard for keys, logs, playground and docs. Designed for one-command deployment on **Coolify** via Dockerfile, with **persistent state** so redeploys/updates never lose accounts or keys.

## Features

- Endpoints: mask (remove background), 2x upscale, erase, fill, inpaint, uncrop/outpaint
- OpenAI-compatible: `/v1/models`, `/v1/images/edits`, `/v1/images/generations`
- Every endpoint accepts **multipart uploads**, **image URLs** or **base64** (raw or data URI)
- **Output storage mode:** default `tmpfiles` auto-uploads every result to `https://tmpfiles.org/api/v1/upload` (48h expiry) and returns a final download URL — **nothing is stored on your VPS**. Switch to `local` in the dashboard to keep files in `/app/data/outputs`.
- One-click cleanup: delete all generated files, all logs, or everything from Settings. Logs auto-expire after **10 days** (configurable).
- Dashboard at `/`: overview, playground (all models + generated download links), models list, full cURL docs with every parameter, live logs, settings
- Firebase anonymous account created once and persisted in a Docker volume (`data/anon_token.json`), auto-refreshed
- API keys + admin key stored in SQLite (`data/app.db`) on the same volume; rotate/manage from the UI
- Request + audit logging, visible live in the dashboard

---

## Deploy on Coolify (Dockerfile)

1. Push this repo to GitHub.
2. In Coolify: **New resource → Application → Public Git Repository**, paste the repo URL.
3. Build pack: **Dockerfile** (auto-detected). Port: **8000**.
4. Add a persistent volume: **`/app/data`** (this is mandatory — it stores the account, keys and logs).
5. Environment variables (optional):
   - `ADMIN_KEY` — dashboard admin key. If blank, one is generated and written to `data/ADMIN_KEY.txt` inside the volume.
   - `DEMO_KEY` — initial API key. If blank, one is generated.
   - `FLASK_SECRET` — session signing secret. Set a long random string.
6. Deploy. Open `http://<your-domain>/` and sign in with the admin key.

Local Docker equivalent:

```bash
docker compose up -d --build
# admin key (if auto-generated):
docker compose exec photoroom-2api cat /app/data/ADMIN_KEY.txt
```

Local Python (no Docker):

```bash
pip install -r requirements.txt
python app.py        # http://127.0.0.1:8000
```

> Persistence note: all mutable state lives under `DATA_DIR` (`/app/data` in Docker). Keep that path mounted across deploys. Updating the image/code never touches it, so the Firebase anonymous UID, API keys, admin key and logs survive.

---

## Authentication

- **API endpoints:** `Authorization: Bearer <API_KEY>` (default generated key shown on first run, or set `DEMO_KEY`).
- **Dashboard / admin API:** sign in at `/` with the admin key (or send `X-Admin-Key` header).

Rotate or add keys anytime in **Settings** in the dashboard.

---

## Available models / endpoints

| Endpoint | What it does | Required inputs |
|---|---|---|
| `POST /api/mask` | AI background removal | `image`, optional `model_type`: `person` / `object` / `graphics` / `standard` |
| `POST /api/upscale` | 2x AI upscale (v3) | `image` |
| `POST /api/erase` | AI erase | `image` + `mask` (white = erase) |
| `POST /api/fill` | AI generative fill | `image` + `mask` + `prompt` |
| `POST /api/inpaint` | AI inpainting | `image` + `mask` (white = redraw, area <50%) |
| `POST /api/uncrop` | AI outpainting | `image`, `target_w`, `target_h` (default 768, max 2048), optional `prompt` |
| `POST /v1/images/edits` | OpenAI cutout → transparent PNG, `b64_json` | `image`, optional `model` (`person`...) |
| `POST /v1/images/generations` | OpenAI text-to-image, `b64_json` | `prompt` |
| `GET /v1/models` | OpenAI model list | — |

By default every endpoint returns JSON containing a final download URL (tmpfiles.org, 48h, or local `/files/…`). Options:
- `response_format=b64_json` (or `?format=b64_json`) → inline base64
- `?format=raw` → raw image bytes
- `?redirect=1` (or `response_format=redirect`) → 302 to the file; use `curl -L -o file.png`
- `/v1/images/*` always return JSON with both `url` and `b64_json`.

---

## cURL examples

Set your key once: `export API_KEY=sk-pr-...`. Responses are JSON with the final download URL in `data[0].url`.

### Image URL input → download link

```bash
RESP=$(curl -s -X POST "http://localhost:8000/api/upscale" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"image": "https://example.com/photo.jpg"}')
echo "$RESP"
echo "URL: $(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['data'][0]['url'])")"
```

### Direct download in one step (either storage mode)

```bash
curl -L "http://localhost:8000/api/upscale?redirect=1" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"image": "https://example.com/photo.jpg"}' \
  --output upscaled.png
```

### Base64 input (raw or data URI)

```bash
IMG_B64=$(base64 -i photo.jpg | tr -d '\n')
curl -s -X POST "http://localhost:8000/api/upscale" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"image\": \"data:image/jpeg;base64,$IMG_B64\"}"
```

### Multipart upload

```bash
curl -s -X POST "http://localhost:8000/api/upscale" \
  -H "Authorization: Bearer $API_KEY" \
  -F "image=@photo.jpg;type=image/jpeg"
```

### Mask / erase / fill

```bash
curl -s -X POST "http://localhost:8000/api/fill" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"image": "https://example.com/photo.jpg", "mask": "https://example.com/mask.png", "prompt": "blue sky"}'
```

### OpenAI compatible

```bash
curl -s -X POST "http://localhost:8000/v1/images/edits" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"image": "https://example.com/photo.jpg", "model": "person"}'

curl "http://localhost:8000/v1/models" -H "Authorization: Bearer $API_KEY"
```

## All available parameters

| Parameter | Type / accepted values | Example | Endpoints |
|---|---|---|---|
| `image` | URL, raw base64, data URI or local path | `https://example.com/photo.jpg` | all image endpoints |
| `image_url` | string or `{url: "..."}` | `{"url": "https://example.com/photo.jpg"}` | all image endpoints |
| `image_b64` / `b64_json` | base64 string | `/9j/4AAQSk...` | all image endpoints |
| `mask` | URL or base64 (white = affected area) | `https://example.com/mask.png` | erase, fill, inpaint |
| `model_type` | `person` / `object` / `graphics` / `standard` | `person` | `/api/mask` |
| `model` | same as above | `person` | `/v1/images/edits` |
| `prompt` | string | `blue sky` | fill, uncrop, generations |
| `target_w` / `target_h` | integer 64–2048 (default 768) | `1024` | uncrop |
| `response_format` | `url` / `b64_json` / `raw` / `redirect` | `url` | all endpoints |
| `format` (query) | `url` / `b64_json` / `raw` / `redirect` | `?format=raw` | all endpoints |
| `redirect` (query) | `1` = 302 to final file | `?redirect=1` | all endpoints |
| `Authorization` | `Bearer <API_KEY>` | `Bearer sk-pr-...` | all API endpoints |

The dashboard **API Docs** tab shows every parameter plus generated cURL for URL, base64 and multipart modes; the **Playground** runs every model and returns the download link.

---

## How the persistence works

| File | Location (in container) | Purpose |
|---|---|---|
| `anon_token.json` | `/app/data/anon_token.json` | Firebase anonymous UID + refresh token (survives redeploy) |
| `app.db` | `/app/data/app.db` | API keys (hashed), admin key hash, settings, logs |
| `ADMIN_KEY.txt` | `/app/data/ADMIN_KEY.txt` | Generated admin key (only when `ADMIN_KEY` not set) |
| `flask_secret` | `/app/data/flask_secret` | Session signing key (dashboard login survives restarts) |
| `outputs/` | `/app/data/outputs` | Only used in `local` output mode (empty in default tmpfiles mode) |

The app never writes mutable state to the image filesystem. Updating the container image keeps `/app/data` intact. If the anonymous token expires it auto-refreshes; you can also force-refresh in **Settings**.

Output storage is controlled in **Settings → Output / media storage**:
- `tmpfiles` (default): results are streamed to `https://tmpfiles.org/api/v1/upload` with `expire=172800` (48h); the response contains the final direct download URL plus a share URL. No image files are written to the VPS.
- `local`: results are written to `/app/data/outputs` and served at `/files/<name>`.

**Cleanup** (Settings → Danger zone, or Logs tab): delete all generated files, all logs, or both. Log rows older than **10 days** are automatically removed hourly (`log_retention_days` is editable in Settings).

---

## Project layout

```
app.py                  Flask app: API + admin + dashboard routes
templates/
  dashboard.html        Dashboard UI
  login.html            Admin login
requirements.txt
Dockerfile
docker-compose.yml
.env.example
stress_test.py          Legacy stress test (points to :8908)
PROTOCOL.md             Reverse-engineering protocol notes
docs/KB.md              Cross-project methodology
```

## Security notes

- API keys and admin key are stored as SHA-256 hashes; only a prefix is shown in the UI.
- Set `FLASK_SECRET` in production so sessions stay valid across restarts.
- The upstream Photoroom API calls use the anonymous token; no personal account is required.
