# language: python, file: server.py
# Vaulthaus v6 — HTML UI + API
# by Ins Khashan
# data: all fake

import os
import io
import json
import time
import hmac
import base64
import hashlib
import secrets
import zipfile
import sqlite3
import urllib.request
import re
from functools import wraps

from flask import Flask, request, jsonify, Response, g, render_template_string, redirect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.environ.get("VAULTHAUS_DB", os.path.join(BASE_DIR, "vaulthaus.db"))
FILES_DIR = os.path.join(BASE_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

SESSION_TTL = 3600
JWT_TTL     = 3600
JWT_SECRET  = b"vaulthaus_internal_signing_key_2024"
RATE_WINDOW = 60
RATE_MAX    = 240
MAX_BODY    = 1024 * 1024

CSRF_TOKENS    = {}
RATE_LIMIT     = {}
SESSION_TOKENS = {}

app = Flask(__name__)

# =========================================================
#  DB
# =========================================================
def db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

def hash_password(pw, salt=None):
    if salt is None:
        salt = os.urandom(16)
    return salt, hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL, full_name TEXT, department TEXT,
            salt BLOB NOT NULL, pw_hash BLOB NOT NULL,
            role TEXT DEFAULT 'user', created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
            name TEXT NOT NULL, content TEXT NOT NULL,
            mime TEXT DEFAULT 'text/plain', created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
            sender TEXT NOT NULL, subject TEXT NOT NULL,
            body TEXT NOT NULL, unread INTEGER DEFAULT 1, ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, ts REAL NOT NULL,
            actor TEXT, action TEXT, ip TEXT);
    """)
    conn.commit()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] > 0:
        conn.close()
        return
    seed_users = [
        ("admin", "admin@vaulthaus.internal", "System Administrator", "IT", "WelcomeAdmin!2024", "admin"),
        ("ops", "ops@vaulthaus.internal", "Operations Team", "IT", "OpsTeam#2024", "admin"),
        ("elena.rojas", "e.rojas@northbridge-corp.com", "Elena Rojas", "Finance", "elena123", "user"),
        ("kenji.tanaka", "k.tanaka@fujikawa-labs.jp", "Kenji Tanaka", "R&D", "kenji456", "user"),
        ("amara.diallo", "a.diallo@sahel-ventures.com", "Amara Diallo", "Sales", "amara789", "user"),
        ("sophie.martin", "s.martin@lyonnaise.group", "Sophie Martin", "HR", "sophie321", "user"),
        ("diego.silva", "d.silva@atlantico.pt", "Diego Silva", "DevOps", "diego654", "user"),
        ("marcus.wright", "m.wright@harbor-holdings.co.uk", "Marcus Wright", "Legal", "marcus987", "user"),
        ("priya.sharma", "p.sharma@deccan-systems.in", "Priya Sharma", "Product", "priya456", "user"),
    ]
    ids = {}
    for u, e, full, dept, pw, role in seed_users:
        salt, h = hash_password(pw)
        cur.execute("INSERT INTO users(username,email,full_name,department,salt,pw_hash,role,created) VALUES(?,?,?,?,?,?,?,?)",
                    (u, e, full, dept, salt, h, role, time.time()))
        ids[u] = cur.lastrowid
    seed_files = [
        (ids["admin"], "flag.txt", "LAB{vh6_alg_none_and_mass_assignment_chain_2026}"),
        (ids["admin"], "internal-notes.txt", "Q3 rotation: migrate to v6.1\ncontact: admin@vaulthaus.internal"),
        (ids["admin"], "ssh-config.txt", "# FAKE\nHost vh-prod\n  HostName 198.51.100.10\n  User deploy"),
        (ids["ops"], "flag.txt", "LAB{alg_none_for_jwt_is_still_broken}"),
        (ids["ops"], "runbook.txt", "Deploy: docker compose up -d\nRollback: git revert HEAD"),
        (ids["elena.rojas"], "flag.txt", "LAB{zip_slip_extract_anywhere_you_like}"),
        (ids["elena.rojas"], "quarterly.txt", "Q2 revenue: 4.2M EUR, up 18% YoY\ncosts: 1.1M"),
        (ids["elena.rojas"], "clients.csv", "id,name,email,country\n1,Jean Dupont,jean@example.com,FR\n2,Maria Rossi,maria@example.it,IT"),
        (ids["kenji.tanaka"], "flag.txt", "LAB{ssrf_to_internal_metadata}"),
        (ids["kenji.tanaka"], "api-keys.txt", "OPENAI=sk-fake-0000\nAWS=AKIAFAKE0000NOTREAL\nSTRIPE=sk_test_fake_0000"),
        (ids["amara.diallo"], "customers.csv", "id,name,email,country\n1,Amina,amina@example.ma,MA\n2,Ismail,ismail@example.eg,EG"),
        (ids["sophie.martin"], "hr-notes.txt", "reviews due 2026-11-30\nbudget: pending CFO"),
        (ids["sophie.martin"], "payroll.txt", "Oct 2026: headcount 42, gross 287K EUR"),
        (ids["diego.silva"], "passwords.txt", "vpn: vpnuser/FakePass!23\nrouter: admin/admin123\nwifi: GuestNet2024"),
        (ids["marcus.wright"], "audit-2026.txt", "Compliance audit: 3 minor, 0 critical\nremediation: 2026-12-31"),
        (ids["priya.sharma"], "roadmap.txt", "Q4: SSO + mobile beta\n2027 Q1: API v2 GA"),
    ]
    for oid, name, content in seed_files:
        cur.execute("INSERT INTO files(owner_id,name,content,created) VALUES(?,?,?,?)",
                    (oid, name, content, time.time()))
    seed_msgs = [
        (ids["admin"], "noreply@vaulthaus.internal", "Security alert", "12 failed login attempts from 198.51.100.42."),
        (ids["admin"], "billing@northbridge-corp.com", "Invoice #4421", "EUR 12,400 due in 7 days."),
        (ids["elena.rojas"], "hr@lyonnaise.group", "Review scheduling", "Book your annual review before 2026-11-15."),
        (ids["kenji.tanaka"], "research@fujikawa-labs.jp", "Experiment ready", "Results for EXP-1045 ready."),
        (ids["diego.silva"], "oncall@vaulthaus.internal", "On-call rotation", "You are on-call next week."),
        (ids["sophie.martin"], "benefits@provider.com", "Benefits enrollment", "Window opens Monday."),
    ]
    for oid, s, sub, body in seed_msgs:
        cur.execute("INSERT INTO messages(owner_id,sender,subject,body,ts) VALUES(?,?,?,?,?)",
                    (oid, s, sub, body, time.time()))
    for actor, action, ip in [("admin","login_success","203.0.113.14"),
                               ("kenji.tanaka","login_failed","198.51.100.42"),
                               ("diego.silva","file_upload","203.0.113.30")]:
        cur.execute("INSERT INTO events(ts,actor,action,ip) VALUES(?,?,?,?)",
                    (time.time(), actor, action, ip))
    conn.commit()
    conn.close()

# =========================================================
#  AUTH
# =========================================================
def verify(username, password):
    cur = db().cursor()
    cur.execute("SELECT id,salt,pw_hash FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    if not row:
        hash_password(password, os.urandom(16))
        return None
    _, salt, stored = row
    return row[0] if hmac.compare_digest(hash_password(password, salt)[1], stored) else None

def b64e(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def b64d(s):
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode())

def jwt_sign(payload):
    h = b64e(json.dumps({"alg":"HS256","typ":"JWT"}, separators=(",",":")).encode())
    p = b64e(json.dumps(payload, separators=(",",":")).encode())
    sig = b64e(hmac.new(JWT_SECRET, f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"

def jwt_verify(token):
    try:
        parts = token.split(".")
        if len(parts) != 3: return None
        h, p, s = parts
        header = json.loads(b64d(h))
        alg = header.get("alg","HS256").lower()
        msg = f"{h}.{p}".encode()
        if alg == "none":
            payload = json.loads(b64d(p))
            return payload if payload.get("exp",0) >= time.time() else None
        expected = b64e(hmac.new(JWT_SECRET, msg, hashlib.sha256).digest())
        if not hmac.compare_digest(expected, s): return None
        payload = json.loads(b64d(p))
        return payload if payload.get("exp",0) >= time.time() else None
    except Exception:
        return None

def current_user():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = jwt_verify(auth[7:])
        if payload:
            return payload.get("uid"), payload.get("role")
    sid = request.cookies.get("sid")
    if sid:
        s = SESSION_TOKENS.get(sid)
        if s and s["expires"] >= time.time():
            return s["uid"], s["role"]
    return None, None

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        uid, role = current_user()
        if not uid:
            return jsonify({"error":"no session"}), 401
        g.uid = uid; g.role = role
        return f(*args, **kwargs)
    return wrapper

def get_user(uid):
    cur = db().cursor()
    cur.execute("SELECT id,username,email,full_name,department,role FROM users WHERE id=?", (uid,))
    return cur.fetchone()

def rate_limit(ip):
    now = time.time()
    b = RATE_LIMIT.setdefault(ip, [])
    b[:] = [t for t in b if now - t < RATE_WINDOW]
    if len(b) >= RATE_MAX: return False
    b.append(now); return True

def new_csrf():
    t = secrets.token_urlsafe(24)
    CSRF_TOKENS[t] = time.time() + 3600
    return t

def check_csrf(t):
    if not t: return False
    exp = CSRF_TOKENS.get(t)
    return bool(exp and exp >= time.time())

@app.before_request
def _rate():
    ip = request.remote_addr or "0.0.0.0"
    if request.path.startswith("/api/") and not rate_limit(ip):
        return jsonify({"error":"slow down"}), 429

# =========================================================
#  API (same as v5)
# =========================================================
@app.route("/api/register", methods=["POST"])
def api_register():
    d = request.get_json(silent=True) or {}
    u = d.get("username",""); e = d.get("email",""); pw = d.get("password","")
    full = d.get("full_name",""); dept = d.get("department","")
    if not re.match(r"^[A-Za-z0-9_.\-]{3,32}$", u):
        return jsonify({"error":"invalid username"}), 400
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
        return jsonify({"error":"invalid email"}), 400
    if len(pw) < 6:
        return jsonify({"error":"password too short"}), 400
    c = db(); cur = c.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (u,))
    if cur.fetchone():
        return jsonify({"error":"username taken"}), 409
    salt, hsh = hash_password(pw)
    cur.execute("INSERT INTO users(username,email,full_name,department,salt,pw_hash,role,created) VALUES(?,?,?,?,?,?,?,?)",
                (u, e, full, dept, salt, hsh, "user", time.time()))
    c.commit()
    return jsonify({"id": cur.lastrowid}), 201

@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json(silent=True) or {}
    uid = verify(d.get("username",""), d.get("password",""))
    if uid is None:
        return jsonify({"error":"invalid"}), 401
    u = get_user(uid)
    sid = secrets.token_urlsafe(32)
    SESSION_TOKENS[sid] = {"uid": uid, "role": u[5], "expires": time.time() + SESSION_TTL}
    jwt = jwt_sign({"uid": uid, "role": u[5], "exp": int(time.time()) + JWT_TTL})
    csrf = new_csrf()
    resp = jsonify({"ok": True, "access_token": jwt, "token_type": "Bearer",
                    "user": {"id": u[0], "username": u[1], "email": u[2],
                             "full_name": u[3], "department": u[4], "role": u[5]},
                    "csrf": csrf})
    resp.set_cookie("sid", sid, httponly=True, samesite="Strict", path="/")
    resp.set_cookie("csrf", csrf, httponly=True, samesite="Strict", path="/")
    return resp

@app.route("/api/me", methods=["GET"])
@require_auth
def api_me_get():
    u = get_user(g.uid)
    return jsonify({"id": u[0], "username": u[1], "email": u[2],
                    "full_name": u[3], "department": u[4], "role": u[5],
                    "csrf": request.cookies.get("csrf", "")})

@app.route("/api/me", methods=["PUT"])
@require_auth
def api_me_put():
    d = request.get_json(silent=True) or {}
    c = db(); cur = c.cursor()
    for field in ["email", "full_name", "department", "role"]:
        if field in d:
            cur.execute(f"UPDATE users SET {field}=? WHERE id=?", (d[field], g.uid))
    c.commit()
    u = get_user(g.uid)
    return jsonify({"id": u[0], "username": u[1], "email": u[2],
                    "full_name": u[3], "department": u[4], "role": u[5]})

@app.route("/api/users", methods=["GET"])
@app.route("/api/users/<int:target>", methods=["GET"])
@require_auth
def api_users(target=None):
    cur = db().cursor()
    if target is None:
        cur.execute("SELECT id,username,full_name,department,role FROM users")
        return jsonify([{"id": r[0], "username": r[1], "full_name": r[2],
                         "department": r[3], "role": r[4]} for r in cur.fetchall()])
    u = get_user(target)
    if not u:
        return jsonify({"error":"no user"}), 404
    return jsonify({"id": u[0], "username": u[1], "email": u[2],
                    "full_name": u[3], "department": u[4], "role": u[5]})

@app.route("/api/files", methods=["GET"])
@require_auth
def api_files_list():
    sort = request.args.get("sort", "name")
    allowed = {"name","id","created","-name","-id","-created"}
    if sort not in allowed: sort = "name"
    cur = db().cursor()
    cur.execute(f"SELECT id,name,created FROM files WHERE owner_id=? ORDER BY {sort}", (g.uid,))
    return jsonify([{"id": r[0], "name": r[1], "created": r[2]} for r in cur.fetchall()])

@app.route("/api/files/view", methods=["GET"])
@require_auth
def api_files_view():
    fid = request.args.get("id", "")
    if not fid.isdigit():
        return jsonify({"error":"bad id"}), 400
    cur = db().cursor()
    cur.execute("SELECT id,name,content,owner_id,mime FROM files WHERE id=?", (int(fid),))
    row = cur.fetchone()
    if not row:
        return jsonify({"error":"no file"}), 404
    return jsonify({"id": row[0], "name": row[1], "content": row[2],
                    "owner_id": row[3], "mime": row[4]})

@app.route("/api/files/upload", methods=["POST"])
@require_auth
def api_files_upload():
    csrf = request.headers.get("X-CSRF-Token") or request.cookies.get("csrf")
    if not check_csrf(csrf):
        return jsonify({"error":"csrf"}), 403
    d = request.get_json(silent=True) or {}
    name = d.get("name",""); content = d.get("content","")
    if not re.match(r"^[A-Za-z0-9_\-. ]{1,64}$", name):
        return jsonify({"error":"bad name"}), 400
    if len(content) > MAX_BODY:
        return jsonify({"error":"too big"}), 413
    c = db(); cur = c.cursor()
    cur.execute("INSERT INTO files(owner_id,name,content,created) VALUES(?,?,?,?)",
                (g.uid, name, content, time.time()))
    c.commit()
    return jsonify({"id": cur.lastrowid}), 201

@app.route("/api/files/archive", methods=["POST"])
@require_auth
def api_files_archive():
    csrf = request.headers.get("X-CSRF-Token") or request.cookies.get("csrf")
    if not check_csrf(csrf):
        return jsonify({"error":"csrf"}), 403
    d = request.get_json(silent=True) or {}
    try:
        raw = base64.b64decode(d.get("archive",""))
    except Exception:
        return jsonify({"error":"bad archive"}), 400
    dest = os.path.join(FILES_DIR, str(g.uid))
    os.makedirs(dest, exist_ok=True)
    extracted = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        for name in zf.namelist():
            target = os.path.join(dest, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(zf.read(name))
            extracted.append(name)
    except Exception as e:
        return jsonify({"error": f"archive: {e}"}), 400
    return jsonify({"extracted": extracted})

@app.route("/api/inbox", methods=["GET"])
@require_auth
def api_inbox():
    cur = db().cursor()
    cur.execute("SELECT id,sender,subject,body,ts,unread FROM messages WHERE owner_id=? ORDER BY ts DESC", (g.uid,))
    return jsonify([{"id": r[0], "sender": r[1], "subject": r[2], "body": r[3],
                     "ts": r[4], "unread": bool(r[5])} for r in cur.fetchall()])

@app.route("/api/events", methods=["GET"])
@require_auth
def api_events():
    cur = db().cursor()
    cur.execute("SELECT ts,actor,action,ip FROM events ORDER BY ts DESC LIMIT 100")
    return jsonify([{"ts": r[0], "actor": r[1], "action": r[2], "ip": r[3]} for r in cur.fetchall()])

@app.route("/api/fetch", methods=["GET"])
@require_auth
def api_fetch():
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error":"missing url"}), 400
    low = url.lower()
    if "localhost" in low or "127.0.0.1" in low or "169.254" in low or "file://" in low:
        return jsonify({"error":"blocked"}), 403
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Vaulthaus/6.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = r.read(4096).decode("utf-8", errors="replace")
        return jsonify({"url": url, "preview": data})
    except Exception as e:
        return jsonify({"error": f"fetch: {e}"}), 502

@app.route("/debug", methods=["GET"])
@require_auth
def api_debug():
    host = request.headers.get("Host", "").split(":")[0].lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return jsonify({"error":"not local"}), 403
    cur = db().cursor()
    cur.execute("SELECT COUNT(*) FROM users"); users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM files"); files = cur.fetchone()[0]
    return jsonify({"pid": os.getpid(), "cwd": os.getcwd(),
                    "python": "3.13", "users": users, "files": files,
                    "db": DB_PATH, "csrf_active": len(CSRF_TOKENS)})

# =========================================================
#  HTML UI
# =========================================================
CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--surface:#161b22;--surface2:#21262d;--border:#30363d;
--text:#c9d1d9;--muted:#8b949e;--accent:#3fb950;--link:#58a6ff;--danger:#f85149}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;line-height:1.5}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.container{max-width:1200px;margin:0 auto;padding:0 20px}
header{border-bottom:1px solid var(--border);padding:12px 0;background:var(--surface)}
header .inner{display:flex;align-items:center;justify-content:space-between}
.logo{font-size:16px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px}
.logo svg{width:20px;height:20px;fill:var(--accent)}
nav a{margin-left:20px;color:var(--muted);font-size:13px}
nav a:hover{color:var(--text);text-decoration:none}
.hero{text-align:center;padding:80px 20px 60px}
.hero h1{font-size:44px;font-weight:600;letter-spacing:-1px;margin-bottom:12px}
.hero h1 span{color:var(--accent)}
.hero p{color:var(--muted);font-size:16px;max-width:560px;margin:0 auto 8px}
.hero .meta{color:#484f58;font-size:12px;margin-top:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:24px}
.card h2{font-size:18px;margin-bottom:16px;font-weight:600}
.card h3{font-size:13px;color:var(--muted);margin-bottom:12px;font-weight:500;text-transform:uppercase;letter-spacing:.5px}
.auth{max-width:400px;margin:0 auto;padding:0 20px 60px}
.tabs{display:flex;margin-bottom:20px;border-bottom:1px solid var(--border)}
.tab{flex:1;text-align:center;padding:12px;cursor:pointer;color:var(--muted);font-size:14px;border-bottom:2px solid transparent;background:none;border:none;font-family:inherit}
.tab.active{color:var(--text);border-bottom-color:var(--accent)}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px;margin-top:14px}
input,textarea{width:100%;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px;font-family:inherit}
input:focus,textarea:focus{outline:none;border-color:var(--link)}
button{width:100%;padding:10px;background:var(--accent);color:#fff;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;margin-top:20px;font-family:inherit}
button:hover{background:#4ac95e}
.btn-sec{background:var(--surface2);color:var(--text)}
.btn-sec:hover{background:#2d333b}
.grid{display:grid;grid-template-columns:240px 1fr;gap:24px;padding:24px 0;min-height:calc(100vh - 200px)}
.sidebar{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px;height:fit-content;position:sticky;top:20px}
.sidebar .user{padding-bottom:16px;border-bottom:1px solid var(--border);margin-bottom:16px}
.sidebar .user .name{font-weight:600;font-size:14px;margin-bottom:2px}
.sidebar .user .role{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.sidebar a{display:block;padding:8px 12px;color:var(--muted);font-size:13px;border-radius:6px;margin-bottom:2px}
.sidebar a:hover{background:var(--surface2);color:var(--text);text-decoration:none}
.sidebar a.active{background:var(--surface2);color:var(--text)}
.file-row{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border);cursor:pointer}
.file-row:last-child{border-bottom:none}
.file-row:hover{background:var(--surface2)}
.file-row .name{flex:1;font-size:14px}
.file-row .name svg{width:14px;height:14px;fill:var(--muted);margin-right:8px;vertical-align:middle}
.file-row .meta{color:var(--muted);font-size:12px}
.tag{display:inline-block;padding:2px 8px;font-size:11px;border-radius:10px;background:var(--surface2);color:var(--muted)}
.tag.admin{background:#3d1d1d;color:#f85149}
.error,.ok{padding:10px 14px;border-radius:6px;font-size:13px;margin-bottom:16px}
.error{background:#3d1d1d;border:1px solid var(--danger);color:var(--danger)}
.ok{background:#1a2f1a;border:1px solid var(--accent);color:var(--accent)}
.empty{text-align:center;padding:40px 20px;color:var(--muted)}
footer{text-align:center;padding:40px 20px;color:#484f58;font-size:12px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:100;padding:20px}
.modal.show{display:flex}
.modal .box{background:var(--surface);border:1px solid var(--border);border-radius:8px;max-width:640px;width:100%;max-height:80vh;overflow:auto;padding:24px}
.modal pre{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:16px;font-family:ui-monospace,Menlo,monospace;font-size:13px;white-space:pre-wrap;word-break:break-word;color:var(--text)}
.modal .close{float:right;cursor:pointer;color:var(--muted);font-size:20px;line-height:1}
"""

LOGO_SVG = '<svg viewBox="0 0 16 16"><path d="M8 0a4 4 0 0 0-4 4v2H3a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1h-1V4a4 4 0 0 0-4-4zm2 6V4a2 2 0 0 0-4 0v2h4z"/></svg>'

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Vaulthaus</title><style>{css}</style></head><body>
<header><div class="container inner">
<div class="logo">{logo} <span>Vaulthaus</span></div>
{nav}
</div></header>
{content}
<footer>Vaulthaus v6 — Enterprise Secure Vault — by Ins Khashan</footer>
{scripts}</body></html>"""

def page(title, content, user=None, scripts=""):
    if user:
        nav = f'<nav><a href="/app">Files</a><a href="/inbox">Inbox</a><a href="/admin">Admin</a><a href="#" onclick="logout();return false">Logout</a></nav>'
    else:
        nav = '<nav><a href="/">Login</a><a href="/#register">Register</a></nav>'
    return BASE.format(title=title, css=CSS, logo=LOGO_SVG, nav=nav, content=content, scripts=scripts)

# =========================================================
#  HTML PAGES
# =========================================================
@app.route("/")
def page_index():
    content = """
<div class="hero">
<h1>Enterprise <span>Secure Vault</span></h1>
<p>Store, share, and manage sensitive files with confidence.</p>
<p class="meta">Trusted by Northbridge Corp, Fujikawa Labs, Sahel Ventures, Lyonnaise Group.</p>
</div>
<div class="auth">
<div class="card">
<div class="tabs">
<button class="tab active" onclick="showTab('login')">Sign in</button>
<button class="tab" onclick="showTab('register')">Create account</button>
</div>
<div id="msg"></div>
<div id="login-form">
<h3>Sign in to Vaulthaus</h3>
<label>Username</label><input id="login-user" autocomplete="username">
<label>Password</label><input id="login-pass" type="password" autocomplete="current-password">
<button onclick="doLogin()">Sign in</button>
</div>
<div id="register-form" style="display:none">
<h3>Create your account</h3>
<label>Username</label><input id="reg-user">
<label>Email</label><input id="reg-email" type="email">
<label>Full name</label><input id="reg-full">
<label>Department</label><input id="reg-dept">
<label>Password (min 6)</label><input id="reg-pass" type="password">
<button onclick="doRegister()">Create account</button>
</div>
</div>
</div>
"""
    scripts = """<script>
function showTab(t){
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('login-form').style.display = t==='login'?'block':'none';
  document.getElementById('register-form').style.display = t==='register'?'block':'none';
}
function setMsg(t, cls){document.getElementById('msg').innerHTML='<div class="'+cls+'">'+t+'</div>';}
async function doLogin(){
  const u=document.getElementById('login-user').value, p=document.getElementById('login-pass').value;
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
  const j=await r.json();
  if(r.ok){localStorage.setItem('jwt',j.access_token);localStorage.setItem('csrf',j.csrf);localStorage.setItem('user',JSON.stringify(j.user));location.href='/app';}
  else{setMsg(j.error||'Login failed','error');}
}
async function doRegister(){
  const d={username:document.getElementById('reg-user').value,email:document.getElementById('reg-email').value,
  full_name:document.getElementById('reg-full').value,department:document.getElementById('reg-dept').value,
  password:document.getElementById('reg-pass').value};
  const r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  const j=await r.json();
  if(r.ok){setMsg('Account created. You can sign in now.','ok');setTimeout(()=>{document.querySelector('.tab').click();},1200);}
  else{setMsg(j.error||'Registration failed','error');}
}
</script>"""
    return page("Home", content, scripts=scripts)

@app.route("/app")
def page_app():
    content = """
<div class="container">
<div class="grid">
<aside class="sidebar">
<div class="user">
<div class="name" id="sb-name">—</div>
<div class="role" id="sb-role">—</div>
</div>
<a href="/app" class="active">📁 Files</a>
<a href="/inbox">✉️ Inbox</a>
<a href="/admin">⚙️ Admin</a>
<a href="#" onclick="uploadPrompt();return false">⬆️ Upload</a>
</aside>
<div>
<div class="card">
<h2>Your files</h2>
<div id="files-list"><div class="empty">Loading...</div></div>
</div>
</div>
</div>
</div>
<div class="modal" id="modal" onclick="if(event.target===this)closeModal()">
<div class="box">
<span class="close" onclick="closeModal()">×</span>
<h2 id="modal-title">—</h2>
<pre id="modal-body">—</pre>
</div>
</div>
"""
    scripts = """<script>
const jwt=localStorage.getItem('jwt');
if(!jwt){location.href='/';}
const user=JSON.parse(localStorage.getItem('user')||'{}');
document.getElementById('sb-name').textContent=user.username||'—';
document.getElementById('sb-role').textContent=user.role||'—';
async function api(path,opts={}){
  opts.headers=opts.headers||{};
  opts.headers['Authorization']='Bearer '+jwt;
  return fetch(path,opts);
}
async function loadFiles(){
  const r=await api('/api/files');
  if(r.status===401){location.href='/';return;}
  const files=await r.json();
  const el=document.getElementById('files-list');
  if(!files.length){el.innerHTML='<div class="empty">No files yet. Click Upload to add one.</div>';return;}
  el.innerHTML=files.map(f=>'<div class="file-row" onclick="viewFile('+f.id+')"><div class="name">📄 '+f.name+'</div><div class="meta">#'+f.id+'</div></div>').join('');
}
async function viewFile(id){
  const r=await api('/api/files/view?id='+id);
  const j=await r.json();
  document.getElementById('modal-title').textContent=j.name||('File #'+id);
  document.getElementById('modal-body').textContent=j.content||JSON.stringify(j,null,2);
  document.getElementById('modal').classList.add('show');
}
function closeModal(){document.getElementById('modal').classList.remove('show');}
async function uploadPrompt(){
  const name=prompt('File name:');if(!name)return;
  const content=prompt('File content:');if(content===null)return;
  const csrf=localStorage.getItem('csrf')||'';
  const r=await api('/api/files/upload',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({name,content})});
  if(r.ok){loadFiles();}else{const j=await r.json();alert('Error: '+(j.error||r.status));}
}
function logout(){localStorage.clear();location.href='/';}
loadFiles();
</script>"""
    return page("Files", content, user=True, scripts=scripts)

@app.route("/inbox")
def page_inbox():
    content = """
<div class="container">
<div class="grid">
<aside class="sidebar">
<div class="user"><div class="name" id="sb-name">—</div><div class="role" id="sb-role">—</div></div>
<a href="/app">📁 Files</a>
<a href="/inbox" class="active">✉️ Inbox</a>
<a href="/admin">⚙️ Admin</a>
<a href="#" onclick="logout();return false">⏻ Logout</a>
</aside>
<div>
<div class="card"><h2>Inbox</h2><div id="inbox-list"><div class="empty">Loading...</div></div></div>
</div>
</div>
</div>
"""
    scripts = """<script>
const jwt=localStorage.getItem('jwt');if(!jwt)location.href='/';
const user=JSON.parse(localStorage.getItem('user')||'{}');
document.getElementById('sb-name').textContent=user.username||'—';
document.getElementById('sb-role').textContent=user.role||'—';
async function load(){
  const r=await fetch('/api/inbox',{headers:{'Authorization':'Bearer '+jwt}});
  const msgs=await r.json();
  const el=document.getElementById('inbox-list');
  if(!msgs.length){el.innerHTML='<div class="empty">No messages.</div>';return;}
  el.innerHTML=msgs.map(m=>'<div class="file-row" onclick="showMsg('+m.id+')"><div class="name">✉️ '+m.subject+'<div class="meta" style="margin-top:4px">'+m.sender+'</div></div></div>').join('');
  window._msgs=msgs;
}
function showMsg(id){
  const m=window._msgs.find(x=>x.id===id);
  if(!m)return;
  alert('From: '+m.sender+'\\nSubject: '+m.subject+'\\n\\n'+m.body);
}
function logout(){localStorage.clear();location.href='/';}
load();
</script>"""
    return page("Inbox", content, user=True, scripts=scripts)

@app.route("/admin")
def page_admin():
    content = """
<div class="container">
<div class="grid">
<aside class="sidebar">
<div class="user"><div class="name" id="sb-name">—</div><div class="role" id="sb-role">—</div></div>
<a href="/app">📁 Files</a>
<a href="/inbox">✉️ Inbox</a>
<a href="/admin" class="active">⚙️ Admin</a>
<a href="#" onclick="logout();return false">⏻ Logout</a>
</aside>
<div>
<div class="card" style="margin-bottom:16px">
<h2>Users</h2><div id="users-list"><div class="empty">Loading...</div></div>
</div>
<div class="card">
<h2>Audit log</h2><div id="events-list"><div class="empty">Loading...</div></div>
</div>
</div>
</div>
</div>
"""
    scripts = """<script>
const jwt=localStorage.getItem('jwt');if(!jwt)location.href='/';
const user=JSON.parse(localStorage.getItem('user')||'{}');
document.getElementById('sb-name').textContent=user.username||'—';
document.getElementById('sb-role').textContent=user.role||'—';
async function api(p){return fetch(p,{headers:{'Authorization':'Bearer '+jwt}});}
async function load(){
  const u=await (await api('/api/users')).json();
  const ue=document.getElementById('users-list');
  ue.innerHTML=u.map(x=>'<div class="file-row"><div class="name">'+x.username+' <span class="tag '+x.role+'">'+x.role+'</span><div class="meta">'+x.full_name+' — '+x.department+'</div></div></div>').join('');
  const e=await (await api('/api/events')).json();
  const ee=document.getElementById('events-list');
  ee.innerHTML=e.map(x=>'<div class="file-row"><div class="name">'+x.actor+' — '+x.action+'<div class="meta">'+x.ip+' · '+new Date(x.ts*1000).toLocaleString()+'</div></div></div>').join('');
}
function logout(){localStorage.clear();location.href='/';}
load();
</script>"""
    return page("Admin", content, user=True, scripts=scripts)

# =========================================================
#  BOOT
# =========================================================
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
