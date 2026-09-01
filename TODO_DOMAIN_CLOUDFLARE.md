# TO-DO — Put ProcureHub on `partsexpressonline.com` with HTTPS

**Goal:** a working HTTPS address so WhatsApp can move to AWS and the system
runs fully on our own server, with no Neon quota to hit.

**Target:** `https://procurehub.partsexpressonline.com`

> **Why HTTPS is mandatory:** Meta only delivers WhatsApp webhooks to an
> HTTPS URL, on port 443. A bare IP like `http://52.66.83.8:4008` can never
> receive vendor files, no matter which ports are open.

---

## Where things stand today

| | Status |
|---|---|
| ProcureHub on AWS (`52.66.83.8`) | ✅ running — backend 4008, frontend 4009 |
| Database on AWS | ✅ PostgreSQL 18.6, 54 vendors, 132,405 stock rows |
| **Gmail → customer orders** | ✅ **working on AWS** (order 65 imported and allocated) |
| **WhatsApp → vendor stock** | ❌ still on Render, and Render's database is blocked |
| Neon (Render's database) | ❌ blocked — *data transfer quota exceeded* |
| Domain | ✅ `partsexpressonline.com` purchased |

**So the only missing piece is HTTPS.**

---

## The plan: Cloudflare Tunnel

Chosen over the alternatives because it needs **no open ports** and
**nothing from Anik**:

- The server dials *out* to Cloudflare, so no inbound firewall rule and no
  Security Group request.
- Ports 80/443 on that box belong to Anik's Caddy (it fronts the Dealer
  Portal and CarTrends). A tunnel doesn't touch them.
- HTTPS certificates are automatic and free.

```
Internet → https://procurehub.partsexpressonline.com
         → Cloudflare (HTTPS terminates here)
         → tunnel (outbound from EC2, no open port)
         → 127.0.0.1:4009 frontend  ·  127.0.0.1:4008 /api
```

---

## Step 1 — YOU (only step I cannot do)

Needs registrar login, which I don't have.

- [ ] Create a free account at **cloudflare.com**
- [ ] **Add site** → `partsexpressonline.com`, choose the **Free** plan
- [ ] Cloudflare shows **two nameservers** (e.g. `xxx.ns.cloudflare.com`)
- [ ] Log in to wherever the domain was bought and **replace its nameservers**
      with those two
- [ ] Wait for Cloudflare to show **Active** (usually minutes, up to 24h)

Tell me when it says Active.

## Step 2 — YOU (2 minutes, guided)

- [ ] Cloudflare dashboard → **Zero Trust** → **Networks → Tunnels**
- [ ] **Create a tunnel**, name it `procurehub`
- [ ] Choose **Docker** — Cloudflare shows a command containing a long token
- [ ] Send me that command (the token is what I need)

## Step 3 — ME

- [ ] Run the tunnel as a container on EC2, alongside the app
- [ ] Route `procurehub.partsexpressonline.com`:
      `/api/*` → `127.0.0.1:4008`, everything else → `127.0.0.1:4009`
- [ ] Rebuild the frontend for the new address (the API URL is compiled in)
- [ ] Update `CORS_ORIGINS`
- [ ] Verify: site loads over HTTPS, login works, 54 vendors visible

## Step 4 — ME (the WhatsApp cutover)

Do this **before 09:30**, before vendor files start arriving.

- [ ] Confirm Render is not importing (its database is blocked anyway)
- [ ] Set `WHATSAPP_ENABLED=true` on AWS
- [ ] Set `WHATSAPP_WEBHOOK_CALLBACK_URL` to the new HTTPS address
- [ ] In **Meta dashboard → WhatsApp → Configuration**, change the webhook to
      `https://procurehub.partsexpressonline.com/api/whatsapp/webhook`
      and re-verify *(needs Meta login — likely you or Sir)*
- [ ] Ask one vendor to send a stock file; confirm it imports on AWS
- [ ] Confirm the vendor-wise purchase messages arrive

## Step 5 — Afterwards

- [ ] **Leave Render and Neon alone for at least a week** — they cost nothing
      while idle and are the only rollback
- [ ] When Neon's quota resets, dump it and merge anything imported after
      **24 Aug 09:26** that isn't already on AWS
- [ ] Consider an **Elastic IP** for the EC2 box (its public IP already
      changed once)
- [ ] Move to Anik's shared PostgreSQL if he still wants it — unrelated to
      this work, ~15 minutes
- [ ] Re-mint the Gmail OAuth token with the `gmail.modify` scope to stop the
      harmless 403 in the logs

---

## Things NOT to do

- ❌ **Don't delete data from Neon to free the quota.** It is a *data
  transfer* (bandwidth) limit, not storage — deleting rows changes nothing.
  Only a plan upgrade or the monthly reset restores access.
- ❌ **Don't run `docker compose down -v`** on the EC2 box — it destroys the
  database volume holding the production data.
- ❌ **Don't enable WhatsApp or Gmail on Render and AWS at the same time** —
  both would process the same messages into two different databases.

---

## If asked "why not just use the IP?"

Because WhatsApp will not deliver to it. Everything else — the website,
logins, reports — works on the IP today. The domain exists solely so vendor
files can reach us, and secondarily so passwords stop travelling
unencrypted.
