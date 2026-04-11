# SemanticDog

Your NAS keeps your files safe from hardware failure. SemanticDog checks they're still actually openable.

ZFS and RAID verify that bits on disk match what was written. That's not the same as verifying a JPEG can be decoded, a RAW file parsed, or a PDF opened. Bit-rot, partial writes, and failed copies can produce files that pass every checksum but are silently broken at the application layer — you won't find out until you need them.

SemanticDog scans your library on a schedule, tells you which files are corrupt, and alerts you before you need them.

**Works with AI agents.** SemanticDog exposes an [MCP](https://modelcontextprotocol.io) server — Claude and other agents can query scan results, trigger scans, and reason about your library health directly.

---

## Install

```bash
# with pip
pip install semanticdog

# with uv (recommended)
uv tool install semanticdog
```

Then verify your system has the tools it needs:

```bash
sdog check-deps
```

The only hard requirement is Python 3.12+. Install `ffmpeg` for video, `pillow-heif` for HEIC — everything else is bundled.

---

## First scan

```bash
sdog scan /mnt/photos
```

Results go into a local SQLite database. When it finishes:

```bash
sdog show-corrupt    # list broken files with error details
sdog show-stats      # count by status: ok, corrupt, unreadable...
```

Exit code is `0` if everything is clean, `2` if issues were found — works naturally in scripts and CI.

---

## What the results mean

| Status | What it means | What to do |
|--------|--------------|------------|
| `ok` | File opened and parsed successfully | Nothing |
| `corrupt` | File is structurally broken | Restore from backup |
| `unreadable` | Couldn't open the file at all | Check mount / permissions — usually not the file's fault |
| `unsupported` | Library version doesn't recognise this format variant | Update libraries; not flagged as corrupt |
| `error` | Validator crashed or timed out | Check `sdog report --format json` for details |

**`unreadable` usually means a mount problem, not corruption.** If you suddenly see many unreadable files, check your NAS connectivity before investigating individual files.

---

## Supported formats

Photos: JPEG · PNG · TIFF · HEIC · WebP  
RAW: CR2 · CR3 · NEF · ARW · ORF · RW2 · PEF · DNG · RAF · NRW  
Documents: PDF · DOCX · XLSX · PPTX · DOC · XLS · PPT  
Video: MP4 · MOV · MTS · M4V · MKV  
Audio: MP3 · FLAC · WAV · AAC

---

## Scheduled scanning

```bash
0 2 * * * sdog scan --config /data/config/config.yaml >> /data/logs/sdog.log 2>&1
```

On subsequent runs, only changed files are re-validated. A 100k-photo library might take an hour on first scan and two minutes after that.

---

## Notifications

Get alerted when corrupt files are found.

**Email:**
```yaml
notify_email: you@example.com
smtp_host: smtp.example.com
smtp_user: sdog@example.com
smtp_pass: ""   # use SDOG_SMTP_PASS env var
```

**Webhook** (Gotify, Ntfy, Pushover, Slack):
```yaml
webhook_url: https://gotify.example.com/message?token=abc
```

Alerts only fire on the first detection — no repeat notifications for the same broken file.

---

## AI agent integration (MCP)

SemanticDog has a built-in MCP server. Connect Claude or any MCP-compatible agent to query scan results and trigger scans conversationally.

**Enable in config:**
```yaml
mcp_enabled: true
mcp_allow_write: true   # lets agents trigger scans and reset records
```

```bash
SDOG_MCP_AUTH_TOKEN=your-secret uvicorn semanticdog.server:app --port 9090
```

**Add to Claude Code** (`~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "semanticdog": {
      "type": "sse",
      "url": "http://localhost:9090/mcp/sse",
      "headers": { "Authorization": "Bearer your-secret" }
    }
  }
}
```

Once connected, you can ask Claude things like *"which photos are corrupt?"* or *"scan my 2024 folder and summarize the results"*.

---

## Configuration

Copy `config.example.yaml` and edit:

```yaml
paths:
  - /mnt/photos
  - /mnt/documents

db_path: /data/state/state.db

workers: 4        # parallel validators
raw_workers: 2    # RAW uses more memory — keep lower than workers

schedule: "0 2 * * *"
```

Every option has a matching `SDOG_*` environment variable. Env vars always override the YAML file. Full reference is in [`config.example.yaml`](config.example.yaml).

---

## HTTP API and Prometheus

```bash
uvicorn semanticdog.server:app --port 9090
```

- `GET /metrics` — Prometheus scrape endpoint
- `POST /trigger` — kick off a scan remotely (also accepts `{"scope": "/mnt/photos/2024"}`)
- `GET /status` — current state and file counts as JSON

---

## Troubleshooting

**New camera RAW files show `unsupported`**  
LibRaw adds new cameras gradually. `unsupported` is not corruption — the file is fine, just unrecognised. Fix: `pip install -U rawpy`.

**Many `unreadable` files suddenly**  
Almost always a mount going offline or a permission change. SemanticDog flags this as a suspected mount failure in the notification if more than half the scan is unreadable.

**HEIC not validating**  
Needs `pillow-heif`: `pip install pillow-heif`. Run `sdog check-deps` to see everything that's missing at once.

**Video not validating**  
Needs ffmpeg: `apt install ffmpeg` / `brew install ffmpeg`.

**Moved your library to a new path**  
```bash
sdog db-export -o backup.json
sdog db-import -i backup.json --path-map /old/path:/new/path
```

---

<details>
<summary>AI Agent Reference — structured data for agents and tooling</summary>

### Project identity

```
name:       semanticdog
binary:     sdog
module:     semanticdog
python:     >=3.12
entrypoint: semanticdog/cli.py
```

### Repository layout

```
semanticdog/
  cli.py            CLI — all commands (typer)
  config.py         Config dataclass + load_config() + env override
  db.py             Database — SQLite WAL, all queries
  scanner.py        Scanner + walk_paths() + _validate_file() pebble worker
  server.py         FastAPI — /health /metrics /status /trigger + build_app()
  notify.py         ScanSummary, Notifier, SmtpNotifier, WebhookNotifier
  mcp_server.py     MCP SSE transport
  exceptions.py     ConfigError, DatabaseError, LockError
  validators/
    __init__.py     registry: register(), get_validator(), all_extensions()
    base.py         BaseValidator, ValidationResult, DependencyReport
    images.py       JpegValidator PngValidator TiffValidator HeicValidator WebpValidator
    raw.py          RawValidator
    documents.py    PdfValidator OoxmlValidator OleValidator
    media.py        VideoValidator AudioValidator
tests/
  fixtures/generators.py   make_minimal_jpeg, make_corrupt_jpeg, make_minimal_png, ...
  test_e2e.py               37 end-to-end tests (no mocks, real files)
  test_server.py / test_scanner.py / test_db.py / test_notify.py / test_*.py
```

### CLI exit codes

`sdog scan`: `0` = all OK · `1` = config/DB error · `2` = corrupt or unreadable files found  
`sdog check-deps`: `0` = all hard deps present · `1` = hard dep missing

### HTTP API

```
GET  /health      → 200 {"status":"ok"}
GET  /status      → 200 {status, files_indexed, by_status, last_scan}
GET  /metrics     → 200 Prometheus text
POST /trigger     → 200 {status:"complete", scan_id}
                    400 scope outside configured roots
                    409 scan already running
                    429 cooldown {retry_after_s}
                    503 not configured
GET  /mcp/sse     → SSE stream (requires mcp_enabled=true + mcp_auth_token)
```

### Config keys → env vars

| Key | Env var | Default |
|-----|---------|---------|
| `paths` | `SDOG_PATHS` (colon-sep) | `[]` |
| `exclude` | `SDOG_EXCLUDE` (colon-sep) | `["**/@eaDir/**", ...]` |
| `db_path` | `SDOG_DB_PATH` | `/data/state/state.db` |
| `workers` | `SDOG_WORKERS` | `4` |
| `raw_workers` | `SDOG_RAW_WORKERS` | `2` |
| `raw_decode_depth` | `SDOG_RAW_DECODE_DEPTH` | `structure` |
| `validation_timeout_s` | `SDOG_VALIDATION_TIMEOUT_S` | `120` |
| `force_recheck_days` | `SDOG_FORCE_RECHECK_DAYS` | `90` |
| `http_port` | `SDOG_HTTP_PORT` | `9090` |
| `notify_email` | `SDOG_NOTIFY_EMAIL` | `""` |
| `smtp_pass` | `SDOG_SMTP_PASS` | `""` |
| `webhook_url` | `SDOG_WEBHOOK_URL` | `""` |
| `mcp_enabled` | `SDOG_MCP_ENABLED` | `false` |
| `mcp_auth_token` | `SDOG_MCP_AUTH_TOKEN` | `""` |
| `mcp_allow_write` | `SDOG_MCP_ALLOW_WRITE` | `false` |
| `mcp_rate_limit_s` | `SDOG_MCP_RATE_LIMIT_S` | `60` |

### Database schema

```sql
files (
  path TEXT PRIMARY KEY,
  mtime REAL, size INTEGER,
  status TEXT,           -- ok|corrupt|unreadable|unsupported|error
  error TEXT, suggested_action TEXT,
  checked_at TEXT,       -- ISO 8601
  scan_id TEXT,
  notified_at TEXT       -- NULL = not yet notified
)
scans (
  id TEXT PRIMARY KEY,
  started_at TEXT, finished_at TEXT,
  total INTEGER, corrupt INTEGER, unreadable INTEGER,
  scope TEXT,            -- NULL = all paths
  files_per_sec REAL
)
```

### Key internal APIs

```python
from semanticdog.config import load_config
from semanticdog.db import Database
from semanticdog.scanner import Scanner

cfg   = load_config("config.yaml")       # YAML + env override
db    = Database(cfg.db_path)
stats = Scanner(cfg, db).scan()          # all paths → ScanStats
stats = Scanner(cfg, db).scan(["/sub"])  # scoped scan

db.get_corrupt_files(since="2025-01-01", ext="cr2", path_prefix="/mnt")
db.get_stats()         # {"total": N, "by_status": {...}}
db.list_scans(limit=10)
db.export_json()
db.import_json(records, force=False, path_map={"/old": "/new"})
```

### Running tests

```bash
uv run pytest                       # 427 tests
uv run pytest tests/test_e2e.py -v  # E2E only (37 tests, real files)
```

### Known limitations

- RAW `unsupported` ≠ corrupt — LibRaw doesn't cover all camera models
- HEIC: primary frame only; burst/live photo secondary frames skipped
- Sidecars (`.XMP`, `.AAE`): validated independently, no pair correlation
- `verify-hashes` command: not yet implemented

</details>
