# 1. Requirements

## A server

Any Linux machine that runs Docker: a mini PC, a NAS with Docker, a Proxmox container, a Raspberry Pi 4/5 with 4 GB RAM or more. The author runs it in a Debian LXC container with 2 CPU cores and 4 GB RAM; Part-DB and bom-agent together use well under 1 GB.

- **Docker** with the **Compose** plugin (`docker compose version` must work).
- About 2 GB of disk for the images, plus space for photos (a phone photo is 2–5 MB; 500 photos ≈ 2 GB).

## HTTPS, if you want to use the phone camera

Browsers allow the camera in a web page **only over HTTPS** (or on `localhost`). Without HTTPS you can still pick photos from the gallery, but the in-app camera stays off.

Typical solutions, any of them works:
- a reverse proxy you already have (Nginx Proxy Manager, Caddy, Traefik) with a certificate,
- a domain with Let's Encrypt,
- a tunnel (Tailscale, Cloudflare Tunnel).

Setting up HTTPS is not covered in detail here — it depends on what you already run.

## An AI agent that speaks MCP

**MCP** (Model Context Protocol) is the standard way of giving an AI assistant tools. bom-agent exposes its tools over MCP; the agent looks at the photos and calls them.

- Tested with **Claude Code** (the CLI from Anthropic) with a Claude subscription or API key.
- The recognition quality depends on the model. With Claude the author measured that a smaller model invents plausible part names instead of saying "unreadable" — use a model good at reading images (in Claude terms: Sonnet or better).
- The optional unattended recognition ([chapter 5](05-unattended-recognition.md)) needs Claude Code specifically, because it uses `claude -p`.

## Optional

- **Part-DB info provider keys** (TME, DigiKey, Mouser…) — LCSC, Adafruit, SparkFun, Pololu and the generic shop-page reader work without a key. Keys are entered in Part-DB's own settings, not here.
- A **Python 3.13** environment with [uv](https://docs.astral.sh/uv/), only if you want to run the tests or develop.

---

Next: [2. Installation](02-installation.md)
