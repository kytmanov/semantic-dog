# Changelog

## [0.2.0] — 2026-04-14

### New features

**Real-time scan progress**  
The scanner now prints a live progress line to stderr every 5 seconds:
```
[1500/15234]  9.8%  ok:1498  corrupt:2  unreadable:0  43.1 f/s  ETA: ~3.2 min
```
Status counts and speed update as workers finish — they are no longer stuck at zero for the entire run. On a TTY the line overwrites in place; in logs it appends a new line.

**Cancel and resume**  
Every scan now prints its ID upfront:
```
Discovered 15234 files.
Scan ID: abc123-...  (resume with: sdog scan --resume abc123-...)
```
If a scan is interrupted (Ctrl+C, SIGTERM, crash), it stays resumable. Pick up exactly where you left off:
```bash
sdog scan --resume abc123-...
```
Interrupted scans appear as `incomplete` in `sdog list-scans`. Resuming twice in a row correctly tracks position — each resume sees only the files that were not yet processed.

**`sdog show-stats` — library health dashboard**  
New command for the "is everything OK?" view:
- Files indexed by status and format
- Last 5 completed scans with corrupt/unreadable counts
- Stale files (not checked in N days)
- Most frequent error messages

`sdog report` remains the drill-down companion — use it to list individual corrupt files with error details.

### Changes

- `sdog show-corrupt` removed. `sdog report` covers everything it did, with more options (`--format json/csv`, `--since`).
- Config validation now runs before `sdog scan` and `sdog estimate` start. Missing `paths:`, invalid worker counts, or MCP enabled without an auth token now produce a clear error at startup instead of failing deep in the scan.

### Bug fixes

- **Concurrent instance lock** — two `sdog` processes pointed at the same database could both believe they held the exclusive lock and corrupt state. Fixed.
- **Webhook SSRF protection** — the private-IP guard existed but was never enforced. Webhooks to internal addresses (`192.168.x.x`, `localhost`, etc.) are now blocked unless `SDOG_WEBHOOK_ALLOW_PRIVATE=true` is set.
- **Scan deadlock on multi-file pools** — a race between the inline result drain and the blocking drain caused scans to hang indefinitely when more than one file was in flight. Fixed.
- **`asyncio.get_event_loop()` deprecation** — updated to `get_running_loop()` for Python 3.12+ compatibility.


## [0.1.1] — 2026-04-12

- Installation instructions updated.
- MIT license added.


## [0.1.0] — 2026-04-11

Initial release.

- Semantic validation for JPEG, PNG, TIFF, HEIC, WebP, RAW (CR2/CR3/NEF/ARW/ORF/RW2/PEF/DNG/RAF/NRW), PDF, DOCX/XLSX/PPTX, DOC/XLS/PPT, MP4/MOV/MTS/M4V/MKV, MP3/FLAC/WAV/AAC.
- SQLite state database with incremental re-scan (only changed files re-validated).
- Email (SMTP) and webhook notifications on first corrupt detection.
- HTTP API (`/health`, `/status`, `/metrics`, `/trigger`) and Prometheus endpoint.
- MCP server for AI agent integration.
- `sdog scan`, `sdog report`, `sdog estimate`, `sdog list-scans`, `sdog status`, `sdog reset`, `sdog db-export`, `sdog db-import`, `sdog check-deps`.
