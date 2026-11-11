# 6. Troubleshooting

Start with `curl -s http://localhost:8091/health` on the server — the `partdb.error` field usually names the problem.

## Part-DB

| Symptom | Cause and fix |
|---|---|
| Every Part-DB page returns error 500 right after installation | The database was never created. Run `docker compose exec --user www-data partdb php bin/console doctrine:migrations:migrate -n`. |
| `400 Bad Request` / "Untrusted Host" | The name you opened is not in `PARTDB_TRUSTED_HOSTS`. Add it (regular expression, dots as `\.`), then `docker compose up -d`. |
| Wrong `http://` redirects behind a proxy | The proxy's network is not in `TRUSTED_PROXIES` in `docker-compose.yml`. |

## bom-agent `/health` says `degraded`

| `error` contains | Cause and fix |
|---|---|
| `Brak PARTDB_API_TOKEN` (no token) | `PARTDB_API_TOKEN` is empty in `.env`. Create the token ([installation step 5](02-installation.md#5-allow-api-access-and-create-a-token)), then `docker compose up -d`. |
| `odmówił dostępu (403)` (access denied) | Either the token's user lacks the **Access API** permission, or the token is *Read only* — bom-agent needs level **Edit**. |
| `przekierowuje` … `need_pw_change` (redirect) | The user must change the password at first login. Log in once in the browser. |
| `odrzucił token (401)` (token rejected) | The token was deleted, expired or mistyped. |
| `reachable: false` without a message | Part-DB is not running or not healthy yet: `docker compose ps`, `docker compose logs partdb`. |

## The app

| Symptom | Cause and fix |
|---|---|
| The **Zdjęcie** button opens only the gallery, no live camera | The page is not on HTTPS. See [installation step 7](02-installation.md#7-https-and-access-from-the-phone). |
| Photos in Part-DB show as broken images | `PUBLIC_BASE_URL` points to an address the browser cannot reach (e.g. `localhost`). Fix it in `.env`. Already saved attachments keep the old address. |
| **Part-DB ↗** opens the wrong address | `PARTDB_PUBLIC_URL` in `.env`. |
| Upload of several photos fails behind a proxy | Raise the proxy's request size limit to at least 25 MB. |

## The agent

| Symptom | Cause and fix |
|---|---|
| Tools `bom` not visible in Claude Code | Check that `docker exec -i bom-agent python -m bom_agent.mcp_server` works in a terminal; for remote setups, that SSH logs in without a password. |
| `intake_propose` refuses because of similar parts | Intended. The agent must decide: `merge_into` (same thing) or `force_new` (different thing). |
| Unattended run writes "brak zleceń" (nothing to do) although items are flagged | Look at the log line `kolejka:` — it is measured independently of the model. If it shows flagged items, the MCP connection failed; run the script by hand and read the output. |

## Upgrading Part-DB

1. Run `./backup.sh`.
2. Change the image tag in `docker-compose.yml`, then `docker compose pull partdb && docker compose up -d partdb`.
3. **Always** run the migration command from installation step 3. Without it Part-DB starts with a database schema that does not match the code, and some pages fail with "no such column".
4. Check `/health` and open a part card in the app.

---

Next: [7. How it works](07-how-it-works.md)
