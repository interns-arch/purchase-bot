# Deploying ProcureHub to AWS EC2

Runbook for putting the **existing, verified** Docker stack on an Ubuntu EC2
instance. The architecture is unchanged:

```
Internet
  ↓
EC2 · Nginx :80/:443            (host)
  ↓
frontend :4009   backend :4008  (containers, bound to 127.0.0.1)
  ↓
Postgres 18 · db:5432           (container, not published at all)
```

Nothing about the application, the images, the volumes, the `db/Dockerfile`,
the IST timezone or the init hook changes. The only additions are an
**override file** for production port-binding
(`deploy/docker-compose.prod.yml`), a **host Nginx** config, and three
scripts.

> **Neon stays untouched** throughout, and remains the rollback target until
> you have finished Phase 12.

---

## What was verified locally before writing this

| Check | Result |
|---|---|
| Production override merges correctly | backend + frontend publish **one** port each, on `127.0.0.1`; `db` publishes **nothing** |
| Nginx config syntax (`nginx -t`) | passes |
| Frontend through Nginx | `GET /` → 200, app shell served |
| SPA deep link through Nginx | `GET /command-centre` → 200 (not 404) |
| API through Nginx | `POST /api/auth/login` reached FastAPI (401 = wrong password, i.e. routing works) |
| `/health` through Nginx | `{"status":"ok"}` |
| `VITE_API_BASE_URL=/` | browser calls the API **same-origin**; no IP baked into the bundle |
| Restored data in `pgdata18` | 38 tables, **54 vendors**, 21 customers, 28,022 parts, 111,712 inventory rows |

One real bug was found and fixed by this testing: Compose **merges** port
lists instead of replacing them, so the first override attempt tried to bind
the public `8000:8000` *and* the loopback mapping, and failed. The override
now uses `!override`. Without that, the EC2 deploy would have failed on the
first `up` — and if it had succeeded, port 8000 would have been public.

---

## Phase 1 — The EC2 instance

**Ubuntu 24.04 LTS** (`ami-*ubuntu-noble-24.04-amd64-server-*`), x86_64.

**Instance type — `t3.small` minimum, `t3.medium` recommended.**

Sized from what this stack actually uses, not a guess:

| Component | Memory |
|---|---|
| Postgres 18 (28k parts, 111k inventory rows) | ~250–400 MB |
| Backend (Python + FastAPI baseline) | ~150 MB |
| Backend peak per Vendor-Comparison/allocation request | +~55 MB |
| Frontend (nginx serving static files) | ~10 MB |
| Host Nginx + OS | ~200 MB |

`t3.small` (2 GiB) runs it, but the **frontend build** (`npm ci` + Vite) is
the spike — it wants ~1 GiB on its own. On a 2 GiB box either add swap
(below) or build the frontend image elsewhere. `t3.medium` (4 GiB) removes
the problem and leaves headroom for large imports.

```bash
# Recommended on t3.small: 2 GB swap so a build cannot OOM the box.
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**Storage — 30 GiB gp3** (the free-tier maximum, and the right size anyway):

| | |
|---|---|
| Docker images | ~1.25 GB (backend 751 MB + db 419 MB + frontend 75 MB) |
| Build cache + npm | ~1–2 GB |
| Database volume | ~0.5 GB today, grows with daily vendor stock |
| Uploads volume | grows with every imported file — the main consumer over time |
| Ubuntu + Docker | ~4 GB |

**Elastic IP** — allocate and associate one. A stopped/started instance
otherwise gets a new public IP, which breaks your bookmarks, the Security
Group notes and any future DNS record. (Note the frontend does *not* need
rebuilding when the IP changes, because the API base is same-origin — but a
stable address is still worth the two clicks.)

**Security Group — inbound only:**

| Port | Source | Why |
|---|---|---|
| 22 | **My IP only** | SSH. Not `0.0.0.0/0`. |
| 80 | `0.0.0.0/0` | Public site via Nginx |
| 443 | `0.0.0.0/0` | HTTPS, once a domain exists |

### The ports this application uses (for the Security Group)

| Service | Port | Opened in the Security Group? |
|---|---|---|
| Nginx (public entrance) | **80**, **443** | **Yes** |
| SSH | **22** | **Yes — your IP only** |
| Backend — FastAPI/uvicorn | **4008** | **No** — bound to `127.0.0.1`, reached at `http://<ip>/api/…` |
| Frontend — nginx serving the built UI | **4009** | **No** — bound to `127.0.0.1`, reached at `http://<ip>/` |
| PostgreSQL 18 | 5432 | **No** — not published at all; internal `db:5432` only |

The frontend and backend do run on separate ports, as instructed — 4008 and
4009 — they simply are not *public* ports. Nginx forwards `/api/…` to 4008
and everything else to 4009, so the browser only ever talks to port 80.

That choice buys three things: no CORS configuration to maintain, no public
IP compiled into the frontend bundle (so an IP change needs no rebuild), and
HTTPS later is a single `certbot` command instead of certificates on two
ports plus the mixed-content problem an HTTPS page hits when it calls an
HTTP API.

**Never open** 5432, 55432, 4008 or 4009. They are bound to `127.0.0.1` by
the override, so even a mistaken rule would not expose them — defence in
depth, not a substitute for it.

---

## Phase 2 — SSH from Windows

Windows refuses a key that is readable by other accounts; fix the ACL once:

```powershell
icacls "C:\path\to\procurehub-key.pem" /inheritance:r
icacls "C:\path\to\procurehub-key.pem" /grant:r "$env:USERNAME:(R)"
```

```powershell
ssh -i "C:\path\to\procurehub-key.pem" ubuntu@EC2_PUBLIC_IP
```

The `.pem` never leaves your machine and is never committed.

---

## Phase 3 — Install Docker

```bash
git clone https://github.com/<your-account>/<your-repo>.git procurehub
cd procurehub
chmod +x deploy/*.sh
./deploy/install-docker.sh
```

Log out and back in (group membership), then **verify**:

```bash
docker --version
docker compose version
docker run --rm hello-world      # must work without sudo
```

---

## Phase 4 — Clone the repository

Done in Phase 3. If the repo is private, use a deploy key or
`gh auth login`. Do not paste a personal access token into shell history.

---

## Phase 5 — Environment configuration

Two files, **neither committed**:

```bash
# 1. Compose/production settings
cp deploy/.env.production.example .env
nano .env          # set JWT_SECRET_KEY, POSTGRES_PASSWORD, CORS_ORIGINS
chmod 600 .env
```

```powershell
# 2. Application settings, copied from your machine (NOT via GitHub)
scp -i "C:\path\to\procurehub-key.pem" `
    D:\Downloads\ProcureHub\backend\.env `
    ubuntu@EC2_PUBLIC_IP:/home/ubuntu/procurehub/backend/.env
```

```bash
chmod 600 backend/.env
```

**Then remove the Neon URL from `backend/.env` on the server** so it cannot
be reached even by accident:

```bash
sed -i 's|^DATABASE_URL=|# DATABASE_URL (Neon) intentionally unset on EC2 -- Compose sets db:5432\n# DATABASE_URL=|' backend/.env
grep -n '^DATABASE_URL' backend/.env || echo "OK: no active DATABASE_URL in backend/.env"
```

Compose supplies `DATABASE_URL=postgresql://…@db:5432/procurehub` and
`environment:` beats `env_file:`, so the container database wins regardless —
this just removes the credential from the box entirely.

Integrations stay **off** (`WHATSAPP_ENABLED=false`, `GMAIL_ENABLED=false`,
`ENABLE_GOOGLE_SHEETS_SYNC=false`). Both this instance and Render would
otherwise poll the same Gmail inbox and message the same vendors.

---

## Phase 6 — Start Postgres only

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d db
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps db
```

**Verify before going further:**

```bash
C="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml"
$C exec db psql -U procurehub -d procurehub -c "show server_version;"   # 18.x
$C exec db psql -U procurehub -d procurehub -c "show timezone;"         # Asia/Kolkata
$C exec db sh -c 'echo $PGDATA'                                         # /var/lib/postgresql/18/docker
docker volume ls | grep pgdata18                                        # procurehub_pgdata18
$C logs db | grep 01-timezone                                           # init hook ran once
```

**Do not start the backend yet** — it would create the 38 empty tables and
collide with the restore.

---

## Phase 7 — Restore the production dump

Copy the dump over SSH. It is git-ignored, never enters an image, and is
deleted from the container afterwards.

```powershell
scp -i "C:\path\to\procurehub-key.pem" `
    D:\Downloads\ProcureHub\procurehub.dump `
    ubuntu@EC2_PUBLIC_IP:/home/ubuntu/procurehub/procurehub.dump
```

```bash
./deploy/restore-dump.sh procurehub.dump
```

The script refuses to run if the target is not the `db` container, if the
server is not Postgres 18, if `DATABASE_URL` in the shell mentions Neon, or
if the database already has tables (pass `--clean` only if you mean it). It
then asks you to type the database name before writing anything.

**Expected afterwards:** 38 tables, **54 vendors**, plus users, customers,
parts, inventory and orders — printed by the script.

---

## Phase 8 — Backend

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d backend
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs -f backend
```

**Verify:**

```bash
curl -i http://127.0.0.1:4008/health                     # 200 {"status":"ok"}
$C exec backend sh -c 'echo $DATABASE_URL'               # ...@db:5432/... and NOT neon.tech
$C logs backend | grep -iE 'OperationalError|FATAL|could not connect'   # expect nothing
$C exec db psql -U procurehub -d procurehub -tAc "select count(*) from vendors"  # still 54
```

That last check matters: it confirms startup did **not** wipe or re-create
anything. The app only ever issues `CREATE TABLE IF NOT EXISTS` plus additive
`ALTER TABLE ... ADD COLUMN`, so an existing restored schema is left alone —
but verify rather than trust.

---

## Phase 9 — Frontend

The API base is `/` (same origin), so nothing host-specific is compiled in.
Build and start:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml build frontend
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d frontend
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4009/     # 200
```

If you ever point the frontend at a *different* origin than the page it is
served from, set `VITE_API_BASE_URL=https://api.example.com` in `.env` and
**rebuild** — Vite inlines it at build time; a restart is not enough.

---

## Phase 10 — Nginx

```bash
sudo cp deploy/nginx/procurehub.conf /etc/nginx/sites-available/procurehub
sudo ln -sf /etc/nginx/sites-available/procurehub /etc/nginx/sites-enabled/procurehub
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Routing:

| Public path | Goes to |
|---|---|
| `/api/…` | backend `127.0.0.1:4008` |
| `/health` | backend (for uptime pingers) |
| everything else | frontend `127.0.0.1:4009` (which handles SPA fallback) |

`client_max_body_size 32m` sits above the app's own 25 MB limit so a large
vendor file gets the backend's clear message, not a bare 413. Proxy timeouts
are 120 s because a 25,000-row import is processed synchronously.

**HTTPS**, once DNS points at the instance:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d procurehub.example.com
```

No application change is needed — the frontend calls the API on whatever
origin it was loaded from, so it follows to HTTPS automatically.

---

## Phase 11 — Security Group, final state

Inbound: **22 (my IP), 80, 443**. Nothing else.

If you temporarily open **4008** to debug before Nginx works, treat it as
temporary: remove the rule the moment Phase 10 passes, and confirm with

```bash
docker ps --format '{{.Names}} {{.Ports}}'    # every mapping should read 127.0.0.1:
ss -ltnp | grep -E ':(5432|55432|4008|4009)'  # all bound to 127.0.0.1 only
```

---

## Phase 12 — Production testing

```bash
./deploy/verify-deployment.sh
```

Then, in a browser at `http://EC2_PUBLIC_IP/`:

- [ ] Frontend loads
- [ ] Log in with an **existing migrated user** (production credentials — the
      `ADMIN_PASSWORD` in `.env` is ignored because users already exist)
- [ ] Vendor Inventory lists the **54 vendors**
- [ ] Customer Orders shows the migrated orders
- [ ] Part Intelligence finds a known part
- [ ] Command Centre renders without errors
- [ ] An update (e.g. a vendor selection) saves and survives a refresh
- [ ] Logout, then log in again
- [ ] Upload a small vendor file; confirm it appears in File Inbox and can be
      downloaded back (proves the `uploads` volume)

---

## Phase 13 — Persistence

```bash
C="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml"
$C exec db psql -U procurehub -d procurehub -tAc "select count(*) from vendors"   # 54
$C down          # NEVER with -v
$C up -d
sleep 20
$C exec db psql -U procurehub -d procurehub -tAc "select count(*) from vendors"   # still 54
```

> **`docker compose down -v` destroys the database volume.** It must never be
> run on this instance. The same applies to `docker volume prune`.
>
> The one safe-looking trap: `down -v` is exactly what the local dev docs
> suggest for a clean slate. On EC2 it deletes the restored production data.

Take a backup before any risky change:

```bash
$C exec -T db pg_dump -U procurehub -Fc procurehub > ~/backups/procurehub-$(date +%F).dump
```

---

## Phase 14 — Final status report

Fill in after Phase 13 passes:

```
EC2 public IP / Elastic IP : ____________________
Instance type / storage    : ____________ / ____ GiB gp3
Ubuntu version             : 24.04 LTS
Running containers         : (docker compose ps)
Image versions             : procurehub-backend, procurehub-db:local (postgres:18-alpine), procurehub-frontend
PostgreSQL version         : (show server_version)  → expect 18.x
Database volume            : procurehub_pgdata18
Frontend URL               : http://<ip>/
Backend API route          : http://<ip>/api/…   (health: http://<ip>/health)
Nginx status               : (systemctl status nginx)
Open Security Group ports  : 22 (my IP), 80, 443
Data verification          : 38 tables, 54 vendors, __ users, __ customers, __ parts
Integrations               : WhatsApp / Gmail / Sheets = disabled
Outstanding issues         : ____________________
```

---

## Rollback

Neon is untouched and still serves the Render deployment. If EC2 misbehaves,
nothing needs undoing — keep using Render. Decommission Render only after
this instance has run a full business day, and decide explicitly which
instance owns the WhatsApp and Gmail integrations before enabling them here;
**both must never be enabled at once**.
