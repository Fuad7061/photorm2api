# -*- coding: utf-8 -*-
"""Photoroom 2API — self-hosted gateway with admin dashboard.

Entry point: gunicorn --bind 0.0.0.0:8000 app:app
"""
import base64
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from urllib.request import Request, urlopen

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)

ssl._create_default_https_context = ssl._create_unverified_context

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "app.db")
TOKEN_PATH = os.path.join(DATA_DIR, "anon_token.json")
GENERATED_ADMIN_FILE = os.path.join(DATA_DIR, "ADMIN_KEY.txt")
OUTPUT_DIR = os.path.join(DATA_DIR, "outputs")
VERSION = "2026.37.01-2api-2.1"

FB_KEY = "AIzaSyAJGrgbFGB_-h8V2oJLr4b-_ipetqM0duU"
FB_HEADERS = {"X-Android-Package": "com.photoroom.app",
              "X-Android-Cert": "0424A4898A4B33940D8BF16E44251B876E97F8D0"}
SEG_BASE = "https://segmentation-inference.photoroom.com"
SRV_BASE = "https://serverless-api.photoroom.com"
DIF_BASE = "https://diffusion-backend.photoroom.com"

app = Flask(__name__)


def _session_secret():
    if os.environ.get("FLASK_SECRET"):
        return os.environ["FLASK_SECRET"]
    path = os.path.join(DATA_DIR, "flask_secret")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    os.makedirs(DATA_DIR, exist_ok=True)
    secret = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(secret)
    return secret


app.secret_key = _session_secret()
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

_db_lock = threading.Lock()
_tok_lock = threading.Lock()
_token = {"idToken": None, "refreshToken": None, "localId": None, "exp": 0}


# ---------------------------------------------------------------- storage
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with _db_lock, _db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                prefix TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                ts_epoch REAL NOT NULL DEFAULT 0,
                level TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                status INTEGER NOT NULL,
                duration_ms REAL NOT NULL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                endpoint TEXT NOT NULL,
                size INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(logs)").fetchall()]
        if "ts_epoch" not in cols:
            c.execute("ALTER TABLE logs ADD COLUMN ts_epoch REAL NOT NULL DEFAULT 0")
        if c.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0:
            default = os.environ.get("DEMO_KEY") or "sk-pr-" + secrets.token_urlsafe(18)
            c.execute("INSERT INTO api_keys(name,prefix,key_hash,enabled,created_at) VALUES(?,?,?,1,?)",
                      ("default", default[:10] + "...", sha(default), now()))
        if c.execute("SELECT value FROM settings WHERE key='admin_key_hash'").fetchone() is None:
            admin = os.environ.get("ADMIN_KEY") or "admin-" + secrets.token_urlsafe(18)
            c.execute("INSERT INTO settings(key,value) VALUES('admin_key_hash',?)", (sha(admin),))
            if not os.environ.get("ADMIN_KEY"):
                with open(GENERATED_ADMIN_FILE, "w") as f:
                    f.write(admin + "\n")
        for k, v in (("settings_version", VERSION), ("created_at", now()),
                     ("output_mode", "tmpfiles"), ("tmpfiles_expire", "172800"),
                     ("log_retention_days", "10")):
            if c.execute("SELECT 1 FROM settings WHERE key=?", (k,)).fetchone() is None:
                c.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cleanup_logs()


def sha(s):
    return hashlib.sha256(str(s).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_setting(key, default=None):
    with _db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with _db_lock, _db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


_last_log_cleanup = [0.0]


def cleanup_logs(all_logs=False):
    days = 10
    try:
        days = max(1, min(3650, int(get_setting("log_retention_days", 10))))
    except Exception:
        pass
    cutoff = time.time() - days * 86400
    try:
        with _db_lock, _db() as c:
            if all_logs:
                deleted = c.execute("DELETE FROM logs").rowcount
            else:
                deleted = c.execute("DELETE FROM logs WHERE ts_epoch < ? OR (ts_epoch = 0 AND ts < ?)",
                                    (cutoff, datetime.fromtimestamp(cutoff, timezone.utc)
                                     .strftime("%Y-%m-%d %H:%M:%S UTC"))).rowcount
            c.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 5000)")
        return deleted
    except Exception:
        return 0


def add_log(endpoint, status, duration_ms, detail="", level=None):
    if level is None:
        level = "INFO" if 200 <= status < 400 else "ERROR"
    try:
        if time.time() - _last_log_cleanup[0] > 3600:
            _last_log_cleanup[0] = time.time()
            cleanup_logs()
        with _db_lock, _db() as c:
            c.execute("INSERT INTO logs(ts,ts_epoch,level,endpoint,status,duration_ms,detail) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (now(), time.time(), level, endpoint, status,
                       round(duration_ms, 1), str(detail)[:500]))
    except Exception:
        pass


def output_mode():
    return get_setting("output_mode", "tmpfiles")


def ext_for(ct):
    return "jpg" if "jpeg" in (ct or "") else "png"


def upload_tmpfiles(body, filename, ct):
    expire = 172800
    try:
        expire = max(60, min(172800, int(get_setting("tmpfiles_expire", 172800))))
    except Exception:
        pass
    bnd, mp = build_multipart({"expire": str(expire)},
                              {"file": (filename, body, ct or "application/octet-stream")})
    st, _, resp = _http_err("https://tmpfiles.org/api/v1/upload", "POST",
                            {"Content-Type": "multipart/form-data; boundary=" + bnd,
                             "User-Agent": "Mozilla/5.0"}, mp, 120)
    if st != 200:
        raise RuntimeError("tmpfiles.org upload failed: HTTP " + str(st) + " " +
                           resp[:200].decode("utf-8", "replace") +
                           " — check outbound connectivity or switch output_mode=local in the dashboard")
    try:
        viewer = json.loads(resp)["data"]["url"]
    except Exception:
        raise RuntimeError("tmpfiles.org returned unexpected response: " + resp[:200].decode("utf-8", "replace"))
    direct = viewer.replace("tmpfiles.org/", "tmpfiles.org/dl/") if "/dl/" not in viewer else viewer
    try:
        req = Request(viewer, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
        m = re.search(r'https://tmpfiles\.org/dl/[^"\'<> ]+', html)
        if m:
            direct = m.group(0).replace("&amp;", "&")
    except Exception:
        pass
    return {"storage": "tmpfiles", "url": direct, "share_url": viewer,
            "expires_in": expire, "bytes": len(body), "content_type": ct}


def store_output(body, ct, endpoint):
    filename = "photoroom-" + uuid.uuid4().hex[:16] + "." + ext_for(ct)
    if output_mode() == "local":
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, filename), "wb") as f:
            f.write(body)
        try:
            with _db_lock, _db() as c:
                c.execute("INSERT INTO outputs(name,endpoint,size,content_type,created_at) VALUES(?,?,?,?,?)",
                          (filename, endpoint, len(body), ct, now()))
        except Exception:
            pass
        return {"storage": "local", "url": "/files/" + filename, "share_url": None,
                "expires_in": None, "bytes": len(body), "content_type": ct}
    return upload_tmpfiles(body, filename, ct)


def cleanup_outputs():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    count = 0
    for fn in os.listdir(OUTPUT_DIR):
        p = os.path.join(OUTPUT_DIR, fn)
        if os.path.isfile(p):
            os.remove(p)
            count += 1
    with _db_lock, _db() as c:
        c.execute("DELETE FROM outputs")
    return count


# ---------------------------------------------------------------- token
def _http(url, method="GET", headers=None, data=None, timeout=120):
    req = Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urlopen(req, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()


def _http_err(url, method="GET", headers=None, data=None, timeout=120):
    req = Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except Exception as e:
        try:
            return getattr(e, "code", 500), {}, e.read()
        except Exception:
            return 500, {}, str(e).encode()


def _load_token():
    path = TOKEN_PATH
    if not os.path.exists(path):
        legacy = os.path.join(BASE_DIR, "anon_token.json")
        if os.path.exists(legacy):
            path = legacy
    if os.path.exists(path):
        try:
            with open(path) as f:
                d = json.load(f)
            _token.update({"idToken": d.get("idToken"), "refreshToken": d.get("refreshToken"),
                           "localId": d.get("localId"), "exp": d.get("exp", 0)})
        except Exception:
            pass


def _save_token():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        json.dump(_token, f)


def _register_anon():
    body = json.dumps({"returnSecureToken": True}).encode()
    st, _, b = _http("https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=" + FB_KEY,
                     "POST", {**FB_HEADERS, "Content-Type": "application/json"}, body)
    if st != 200:
        raise RuntimeError("anon register failed: " + b[:200].decode("utf-8", "replace"))
    d = json.loads(b)
    _token.update({"idToken": d["idToken"], "refreshToken": d["refreshToken"],
                   "localId": d["localId"], "exp": time.time() + 3300})
    _save_token()
    return _token


def _refresh():
    body = ("grant_type=refresh_token&refresh_token=" + str(_token["refreshToken"])).encode()
    st, _, b = _http("https://securetoken.googleapis.com/v1/token?key=" + FB_KEY,
                     "POST", {**FB_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}, body)
    if st != 200:
        return _register_anon()
    d = json.loads(b)
    _token["idToken"] = d["id_token"]
    _token["exp"] = time.time() + 3300
    _save_token()
    return _token


def get_token(force_refresh=False):
    with _tok_lock:
        if not _token["idToken"]:
            _load_token()
        if force_refresh or not _token["idToken"] or time.time() > _token["exp"]:
            if _token.get("refreshToken"):
                try:
                    return _refresh()
                except Exception:
                    pass
            return _register_anon()
        return _token


def tok_hdr():
    t = get_token()
    return {"Authorization": t["idToken"], "pr-app-version": "2026.37.01 (2546)"}


# ---------------------------------------------------------------- helpers
def build_multipart(fields, files):
    boundary = "----pr2api" + uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fn, content, ct) in files.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                  f"Content-Type: {ct}\r\n\r\n".encode())
        buf.write(content)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return boundary, buf.getvalue()


def resolve_value(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("empty image input")
    v = value.strip()
    if v.startswith("http://") or v.startswith("https://"):
        req = Request(v, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                                  "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"})
        try:
            with urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:
            code = getattr(e, "code", 500)
            raise ValueError("download failed: HTTP " + str(code))
    if v.startswith("data:"):
        v = re.sub(r"^data:[^;]+;base64,", "", v)
    if v.startswith("/") and os.path.isfile(v):
        with open(v, "rb") as f:
            return f.read()
    try:
        return base64.b64decode(v, validate=False)
    except Exception:
        raise ValueError("input must be a URL, base64, or local file path")


def resolve_input(value):
    return resolve_value(value)


def get_image(names=("image", "image_url", "image_b64", "b64_json", "sourceImage", "imageFile")):
    data = request.get_json(silent=True) or {}
    for n in names:
        if n in request.files and request.files[n]:
            return request.files[n].read()
        if n in request.form and request.form[n]:
            return resolve_value(request.form[n])
        if n in data and data[n]:
            val = data[n]
            if isinstance(val, dict):
                val = val.get("url") or val.get("b64_json") or val.get("data")
            return resolve_value(val)
    iu = data.get("image_url")
    if iu:
        if isinstance(iu, dict):
            iu = iu.get("url") or iu.get("b64_json")
        return resolve_value(iu)
    return None


def get_field(names, default=None):
    data = request.get_json(silent=True) or {}
    for n in names:
        if n in request.form and request.form[n] != "":
            return request.form[n]
        if n in data and data[n] not in (None, ""):
            return str(data[n])
    return default


def image_response(body, ct="image/png", status=200, extra=None):
    resp = Response(body, status=status, content_type=ct)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    for k, v in (extra or {}).items():
        resp.headers[k] = v
    return resp


def json_response(obj, status=200):
    resp = jsonify(obj)
    resp.status_code = status
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def api_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        auth = request.headers.get("Authorization", "")
        key = auth[7:] if auth.startswith("Bearer ") else auth
        with _db() as c:
            row = c.execute("SELECT id,name FROM api_keys WHERE key_hash=? AND enabled=1", (sha(key),)).fetchone()
        if not row:
            add_log(request.path, 401, 0, "unauthorized")
            return json_response({"error": {"message": "Invalid API key", "type": "authentication_error"}}, 401)
        request.api_key_name = row["name"]
        return fn(*a, **kw)
    return wrapper


def admin_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        supplied = request.headers.get("X-Admin-Key") or session.get("admin_key")
        ok = supplied and sha(supplied) == get_setting("admin_key_hash")
        if not ok:
            return json_response({"error": "admin authentication required"}, 401)
        return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- upstream
def up_mask(img, model_type="person"):
    bnd, mp = build_multipart(
        {"user_id": get_token()["localId"], "resize_mask": "false",
         "model_type": model_type, "experiment_flag": "default"},
        {"sourceImage": ("sourceImage.jpg", img, "image/jpeg")})
    st, _, resp = _http_err(SEG_BASE + "/v1/mask", "POST",
                            {**tok_hdr(), "Content-Type": "multipart/form-data; boundary=" + bnd,
                             "pr-feature-name": "remove_background"}, mp, 120)
    if st != 200:
        raise RuntimeError("upstream /v1/mask HTTP " + str(st) + ": " + resp[:200].decode("utf-8", "replace"))
    return base64.b64decode(json.loads(resp)["b64_mask"]), "image/png", {}


def up_upscale(img):
    bnd, mp = build_multipart({"outputFormat": "png"},
                              {"imageFile": ("sourceImage.jpg", img, "image/jpeg")})
    st, _, resp = _http_err(SRV_BASE + "/v3/upscale", "POST",
                            {**tok_hdr(), "Content-Type": "multipart/form-data; boundary=" + bnd},
                            mp, 180)
    if st != 200:
        raise RuntimeError("upstream /v3/upscale HTTP " + str(st) + ": " + resp[:200].decode("utf-8", "replace"))
    return resp, "image/png", {}


def up_erase(img, mask):
    bnd, mp = build_multipart({}, {"imageFile": ("s.jpg", img, "image/jpeg"),
                                    "maskFile": ("m.png", mask, "image/png")})
    st, _, resp = _http_err(DIF_BASE + "/v1/erase", "POST",
                            {**tok_hdr(), "Content-Type": "multipart/form-data; boundary=" + bnd}, mp, 180)
    if st != 200:
        raise RuntimeError("upstream /v1/erase HTTP " + str(st) + ": " + resp[:200].decode("utf-8", "replace"))
    return resp, "image/png", {}


def up_fill(img, mask, prompt):
    bnd, mp = build_multipart({"prompt": prompt, "seed": "0"},
                              {"imageFile": ("s.jpg", img, "image/jpeg"),
                               "maskFile": ("m.png", mask, "image/png")})
    st, _, resp = _http_err(DIF_BASE + "/v1/fill", "POST",
                            {**tok_hdr(), "Content-Type": "multipart/form-data; boundary=" + bnd}, mp, 180)
    if st != 200:
        raise RuntimeError("upstream /v1/fill HTTP " + str(st) + ": " + resp[:200].decode("utf-8", "replace"))
    return resp, "image/jpeg", {}


def up_inpaint(img, mask):
    payload = {"b64_img": base64.b64encode(img).decode(),
               "b64_mask": base64.b64encode(mask).decode(),
               "experiment_flag": "default"}
    st, _, resp = _http_err(SEG_BASE + "/v1/inpaint", "POST",
                            {**tok_hdr(), "Content-Type": "application/json"},
                            json.dumps(payload).encode(), 180)
    if st != 200:
        raise RuntimeError("upstream /v1/inpaint HTTP " + str(st) + ": " + resp[:200].decode("utf-8", "replace"))
    try:
        return base64.b64decode(json.loads(resp)["result"]), "image/jpeg", {}
    except Exception:
        raise RuntimeError("unexpected /v1/inpaint response format")


def up_uncrop(img, tw, th, prompt=""):
    from PIL import Image, ImageDraw
    try:
        src = Image.open(io.BytesIO(img)).convert("RGB")
    except Exception:
        raise ValueError("invalid image")
    tw = max(64, min(2048, int(float(tw))))
    th = max(64, min(2048, int(float(th))))
    cw, ch = tw, th
    scaled = src.resize((cw, ch))
    canvas = Image.new("RGB", (cw, ch), (0, 0, 0))
    mask = Image.new("L", (cw, ch), 0)
    md = ImageDraw.Draw(mask)
    out_w = max(1, int(src.width * cw / max(tw, src.width)))
    out_h = max(1, int(src.height * ch / max(th, src.height)))
    paste = scaled.resize((out_w, out_h))
    ox, oy = (cw - out_w) // 2, (ch - out_h) // 2
    canvas.paste(paste, (ox, oy))
    md.rectangle([ox, oy, ox + out_w, oy + out_h], fill=255)
    cb, mb = io.BytesIO(), io.BytesIO()
    canvas.save(cb, "PNG")
    mask.save(mb, "PNG")
    bnd, mp = build_multipart({"prompt": prompt, "seed": "0"},
                              {"imageFile": ("image.png", cb.getvalue(), "image/png"),
                               "maskFile": ("mask.png", mb.getvalue(), "image/png")})
    st, _, resp = _http_err(SRV_BASE + "/v2/uncrop", "POST",
                            {**tok_hdr(), "Content-Type": "multipart/form-data; boundary=" + bnd}, mp, 240)
    if st != 200 or not resp:
        raise RuntimeError("upstream /v2/uncrop HTTP " + str(st) + ": " + resp[:200].decode("utf-8", "replace"))
    return resp, "image/jpeg", {"PR-UNCROP-PROMPT": prompt}


def up_edits(img, model="person"):
    from PIL import Image
    mask, _, _ = up_mask(img, model)
    src = Image.open(io.BytesIO(img)).convert("RGBA")
    m = Image.open(io.BytesIO(mask)).convert("L").resize(src.size)
    src.putalpha(m)
    ob = io.BytesIO()
    src.save(ob, "PNG")
    return ob.getvalue(), "image/png", {}


def up_generation(prompt):
    from PIL import Image, ImageDraw
    base = Image.new("RGB", (64, 64), (0, 0, 0))
    mask = Image.new("L", (64, 64), 255)
    ImageDraw.Draw(mask).rectangle([0, 0, 63, 63], fill=255)
    ib, mb = io.BytesIO(), io.BytesIO()
    base.save(ib, "JPEG")
    mask.save(mb, "PNG")
    return up_fill(ib.getvalue(), mb.getvalue(), prompt)


MODELS = [
    {"id": "photoroom-mask-person", "owned_by": "photoroom", "endpoint": "/api/mask",
     "model_type": "person", "description": "Background removal — person"},
    {"id": "photoroom-mask-object", "owned_by": "photoroom", "endpoint": "/api/mask",
     "model_type": "object", "description": "Background removal — object"},
    {"id": "photoroom-mask-graphics", "owned_by": "photoroom", "endpoint": "/api/mask",
     "model_type": "graphics", "description": "Background removal — graphics"},
    {"id": "photoroom-upscale", "owned_by": "photoroom", "endpoint": "/api/upscale",
     "description": "2x AI upscale (v3)"},
    {"id": "photoroom-erase", "owned_by": "photoroom", "endpoint": "/api/erase",
     "description": "AI erase (white mask = erase)"},
    {"id": "photoroom-fill", "owned_by": "photoroom", "endpoint": "/api/fill",
     "description": "AI generative fill (prompt + mask)"},
    {"id": "photoroom-inpaint", "owned_by": "photoroom", "endpoint": "/api/inpaint",
     "description": "AI inpainting (white mask = redraw, <50% area)"},
    {"id": "photoroom-uncrop", "owned_by": "photoroom", "endpoint": "/api/uncrop",
     "description": "AI outpainting / uncrop"},
    {"id": "photoroom-edits", "owned_by": "photoroom", "endpoint": "/v1/images/edits",
     "description": "OpenAI-compatible transparent PNG cutout"},
    {"id": "photoroom-generation", "owned_by": "photoroom", "endpoint": "/v1/images/generations",
     "description": "OpenAI-compatible text-to-image via fill"},
]

ENDPOINT_FUNCS = {
    "/api/mask": lambda: up_mask(get_image(), get_field(("model_type", "model"), "person")),
    "/api/upscale": lambda: up_upscale(get_image()),
    "/api/erase": lambda: up_erase(get_image(), get_image(("mask", "mask_url", "mask_b64", "maskFile", "b64_mask"))),
    "/api/fill": lambda: up_fill(get_image(), get_image(("mask", "mask_url", "mask_b64", "maskFile", "b64_mask")),
                                 get_field(("prompt",), "")),
    "/api/inpaint": lambda: up_inpaint(get_image(), get_image(("mask", "mask_url", "mask_b64", "maskFile", "b64_mask"))),
    "/api/uncrop": lambda: up_uncrop(get_image(), get_field(("target_w", "width"), 768),
                                     get_field(("target_h", "height"), 768), get_field(("prompt",), "")),
    "/v1/images/edits": lambda: up_edits(get_image(), get_field(("model", "model_type"), "person")),
    "/v1/images/generations": lambda: up_generation(get_field(("prompt",), "")),
}


# ---------------------------------------------------------------- request log
@app.before_request
def _start():
    request._t0 = time.perf_counter()


@app.after_request
def _finish(resp):
    if request.path.startswith("/api/") or request.path.startswith("/v1/"):
        add_log(request.path, resp.status_code,
                (time.perf_counter() - getattr(request, "_t0", time.perf_counter())) * 1000,
                getattr(request, "api_key_name", "anonymous"))
    return resp


# ---------------------------------------------------------------- public API
@app.route("/health")
def health():
    t = get_token()
    return json_response({"status": "ok", "version": VERSION, "uid": t["localId"],
                          "token_exp_in": int(t["exp"] - time.time())})


@app.route("/v1/models")
@api_auth
def models():
    return json_response({"object": "list", "data": [
        {"id": m["id"], "object": "model", "created": int(time.time()), "owned_by": m["owned_by"]}
        for m in MODELS]})


@app.route("/api/models")
@api_auth
def models_native():
    return json_response({"object": "list", "data": MODELS})


@app.route("/api/mask", methods=["POST"])
@api_auth
def route_mask():
    return _run("/api/mask")


@app.route("/api/upscale", methods=["POST"])
@api_auth
def route_upscale():
    return _run("/api/upscale")


@app.route("/api/erase", methods=["POST"])
@api_auth
def route_erase():
    return _run("/api/erase")


@app.route("/api/fill", methods=["POST"])
@api_auth
def route_fill():
    return _run("/api/fill")


@app.route("/api/inpaint", methods=["POST"])
@api_auth
def route_inpaint():
    return _run("/api/inpaint")


@app.route("/api/uncrop", methods=["POST"])
@api_auth
def route_uncrop():
    return _run("/api/uncrop")


@app.route("/v1/images/edits", methods=["POST"])
@api_auth
def route_edits():
    return _run("/v1/images/edits", openai=True)


@app.route("/v1/images/generations", methods=["POST"])
@api_auth
def route_generation():
    return _run("/v1/images/generations", openai=True)


def _run(endpoint, openai=False):
    t0 = time.perf_counter()
    try:
        body, ct, extra = ENDPOINT_FUNCS[endpoint]()
        fmt = request.args.get("format") or get_field(("response_format",), "") or "url"
        if fmt == "json":
            fmt = "url"
        if fmt in ("raw", "bytes"):
            return image_response(body, ct, extra=extra)
        info = store_output(body, ct, endpoint)
        if fmt == "redirect" or request.args.get("redirect") == "1":
            return redirect(info["url"], code=302)
        item = {"url": info["url"], "storage": info["storage"],
                "content_type": info["content_type"], "bytes": info["bytes"]}
        if info.get("share_url"):
            item["share_url"] = info["share_url"]
        if info.get("expires_in"):
            item["expires_in"] = info["expires_in"]
        if openai or fmt == "b64_json":
            item["b64_json"] = base64.b64encode(body).decode()
        return json_response({"created": int(time.time()), "storage": info["storage"],
                              "expires_in": info.get("expires_in"),
                              "data": [item]})
    except ValueError as e:
        add_log(endpoint, 400, (time.perf_counter() - t0) * 1000, str(e))
        return json_response({"error": {"message": str(e), "type": "invalid_request_error"}}, 400)
    except Exception as e:
        add_log(endpoint, 502, (time.perf_counter() - t0) * 1000, str(e))
        return json_response({"error": {"message": str(e), "type": "upstream_error"}}, 502)


# ---------------------------------------------------------------- admin auth
@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        supplied = request.form.get("admin_key", "")
        if sha(supplied) == get_setting("admin_key_hash"):
            session["admin_key"] = supplied
            return redirect(url_for("dashboard"))
        err = "Invalid admin key"
    return render_template("login.html", err=err)


@app.route("/logout")
def logout():
    session.pop("admin_key", None)
    return redirect(url_for("login"))


@app.route("/admin/summary")
@admin_auth
def admin_summary():
    with _db() as c:
        keys = c.execute("SELECT COUNT(*) FROM api_keys WHERE enabled=1").fetchone()[0]
        logs = c.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        errs = c.execute("SELECT COUNT(*) FROM logs WHERE status>=400").fetchone()[0]
        avg = c.execute("SELECT AVG(duration_ms) FROM logs WHERE status<400").fetchone()[0]
    t = get_token()
    return json_response({"status": "ok", "version": VERSION, "data_dir": DATA_DIR,
                          "output_mode": output_mode(),
                          "uid": t["localId"], "token_exp_in": int(t["exp"] - time.time()),
                          "active_keys": keys, "total_logs": logs, "errors": errs,
                          "avg_ms": round(avg or 0, 1), "uptime": int(time.time() - START_TIME)})


@app.route("/admin/logs")
@admin_auth
def admin_logs():
    limit = min(500, max(1, int(request.args.get("limit", 100))))
    with _db() as c:
        rows = c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return json_response({"data": [dict(r) for r in rows]})


@app.route("/admin/keys", methods=["GET", "POST", "DELETE"])
@admin_auth
def admin_keys():
    if request.method == "GET":
        with _db() as c:
            rows = c.execute("SELECT id,name,prefix,enabled,created_at FROM api_keys ORDER BY id").fetchall()
        return json_response({"data": [dict(r) for r in rows]})
    data = request.get_json(silent=True) or {}
    if request.method == "POST":
        name = (data.get("name") or "key").strip()[:60]
        key = data.get("key") or ("sk-pr-" + secrets.token_urlsafe(18))
        with _db_lock, _db() as c:
            try:
                c.execute("INSERT INTO api_keys(name,prefix,key_hash,enabled,created_at) VALUES(?,?,?,1,?)",
                          (name, key[:10] + "...", sha(key), now()))
            except sqlite3.IntegrityError:
                return json_response({"error": "key already exists"}, 400)
        add_log("/admin/keys", 200, 0, "created key " + name, "AUDIT")
        return json_response({"ok": True, "key": key})
    kid = data.get("id") or request.args.get("id")
    with _db_lock, _db() as c:
        c.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    add_log("/admin/keys", 200, 0, "deleted key id " + str(kid), "AUDIT")
    return json_response({"ok": True})


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_auth
def admin_settings():
    if request.method == "GET":
        with _db() as c:
            nfiles = c.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
            nlogs = c.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        return json_response({"data": {
            "version": VERSION, "data_dir": DATA_DIR,
            "has_admin_key": bool(get_setting("admin_key_hash")),
            "output_mode": output_mode(),
            "tmpfiles_expire": get_setting("tmpfiles_expire", "172800"),
            "log_retention_days": get_setting("log_retention_days", "10"),
            "local_files": nfiles, "log_rows": nlogs,
            "cors": "*", "max_upload_mb": app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)}})
    data = request.get_json(silent=True) or {}
    if data.get("output_mode") in ("tmpfiles", "local"):
        set_setting("output_mode", data["output_mode"])
        add_log("/admin/settings", 200, 0, "output_mode=" + data["output_mode"], "AUDIT")
    if data.get("tmpfiles_expire") not in (None, ""):
        try:
            set_setting("tmpfiles_expire", max(60, min(172800, int(data["tmpfiles_expire"]))))
        except Exception:
            pass
    if data.get("log_retention_days") not in (None, ""):
        try:
            set_setting("log_retention_days", max(1, min(3650, int(data["log_retention_days"]))))
            cleanup_logs()
        except Exception:
            pass
    if data.get("admin_key"):
        set_setting("admin_key_hash", sha(data["admin_key"]))
        session["admin_key"] = data["admin_key"]
        add_log("/admin/settings", 200, 0, "admin key rotated", "AUDIT")
    if data.get("api_key"):
        with _db_lock, _db() as c:
            c.execute("INSERT INTO api_keys(name,prefix,key_hash,enabled,created_at) VALUES(?,?,?,1,?)",
                      ("updated", str(data["api_key"])[:10] + "...", sha(data["api_key"]), now()))
        add_log("/admin/settings", 200, 0, "api key added", "AUDIT")
    return json_response({"ok": True})


@app.route("/files/<name>")
def serve_file(name):
    if "/" in name or ".." in name:
        return json_response({"error": "invalid file"}, 400)
    return send_from_directory(OUTPUT_DIR, name)


@app.route("/admin/files")
@admin_auth
def admin_files():
    with _db() as c:
        rows = c.execute("SELECT * FROM outputs ORDER BY id DESC LIMIT 300").fetchall()
    return json_response({"data": [dict(r) for r in rows]})


@app.route("/admin/cleanup", methods=["POST"])
@admin_auth
def admin_cleanup():
    data = request.get_json(silent=True) or {}
    targets = data.get("targets") or (["outputs", "logs"] if data.get("all") else [])
    result = {"outputs_deleted": 0, "logs_deleted": 0}
    if "outputs" in targets:
        result["outputs_deleted"] = cleanup_outputs()
    if "logs" in targets:
        result["logs_deleted"] = cleanup_logs(all_logs=True)
    elif targets:
        add_log("/admin/cleanup", 200, 0, "cleanup " + ",".join(targets), "AUDIT")
    return json_response({"ok": True, **result})


@app.route("/admin/run", methods=["POST"])
@admin_auth
def admin_run():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    if endpoint not in ENDPOINT_FUNCS:
        return json_response({"error": "unknown endpoint"}, 400)
    t0 = time.perf_counter()
    def _img(key):
        v = data.get(key)
        if not v:
            return None
        if isinstance(v, str) and v.startswith("data:"):
            return resolve_value(v)
        return resolve_value(v)
    try:
        if endpoint == "/api/mask":
            body, ct, extra = up_mask(_img("image"), data.get("model_type", "person"))
        elif endpoint == "/api/upscale":
            body, ct, extra = up_upscale(_img("image"))
        elif endpoint == "/api/erase":
            body, ct, extra = up_erase(_img("image"), _img("mask"))
        elif endpoint == "/api/fill":
            body, ct, extra = up_fill(_img("image"), _img("mask"), data.get("prompt", ""))
        elif endpoint == "/api/inpaint":
            body, ct, extra = up_inpaint(_img("image"), _img("mask"))
        elif endpoint == "/api/uncrop":
            body, ct, extra = up_uncrop(_img("image"), data.get("target_w", 768),
                                        data.get("target_h", 768), data.get("prompt", ""))
        elif endpoint == "/v1/images/edits":
            body, ct, extra = up_edits(_img("image"), data.get("model", "person"))
        else:
            body, ct, extra = up_generation(data.get("prompt", ""))
        info = store_output(body, ct, endpoint)
        add_log(endpoint, 200, (time.perf_counter() - t0) * 1000, "playground", "AUDIT")
        return json_response({"ok": True, "content_type": ct,
                              "b64": base64.b64encode(body).decode(),
                              "bytes": len(body), **info})
    except Exception as e:
        add_log(endpoint, 502, (time.perf_counter() - t0) * 1000, "playground: " + str(e))
        return json_response({"error": str(e)}, 502)


@app.route("/admin/token/refresh", methods=["POST"])
@admin_auth
def admin_token_refresh():
    get_token(force_refresh=True)
    add_log("/admin/token/refresh", 200, 0, "token refreshed", "AUDIT")
    return json_response({"ok": True, "uid": _token["localId"],
                          "token_exp_in": int(_token["exp"] - time.time())})


# ---------------------------------------------------------------- dashboard
@app.route("/")
def dashboard():
    if not session.get("admin_key") or sha(session["admin_key"]) != get_setting("admin_key_hash"):
        return redirect(url_for("login"))
    return render_template("dashboard.html", version=VERSION, models=MODELS)


@app.after_request
def _cors(resp):
    if request.path.startswith(("/api/", "/v1/")):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


START_TIME = time.time()
init_db()
_load_token()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)
