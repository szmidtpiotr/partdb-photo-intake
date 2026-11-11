# 5. Unattended recognition (optional)

> **Needs Claude Code** on a machine that can reach bom-agent, logged in with a subscription or API key. Each run that has work costs model usage.

`scripts/recognize_requested.sh` runs once an hour from cron. It:

1. asks bom-agent over HTTP whether anything is flagged with **Rozpoznaj** and has no proposals yet — if not, it **does not start the model at all**;
2. otherwise starts `claude -p` with the prompt in `scripts/recognize_requested.md`, up to 15 items per run, with a 900-second limit;
3. writes a log to `~/.local/state/bom-recognize.log` (trimmed to the last 1000 lines).

## Why it cannot damage the inventory

The run is closed from three sides:

1. **Only three tools** are allowed: `intake_pending`, `intake_zoom`, `intake_propose`. No `intake_commit`, so it can only write *proposals*. The worst outcome of a mistake is a card you reject with one tap.
2. **Its own MCP config** (`scripts/mcp-cron.json`) with `--strict-mcp-config`: the Part-DB server is not in it, so the run cannot read or write the inventory at all.
3. **`INTAKE_ONLY_REQUESTED=1`** is set inside the bom-agent process for this run, so `intake_pending` returns only the items you flagged — whatever the model asks for. A sentence in a prompt is a request; this is a guarantee.

## Setup

1. Install and log in to Claude Code for the user that will run cron (`claude` must work in a terminal).
2. If bom-agent runs on another machine, edit `scripts/mcp-cron.json` and put `"ssh", "user@your-server"` in front of `"docker"` (see [chapter 3](03-connect-agent.md#claude-code-on-a-different-machine)), and set `BOM_RECOGNIZE_URL` to bom-agent's address.
3. Test by hand: flag one photo with **Rozpoznaj** and run `scripts/recognize_requested.sh`, then read the log.
4. Add to `crontab -e`:
   ```
   7 * * * * /path/to/partdb-photo-intake/scripts/recognize_requested.sh
   ```
   Minute 7 rather than 0, because on the full hour everybody's jobs hit the API at once.

Environment variables the script understands: `BOM_RECOGNIZE_URL` (default `http://localhost:8091`), `BOM_RECOGNIZE_LOG`, `BOM_RECOGNIZE_LOCK`, `BOM_RECOGNIZE_TIMEOUT` (seconds, default 900).

Measured on the author's setup: about 24 seconds per item.

---

Next: [6. Troubleshooting](06-troubleshooting.md)
