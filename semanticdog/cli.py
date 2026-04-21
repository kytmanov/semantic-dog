"""sdog CLI entrypoint."""

from __future__ import annotations

import json
import sys
from typing import Optional

import typer

app = typer.Typer(
    name="sdog",
    help="SemanticDog — file semantic integrity validator.",
    no_args_is_help=True,
)

_CONFIG_SEARCH_PATHS = [
    "./config.yaml",
    "~/.config/semanticdog/config.yaml",
    "/data/config/config.yaml",
]


def _find_config() -> str | None:
    from pathlib import Path
    for candidate in _CONFIG_SEARCH_PATHS:
        p = Path(candidate).expanduser()
        if p.exists():
            return str(p)
    return None


def _load_cfg(config_path: str | None = None, validate: bool = False):
    from .config import load_config
    resolved = config_path or _find_config()
    try:
        cfg = load_config(resolved)
        if validate:
            cfg.validate()
        return cfg
    except Exception as e:
        typer.echo(f"Config error: {e}", err=True)
        raise typer.Exit(1)


def _open_db(cfg):
    from .db import Database
    try:
        return Database(cfg.db_path)
    except Exception as e:
        typer.echo(f"DB error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Host interface to bind"),
    port: Optional[int] = typer.Option(None, "--port", help="Port to listen on"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Run the HTTP server for the Web UI and API."""
    import uvicorn

    from .runtime import load_runtime
    from .server import create_app

    resolved = config or _find_config()
    runtime = load_runtime(resolved)
    listen_port = port or (runtime.cfg.http_port if runtime.cfg is not None else 8181)

    if runtime.config_error:
        typer.echo(f"Config warning: {runtime.config_error}", err=True)
    if runtime.db_error:
        typer.echo(f"DB warning: {runtime.db_error}", err=True)

    uvicorn.run(create_app(runtime), host=host, port=listen_port)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

@app.command()
def scan(
    path: Optional[str] = typer.Argument(None, help="Scope scan to this path (default: all SDOG_PATHS)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Walk and count without validating or writing DB"),
    strict: bool = typer.Option(False, "--strict", help="Enable strict mode (disable parser recovery)"),
    exclude: Optional[str] = typer.Option(None, "--exclude", help="Additional glob exclusion pattern"),
    resume: Optional[str] = typer.Option(None, "--resume", help="Resume interrupted scan by scan ID"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Validate files for semantic integrity.

    Progress is printed to stderr every 5 seconds with % complete and ETA.
    The scan ID is printed at startup — use it with --resume to continue an interrupted scan.
    """
    cfg = _load_cfg(config, validate=True)
    if path:
        cfg.paths = [path]
    if exclude:
        cfg.exclude = list(cfg.exclude) + [exclude]

    if dry_run:
        from .scanner import walk_paths
        file_list = walk_paths(cfg.paths, cfg.follow_symlinks, cfg.exclude)
        by_ext: dict[str, int] = {}
        for fpath, _, _ in file_list:
            from pathlib import Path
            ext = Path(fpath).suffix.lower()
            by_ext[ext] = by_ext.get(ext, 0) + 1
        total = sum(by_ext.values())
        typer.echo(f"Would scan {total} files:")
        for ext, cnt in sorted(by_ext.items()):
            typer.echo(f"  {ext or '(no ext)':12s}  {cnt}")
        return

    db = _open_db(cfg)
    from .scanner import Scanner
    from .exceptions import ScanError
    scanner = Scanner(cfg, db)
    if resume:
        typer.echo(f"Resuming scan {resume} ...")
    else:
        typer.echo(f"Scanning {', '.join(cfg.paths)} ...")
    try:
        stats = scanner.scan(resume_scan_id=resume)
    except ScanError as e:
        typer.echo(f"Scan error: {e}", err=True)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        raise typer.Exit(130)
    typer.echo(
        f"Done: {stats.total} validated, {stats.corrupt} corrupt, "
        f"{stats.unreadable} unreadable, {stats.skipped} skipped, "
        f"{stats.files_per_sec():.1f} files/sec"
    )
    if stats.corrupt or stats.unreadable:
        raise typer.Exit(2)


# ---------------------------------------------------------------------------
# estimate
# ---------------------------------------------------------------------------

@app.command()
def estimate(
    path: Optional[str] = typer.Argument(None, help="Path to estimate (default: all SDOG_PATHS)"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Estimate file count and scan duration without validating."""
    cfg = _load_cfg(config, validate=True)
    db = _open_db(cfg)
    from .scanner import Scanner
    scanner = Scanner(cfg, db)
    scan_paths = [path] if path else None
    counts = scanner.estimate(scan_paths)
    total = sum(counts.values())
    fps = db.get_last_files_per_sec()
    typer.echo(f"Files needing check: {total}")
    for ext, cnt in sorted(counts.items()):
        typer.echo(f"  {ext or '(no ext)':12s}  {cnt}")
    if fps and fps > 0:
        eta_s = total / fps
        typer.echo(f"ETA at {fps:.1f} files/sec: ~{eta_s / 60:.1f} min")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Show last scan summary."""
    cfg = _load_cfg(config)
    db = _open_db(cfg)
    scans = db.list_scans(limit=1)
    if not scans:
        typer.echo("No scans recorded yet.")
        return
    s = scans[0]
    finished = s["finished_at"] or "running/interrupted"
    typer.echo(f"Last scan ID:   {s['id']}")
    typer.echo(f"Started:        {s['started_at']}")
    typer.echo(f"Finished:       {finished}")
    typer.echo(f"Scope:          {s['scope'] or 'all'}")
    typer.echo(f"Total:          {s['total']}")
    typer.echo(f"Corrupt:        {s['corrupt']}")
    typer.echo(f"Unreadable:     {s['unreadable']}")
    fps = s.get("files_per_sec")
    if fps:
        typer.echo(f"Files/sec:      {fps:.1f}")


# ---------------------------------------------------------------------------
# list-scans
# ---------------------------------------------------------------------------

@app.command("list-scans")
def list_scans(
    limit: int = typer.Option(20, "--limit", help="Number of scans to show"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """List scan history with IDs, scope, and status."""
    cfg = _load_cfg(config)
    db = _open_db(cfg)
    scans = db.list_scans(limit=limit)
    if not scans:
        typer.echo("No scans recorded yet.")
        return
    typer.echo(f"{'ID':36s}  {'Started':32s}  {'Status':12s}  {'Files':>5s}  {'Corrupt':>7s}")
    typer.echo("─" * 100)
    for s in scans:
        status_str = "complete" if s["finished_at"] else "incomplete"
        typer.echo(
            f"{s['id']:36s}  {s['started_at'] or '':32s}  {status_str:12s}  "
            f"{s['total']:5d}  {s['corrupt']:7d}"
        )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@app.command()
def report(
    format: str = typer.Option("table", "--format", help="Output format: table, json, csv"),
    since: Optional[str] = typer.Option(None, "--since", help="Filter results after this ISO date"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """List corrupt files with details — the drill-down companion to show-stats.

    Shows individual corrupt file paths and error messages. Use --since to scope
    to recent scans, --format json/csv for scripting or export.
    """
    cfg = _load_cfg(config)
    db = _open_db(cfg)
    stats = db.get_stats()
    rows = db.get_corrupt_files(since=since)

    if format == "json":
        typer.echo(json.dumps({"stats": stats, "corrupt": rows}, indent=2))
    elif format == "csv":
        typer.echo("path,status,error,checked_at")
        for r in rows:
            typer.echo(f"{r['path']},{r['status']},{r.get('error','')},{r['checked_at']}")
    else:
        typer.echo(f"Total files:    {stats['total']}")
        for st, cnt in sorted(stats.get("by_status", {}).items()):
            typer.echo(f"  {st:<14s} {cnt}")
        if rows:
            typer.echo(f"\nCorrupt files ({len(rows)}):")
            for r in rows[:20]:
                typer.echo(f"  {r['path']}")
            if len(rows) > 20:
                typer.echo(f"  ... and {len(rows) - 20} more")



# ---------------------------------------------------------------------------
# show-stats
# ---------------------------------------------------------------------------

@app.command("show-stats")
def show_stats(
    stale_days: Optional[int] = typer.Option(None, "--stale-days", help="Stale threshold in days (default: force_recheck_days from config)"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Library health dashboard — answers "is everything OK?"

    Shows aggregate counts by status and format, scan health trend, stale files,
    and most frequent errors. For a list of individual corrupt files use 'report'.
    """
    cfg = _load_cfg(config)
    db = _open_db(cfg)
    threshold = stale_days if stale_days is not None else cfg.force_recheck_days

    stats   = db.get_stats()
    scans   = db.list_scans(limit=5)
    formats = db.get_format_counts()
    stale   = db.get_stale_count(threshold)
    errors  = db.get_top_errors()

    # -- Last scan --
    completed = [s for s in scans if s["finished_at"]]
    if completed:
        s = completed[0]
        typer.echo(f"Last scan:  {s['finished_at']}  complete  {s['total']} files  {s['corrupt']} corrupt")
    else:
        typer.echo("Last scan:  No completed scans recorded yet.")
    typer.echo("")

    # -- Files by status --
    typer.echo("Files by status:")
    for st, cnt in sorted(stats.get("by_status", {}).items()):
        typer.echo(f"  {st:<16s} {cnt:>8d}")
    size_bytes = stats.get("total_size_bytes", 0) or 0
    if size_bytes >= 1_073_741_824:
        size_str = f"{size_bytes / 1_073_741_824:.1f} GB"
    elif size_bytes >= 1_048_576:
        size_str = f"{size_bytes / 1_048_576:.1f} MB"
    else:
        size_str = f"{size_bytes / 1024:.1f} KB"
    typer.echo(f"  {'total size':<16s} {size_str}")
    typer.echo("")

    # -- Files by format --
    typer.echo("Files by format:")
    if formats:
        for ext, cnt in formats:
            typer.echo(f"  {ext:<16s} {cnt:>8d}")
    else:
        typer.echo("  (none)")
    typer.echo("")

    # -- Scan health trend --
    if completed:
        typer.echo("Scan health (last 5 completed):")
        for s in completed:
            date = (s["finished_at"] or "")[:10]
            typer.echo(
                f"  {date}  {s['total']:>5d} total  {s['corrupt']:>3d} corrupt  {s['unreadable']:>3d} unreadable"
            )
        incomplete = [s for s in scans if not s["finished_at"]]
        if incomplete:
            typer.echo(f"  ({len(incomplete)} incomplete scan(s) not shown)")
    else:
        typer.echo("Scan health:  No completed scans yet.")
    typer.echo("")

    # -- Stale files --
    typer.echo(f"Stale files (>{threshold} days without check):  {stale}")
    typer.echo("")

    # -- Top errors --
    if errors:
        typer.echo("Top errors:")
        for msg, cnt in errors:
            truncated = msg[:60] + "…" if len(msg) > 60 else msg
            typer.echo(f"  {truncated:<62s} ({cnt})")



# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

@app.command()
def reset(
    path: Optional[str] = typer.Argument(None, help="Clear DB entries for this path (default: all)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Clear DB entries to force rescan."""
    if not yes:
        msg = f"Delete all DB records for {path!r}?" if path else "Delete ALL DB records?"
        typer.confirm(msg, abort=True)
    cfg = _load_cfg(config)
    db = _open_db(cfg)
    count = db.reset(path_prefix=path)
    typer.echo(f"Deleted {count} record(s).")


# ---------------------------------------------------------------------------
# check-deps
# ---------------------------------------------------------------------------

@app.command("check-deps")
def check_deps() -> None:
    """Print dependency matrix with versions and hard/soft status."""
    from .validators import all_validators

    typer.echo(f"{'Tool':<20} {'Status':<12} {'Required':<10} {'Version'}")
    typer.echo("-" * 64)

    seen: set[str] = set()
    any_missing_required = False
    for validator_cls in all_validators():
        instance = validator_cls()
        for rep in instance.check_dependencies():
            if rep.name in seen:
                continue
            seen.add(rep.name)
            status_str = "✓ found" if rep.available else "✗ missing"
            req_str = "hard" if rep.required else "soft"
            ver_str = rep.version or "—"
            typer.echo(f"{rep.name:<20} {status_str:<12} {req_str:<10} {ver_str}")
            if rep.required and not rep.available:
                any_missing_required = True

    if any_missing_required:
        typer.echo("\n[ERROR] Required dependencies missing.", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# db-export
# ---------------------------------------------------------------------------

@app.command("db-export")
def db_export(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file (default: stdout)"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Export DB as portable JSON."""
    cfg = _load_cfg(config)
    db = _open_db(cfg)
    records = db.export_json()
    payload = json.dumps(records, indent=2)
    if output:
        from pathlib import Path
        Path(output).write_text(payload)
        typer.echo(f"Exported {len(records)} records to {output}")
    else:
        typer.echo(payload)


# ---------------------------------------------------------------------------
# db-import
# ---------------------------------------------------------------------------

@app.command("db-import")
def db_import(
    input_file: Optional[str] = typer.Option(None, "--input", "-i", help="Input JSON file"),
    force: bool = typer.Option(False, "--force", help="Imported record wins on conflict"),
    path_map: Optional[str] = typer.Option(None, "--path-map", help="Remap path prefix: old:new"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Import DB from portable JSON."""
    cfg = _load_cfg(config)
    db = _open_db(cfg)

    if input_file:
        from pathlib import Path
        raw = Path(input_file).read_text()
    else:
        raw = sys.stdin.read()

    try:
        records = json.loads(raw)
    except json.JSONDecodeError as e:
        typer.echo(f"Invalid JSON: {e}", err=True)
        raise typer.Exit(1)

    pmap: dict[str, str] | None = None
    if path_map:
        parts = path_map.split(":", 1)
        if len(parts) != 2:
            typer.echo("--path-map must be old:new", err=True)
            raise typer.Exit(1)
        pmap = {parts[0]: parts[1]}

    inserted, skipped = db.import_json(records, force=force, path_map=pmap)
    typer.echo(f"Imported {inserted} records, skipped {skipped}.")


# ---------------------------------------------------------------------------
# verify-hashes
# ---------------------------------------------------------------------------

@app.command("verify-hashes")
def verify_hashes(
    path: Optional[str] = typer.Argument(None, help="Path to verify (default: all)"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Re-hash files and compare against stored hashes to detect at-rest corruption."""
    typer.echo("verify-hashes requires SDOG_ENABLE_HASH=true. Feature available in Stage 10+.")


if __name__ == "__main__":
    app()
