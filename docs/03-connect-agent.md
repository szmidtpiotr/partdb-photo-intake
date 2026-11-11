# 3. Connecting the AI agent

The agent uses **two MCP servers**:

| Server | What it gives the agent | Where it runs |
|---|---|---|
| `bom` (this project) | intake queue, photos and zoom, proposals and commits, boxes, enrichment, inventory digest | inside the `bom-agent` container, over **stdio** |
| `partdb` (built into Part-DB) | reading part details, advanced search, searching supplier catalogues | Part-DB's `/mcp` endpoint over HTTP |

## Claude Code on the same machine as Docker

Copy the example into the folder where you start Claude Code:

```bash
cp .mcp.json.example .mcp.json
```

```json
{
  "mcpServers": {
    "partdb": {
      "type": "http",
      "url": "https://partdb.example.com/mcp",
      "headers": { "Authorization": "Bearer ${PARTDB_MCP_TOKEN}" }
    },
    "bom": {
      "command": "docker",
      "args": ["exec", "-i", "bom-agent", "python", "-m", "bom_agent.mcp_server"]
    }
  }
}
```

- Replace the `url` with your `PARTDB_PUBLIC_URL` + `/mcp`.
- `PARTDB_MCP_TOKEN` is a Part-DB API token (you can create a second one, level *Edit*). Set it as an environment variable before starting Claude Code; don't write it into the file.

## Claude Code on a different machine

The `bom` server must run where the queue and photos are — inside the container. Reach it over SSH:

```json
"bom": {
  "command": "ssh",
  "args": ["user@your-server", "docker", "exec", "-i", "bom-agent", "python", "-m", "bom_agent.mcp_server"]
}
```

This needs SSH key login (no password prompt) and a user allowed to run `docker`.

## Check it works

Start Claude Code and ask: *"Call inventory_digest."* You should get a text listing of your inventory (empty right after installation). Then: *"What is waiting in the intake queue?"* — the agent calls `intake_pending`.

## The tools

| Tool | What it does |
|---|---|
| `inventory_digest(scope)` | compact text of the whole inventory (or a part of it) — call it before designing anything |
| `intake_pending(limit, only_requested)` | items waiting in the queue, **with the photos** and the list of existing categories |
| `intake_zoom(item_id, crop, shot)` | a crop from the **original** photo — for reading small markings |
| `intake_commit(item_id, parts, location, confirm)` | without `confirm=true` returns a plan only; with it, creates parts / adds stock in Part-DB, attaches the photos |
| `intake_propose(item_id, parts, location)` | writes **proposals** that the user approves in the app; touches nothing in Part-DB |
| `intake_reject(item_id, note)` | rejects an item (the photo stays on disk) |
| `assign_location(...)` | puts parts registered "without a box" into a box |
| `enrich_part(...)` | adds a catalogue picture, datasheet, parameters and price from TME or LCSC (through Part-DB's info providers) |
| `record_source(...)` | records where a module was bought (shop page, price, picture) for shops Part-DB does not know |

Each tool has a detailed description that the agent reads; those descriptions are in Polish, which Claude handles without problems.

---

Next: [4. Daily use](04-daily-use.md)
