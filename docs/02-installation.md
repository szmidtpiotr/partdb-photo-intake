# 2. Installation

Everything below happens on the server, in a terminal. Each step says what you should see when it worked.

## 1. Download the project

```bash
git clone https://github.com/szmidtpiotr/partdb-photo-intake.git
cd partdb-photo-intake/deploy
```

## 2. Fill in the configuration

```bash
cp .env.example .env
openssl rand -hex 32        # copy the output
nano .env                   # or any editor
```

Fill in:

| Field | What to put there |
|---|---|
| `PARTDB_APP_SECRET` | the random string from `openssl rand -hex 32` |
| `PARTDB_ADMIN_PASSWORD` | a password for the Part-DB `admin` account (you will be asked to change it at first login) |
| `PARTDB_TRUSTED_HOSTS` | the names under which Part-DB will be opened: keep `partdb` and `localhost`, replace `192\.168\.1\.50` with your server's IP and `partdb\.example\.com` with your domain (dots escaped with `\`). Keep the single quotes. |
| `PUBLIC_BASE_URL` | the address at which **your phone** opens bom-agent, e.g. `https://intake.example.com` or for a first test `http://192.168.1.50:8091` |
| `PARTDB_PUBLIC_URL` | the address at which your browser opens Part-DB |
| `PARTDB_API_TOKEN` | leave empty for now, step 5 |

`PUBLIC_BASE_URL` matters more than it looks: photos are attached to Part-DB parts as **links** to bom-agent at this address. `localhost` would work only on the server itself.

## 3. Start Part-DB and create its database

```bash
docker compose up -d partdb
docker compose exec --user www-data partdb php bin/console doctrine:migrations:migrate -n
```

**Expected:** `[OK] Successfully migrated to version: …`.

A fresh Part-DB has **no database tables** until this command runs; before that every page returns error 500. The same command is needed after every Part-DB upgrade (see [Troubleshooting](06-troubleshooting.md#upgrading-part-db)).

Wait about a minute and check: `docker compose ps` should show `partdb` as `healthy`.

## 4. First login to Part-DB

1. Open `http://<server-ip>:8090` in a browser.
2. Log in as `admin` with `PARTDB_ADMIN_PASSWORD`.
3. Part-DB asks for a new password — set it. **Until you do, the API answers every request with a redirect**, so bom-agent cannot work.

## 5. Allow API access and create a token

A fresh admin account **does not have API permissions**.

1. Menu **Tools → System → Users**, pick your user, tab **Permissions**.
2. In the **API** group set **Access API** and **Use MCP tools (for AI agents)** to *Allow*. Save.
3. Open your **user settings** (user menu, top right), section **API tokens**, create a new token:
   - name: e.g. `bom-agent`,
   - access level: **Edit** (bom-agent creates parts and changes stock; *Read only* is not enough).
4. Copy the token (starts with `tcp_`) — Part-DB shows it only once.
5. Put it into `.env` as `PARTDB_API_TOKEN=tcp_…`.

## 6. Start bom-agent

```bash
docker compose up -d --build
curl -s http://localhost:8091/health
```

**Expected:** JSON with `"status":"ok"`, `"reachable":true`, `"authorized":true`.

If you see `"status":"degraded"`, the `error` field says why — see [Troubleshooting](06-troubleshooting.md).

Open `http://<server-ip>:8091` — you should see the Warsztat app with an empty inventory.

## 7. HTTPS and access from the phone

For the camera you need HTTPS ([Requirements](01-requirements.md#https-if-you-want-to-use-the-phone-camera)). Point two names at the server through your reverse proxy:

| Name (example) | Goes to |
|---|---|
| `intake.example.com` | port `8091` (bom-agent) |
| `partdb.example.com` | port `8090` (Part-DB) |

Then make sure `.env` says the same (`PUBLIC_BASE_URL`, `PARTDB_PUBLIC_URL`, `PARTDB_TRUSTED_HOSTS`) and run `docker compose up -d` again.

Settings for the proxy:
- allow uploads of at least **25 MB** per request (several photos at once),
- if TLS ends on the proxy, Part-DB must trust it — `TRUSTED_PROXIES` in `docker-compose.yml` already covers the usual private networks.

> **Security.** bom-agent has **no login of its own**. Whoever reaches its address can add photos to the queue and change quantities in Part-DB (through bom-agent's token). Do **not** expose it to the internet without authentication in front of it (Authelia, Authentik, basic auth on the proxy, or a VPN/Tailscale).

On the phone: open `PUBLIC_BASE_URL` in the browser and use "Add to home screen" — it behaves like an app. On Android it also appears in the **Share** menu, so you can send photos to it from the gallery or Google Lens.

## 8. Backup

```bash
./backup.sh /path/to/backups
```

It writes a dated folder with: Part-DB's full backup (database, attachments, config), a consistent copy of bom-agent's queue database, a tar of all intake photos, and a copy of `.env`. Old folders are removed after 30 days. Run it from cron, e.g. `0 3 * * * /path/to/deploy/backup.sh /path/to/backups`.

The intake photos are the only data you cannot recreate without going through your boxes again — keep a copy outside the server.

---

Next: [3. Connecting the AI agent](03-connect-agent.md)
