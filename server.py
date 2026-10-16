# language: python, file: server.py
# Vaulthaus v5 — Flask edition
# by Ins Khashan
# deploy: Render / Koyeb / Railway / PythonAnywhere
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
from functools import wraps
from datetime import datetime

from flask import Flask, request, jsonify, Response, g

# =========================================================
#  CONFIG
# =========================================================
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
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            full_name TEXT,
            department TEXT,
            salt BLOB NOT NULL,
            pw_hash BLOB NOT NULL,
            role TEXT DEFAULT 'user',
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            content TEXT NOT NULL,
            mime TEXT DEFAULT 'text/plain',
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            unread INTEGER DEFAULT 1,
            ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            actor TEXT,
            action TEXT,
            ip TEXT
        );
    """)
    conn.commit()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] > 0:
        conn.close()
        return

    seed_users = [
        ("admin",         "admin@vaulthaus.internal",         "System Administrator",  "IT",       "WelcomeAdmin!2024", "admin"),
        ("ops",           "ops@vaulthaus.internal",           "Operations Team",       "IT",       "OpsTeam#2024",      "admin"),
        ("elena.rojas",   "e.rojas@northbridge-corp.com",     "Elena Rojas",           "Finance",  "elena123",          "user"),
        ("kenji.tanaka",  "k.tanaka@fujikawa-labs.jp",        "Kenji Tanaka",          "R&D",      "kenji456",          "user"),
        ("amara.diallo",  "a.diallo@sahel-ventures.com",      "Amara Diallo",          "Sales",    "amara789",          "user"),
        ("sophie.martin", "s.martin@lyonnaise.group",         "Sophie Martin",         "HR",       "sophie321",         "user"),
        ("diego.silva",   "d.silva@atlantico.pt",             "Diego Silva",           "DevOps",   "diego654",          "user"),
        ("marcus.wright", "m.wright@harbor-holdings.co.uk",   "Marcus Wright",         "Legal",    "marcus987",         "user"),
        ("priya.sharma",  "p.sharma@deccan-systems.in",       "Priya Sharma",          "Product",  "priya456",          "user"),
    ]
    ids = {}
    for u, e, full, dept, pw, role in seed_users:
        salt, h = hash_password(pw)
        cur.execute("INSERT INTO users(username,email,full_name,department,salt,pw_hash,role,created) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (u, e, full, dept, salt, h, role, time.time()))
        ids[u] = cur.lastrowid

    seed_files = [
        (ids["admin"],        "flag.txt",         "LAB{vh5_alg_none_and_mass_assignment_chain_2026}"),
        (ids["admin"],        "internal-notes.txt", "Q3 rotation: migrate to v5.1\ncontact: admin@vaulthaus.internal"),
        (ids["admin"],        "ssh-config.txt",     "# FAKE\nHost vh-prod\n  HostName 198.51.100.10\n  User deploy"),
        (ids["ops"],          "flag.txt",         "LAB{alg_none_for_jwt_is_still_broken}"),
        (ids["ops"],          "runbook.txt",      "Deploy: docker compose up -d\nRollback: git revert HEAD"),
        (ids["elena.rojas"],  "flag.txt",         "LAB{zip_slip_extract_anywhere_you_like}"),
        (ids["elena.rojas"],  "quarterly.txt",    "Q2 revenue: 4.2M EUR, up 18% YoY\ncosts: 1.1M"),
        (ids["elena.rojas"],  "clients.csv",      "id,name,email,country\n1,Jean Dupont,jean@example.com,FR\n2,Maria Rossi,maria@example.it,IT"),
        (ids["kenji.tanaka"], "flag.txt",         "LAB{ssrf_to_internal_metadata}"),
        (ids["kenji.tanaka"], "api-keys.txt",     "OPENAI=sk-fake-0000\nAWS=AKIAFAKE0000NOTREAL\nSTRIPE=sk_test_fake_0000"),
        (ids["amara.diallo"], "customers.csv",    "id,name,email,country\n1,Amina,amina@example.ma,MA\n2,Ismail,ismail@example.eg,EG"),
        (ids["sophie.martin"],"hr-notes.txt",     "reviews due 2026-11-30\nbudget: pending CFO"),
        (ids["sophie.martin"],"payroll.txt",      "Oct 2026: headcount 42, gross 287K EUR"),
        (ids["diego.silva"],  "passwords.txt",    "vpn: vpnuser/FakePass!23\nrouter: admin/admin123\nwifi: GuestNet2024"),
        (ids["marcus.wright"],"audit-2026.txt",   "Compliance audit: 3 minor, 0 critical\nremediation: 2026-12-31"),
        (ids["priya.sharma"], "roadmap.txt",      "Q4: SSO + mobile beta\n2027 Q1: API v2 GA"),
    ]
    for oid, name, content in seed_files:
        cur.execute("INSERT INTO files(owner_id,name,content,created) VALUES(?,?,?,?)",
                    (oid, name, content, time.time()))

    seed_messages = [
        (ids["admin"], "noreply@vaulthaus.internal", "Security alert",
         "12 failed login attempts from 198.51.100.42."),
        (ids["admin"], "billing@northbridge-corp.com", "Invoice #4421",
         "EUR 12,400 due in 7 days."),
        (ids["elena.rojas"], "hr@lyonnaise.group", "Review scheduling",
         "Book your annual review before 2026-11-15."),
        (ids["kenji.tanaka"], "research@fujikawa-labs.jp", "Experiment ready",
         "Results for EXP-1045 ready."),
        (ids["diego.silva"], "oncall@vaulthaus.internal", "On-call rotation",
         "You are on-call next week."),
        (ids["sophie.martin"], "benefits@provider.com", "Benefits enrollment",
         "Window opens Monday."),
    ]
    for oid, sender, subj, body in seed_messages:
        cur.execute("INSERT INTO messages(owner_id,sender,subject,body,ts) VALUES(?,?,?,?,?)",
                    (oid, sender, subj, body, time.time()))

    for actor, action, ip in [
        ("admin", "login_success", "203.0.113.14"),
        ("kenji.tanaka", "login_failed", "198.51.100.42"),
        ("diego.silva", "file_upload", "203.0.113.30"),
    ]:
        cur.execute("INSERT INTO events(ts,actor,action,ip) VALUES(?,?,?,?)",
                    (time.time(), actor, action, ip))

    conn.commit()
    conn.close()

# =========================================================
#  AUTH HELPERS
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
        g.uid = uid
        g.role = role
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
    b.append(now)
    return True

def new_csrf():
    t = secrets.token_urlsafe(24)
    CSRF_TOKENS[t] = time.time() + 3600
    return t

def check_csrf(t):
    if not t: return False
    exp = CSRF_TOKENS.get(t)
    return bool(exp and exp >= time.time())

# =========================================================
#  MIDDLEWARE
# =========================================================
@app.before_request
def _rate_and_log():
    ip = request.remote_addr or "0.0.0.0"
    if not rate_limit(ip):
        return jsonify({"error":"slow down"}), 429

@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Server"] = "Vaulthaus/5.0"
    return resp

# =========================================================
#  ROUTES
# =========================================================
@app.route("/")
def index():
    return ("Vaulthaus v5 — by Ins Khashan\n"
            "secure file vault, enterprise edition\n"
            "endpoints: /api/register /api/login /api/me /api/users "
            "/api/files /api/files/view /api/files/upload /api/files/archive "
            "/api/inbox /api/events /api/fetch /dashboard /debug\n")

@app.route("/api/register", methods=["POST"])
def register():
    d = request.get_json(silent=True) or {}
    u = d.get("username",""); e = d.get("email",""); pw = d.get("password","")
    full = d.get("full_name",""); dept = d.get("department","")
    import re
    if not re.match(r"^[A-Za-z0-9_.\-]{3,32}$", u):
        return jsonify({"error":"username"}), 400
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
        return jsonify({"error":"email"}), 400
    if len(pw) < 6:
        return jsonify({"error":"password too short"}), 400
    c = db(); cur = c.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (u,))
    if cur.fetchone():
        return jsonify({"error":"taken"}), 409
    salt, hsh = hash_password(pw)
    cur.execute("INSERT INTO users(username,email,full_name,department,salt,pw_hash,role,created) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (u, e, full, dept, salt, hsh, "user", time.time()))
    c.commit()
    return jsonify({"id": cur.lastrowid}), 201

@app.route("/api/login", methods=["POST"])
def login():
    d = request.get_json(silent=True) or {}
    username = d.get("username","")
    uid = verify(username, d.get("password",""))
    if uid is None:
        return jsonify({"error":"invalid"}), 401
    u = get_user(uid)
    sid = secrets.token_urlsafe(32)
    SESSION_TOKENS[sid] = {"uid": uid, "role": u[5], "expires": time.time() + SESSION_TTL}
    jwt = jwt_sign({"uid": uid, "role": u[5], "exp": int(time.time()) + JWT_TTL})
    csrf = new_csrf()
    resp = jsonify({"ok": True, "access_token": jwt, "token_type": "Bearer"})
    resp.set_cookie("sid", sid, httponly=True, samesite="Strict", path="/")
    resp.set_cookie("csrf", csrf, httponly=True, samesite="Strict", path="/")
    return resp

@app.route("/api/me", methods=["GET"])
@require_auth
def me_get():
    u = get_user(g.uid)
    return jsonify({
        "id": u[0], "username": u[1], "email": u[2],
        "full_name": u[3], "department": u[4], "role": u[5],
        "csrf": request.cookies.get("csrf", ""),
    })

@app.route("/api/me", methods=["PUT"])
@require_auth
def me_put():
    d = request.get_json(silent=True) or {}
    c = db(); cur = c.cursor()
    for field in ["email", "full_name", "department", "role"]:
        if field in d:
            cur.execute(f"UPDATE users SET {field}=? WHERE id=?", (d[field], g.uid))
    c.commit()
    u = get_user(g.uid)
    return jsonify({
        "id": u[0], "username": u[1], "email": u[2],
        "full_name": u[3], "department": u[4], "role": u[5],
    })

@app.route("/api/users", methods=["GET"])
@app.route("/api/users/<int:target>", methods=["GET"])
@require_auth
def users(target=None):
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
def files_list():
    sort = request.args.get("sort", "name")
    allowed = {"name","id","created","-name","-id","-created"}
    if sort not in allowed: sort = "name"
    cur = db().cursor()
    cur.execute(f"SELECT id,name,created FROM files WHERE owner_id=? ORDER BY {sort}", (g.uid,))
    return jsonify([{"id": r[0], "name": r[1], "created": r[2]} for r in cur.fetchall()])

@app.route("/api/files/view", methods=["GET"])
@require_auth
def files_view():
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
def files_upload():
    import re
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
def files_archive():
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
def inbox():
    cur = db().cursor()
    cur.execute("SELECT id,sender,subject,ts,unread FROM messages WHERE owner_id=? ORDER BY ts DESC",
                (g.uid,))
    return jsonify([{"id": r[0], "sender": r[1], "subject": r[2],
                     "ts": r[3], "unread": bool(r[4])} for r in cur.fetchall()])

@app.route("/api/events", methods=["GET"])
@require_auth
def events():
    cur = db().cursor()
    cur.execute("SELECT ts,actor,action,ip FROM events ORDER BY ts DESC LIMIT 100")
    return jsonify([{"ts": r[0], "actor": r[1], "action": r[2], "ip": r[3]}
                    for r in cur.fetchall()])

@app.route("/dashboard", methods=["GET"])
@require_auth
def dashboard():
    cur = db().cursor()
    cur.execute("SELECT actor,action,ip,ts FROM events ORDER BY ts DESC LIMIT 50")
    rows = "".join(f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td></tr>"
                   for r in cur.fetchall())
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Vaulthaus</title>
<style>body{{font-family:monospace;background:#0b0c10;color:#c5c6c7;padding:2rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #333;padding:.4rem}}</style>
</head><body><h1>Vaulthaus — audit log</h1>
<table><thead><tr><th>actor</th><th>action</th><th>ip</th><th>ts</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""
    return Response(html, mimetype="text/html")

@app.route("/api/fetch", methods=["GET"])
@require_auth
def fetch():
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error":"missing url"}), 400
    low = url.lower()
    if "localhost" in low or "127.0.0.1" in low or "169.254" in low or "file://" in low:
        return jsonify({"error":"blocked"}), 403
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Vaulthaus/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = r.read(4096).decode("utf-8", errors="replace")
        return jsonify({"url": url, "preview": data})
    except Exception as e:
        return jsonify({"error": f"fetch: {e}"}), 502

@app.route("/debug", methods=["GET"])
@require_auth
def debug():
    host = request.headers.get("Host", "").split(":")[0].lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return jsonify({"error":"not local"}), 403
    cur = db().cursor()
    cur.execute("SELECT COUNT(*) FROM users"); users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM files"); files = cur.fetchone()[0]
    return jsonify({"pid": os.getpid(), "cwd": os.getcwd(),
                    "python": "3.11", "users": users, "files": files,
                    "db": DB_PATH, "csrf_active": len(CSRF_TOKENS)})

# =========================================================
#  BOOT
# =========================================================
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
