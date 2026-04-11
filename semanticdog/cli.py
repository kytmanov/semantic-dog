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

_DEFAULT_CONFIG = "/data/config/config.yaml"


def _load_cfg(config_path: str | None = None):
    from .config import load_config
    try:
        return load_config(config_path)
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
    """Validate files for semantic integrity."""
    cfg = _load_cfg(config)
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
    scanner = Scanner(cfg, db)
    typer.echo(f"Scanning {', '.join(cfg.paths)} ...")
    stats = scanner.scan()
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
    cfg = _load_cfg(config)
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
    typer.echo(f"{'ID':36s}  {'Started':26s}  {'Status':12s}  Files  Corrupt")
    typer.echo("-" * 90)
    for s in scans:
        status_str = "complete" if s["finished_at"] else "incomplete"
        typer.echo(
            f"{s['id']:36s}  {s['started_at'] or '':26s}  {status_str:12s}  "
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
    """Show validation report."""
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
# show-corrupt
# ---------------------------------------------------------------------------

@app.command("show-corrupt")
def show_corrupt(
    format: Optional[str] = typer.Option(None, "--format", help="Filter by extension e.g. cr2"),
    path: Optional[str] = typer.Option(None, "--path", help="Filter by path prefix"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """List all corrupt files."""
    cfg = _load_cfg(config)
    db = _open_db(cfg)
    rows = db.get_corrupt_files(ext=format, path_prefix=path)
    if not rows:
        typer.echo("No corrupt files found.")
        return
    for r in rows:
        err = f"  [{r['error']}]" if r.get("error") else ""
        typer.echo(f"{r['path']}{err}")


# ---------------------------------------------------------------------------
# show-stats
# ---------------------------------------------------------------------------

@app.command("show-stats")
def show_stats(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config YAML"),
) -> None:
    """Show aggregate statistics by format and status."""
    cfg = _load_cfg(config)
    db = _open_db(cfg)
    stats = db.get_stats()
    typer.echo(f"Total files indexed: {stats['total']}")
    typer.echo("")
    typer.echo("By status:")
    for st, cnt in sorted(stats.get("by_status", {}).items()):
        typer.echo(f"  {st:<16s} {cnt:>8d}")


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
