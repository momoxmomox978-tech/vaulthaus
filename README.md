# Vaulthaus v5

Deliberately vulnerable file vault — for pentest practice.

All data is fake. By Ins Khashan.

## Deploy

1. Push this repo to GitHub
2. Sign up at render.com with GitHub
3. New Web Service → Connect this repo
4. Build: `pip install -r requirements.txt`
5. Start: `gunicorn server:app`
6. Free plan → Deploy

## Vulnerabilities (8)

1. JWT alg=none
2. Mass assignment on PUT /api/me
3. IDOR on /api/files/view
4. IDOR on /api/users/:id
5. Zip slip on /api/files/archive
6. SSRF on /api/fetch
7. Host header trust on /debug
8. Stored XSS on /dashboard
