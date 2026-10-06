"""Command line interface: ``reddit-scout <command>``."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .classify import classify_all
from .config import ConfigError, load_config
from .normalize import FormatError
from .obsidian import export
from .report import coverage, render_text
from .retention import expired_ids, purge_expired, purge_ids
from .sources import local
from .storage import Store

EXIT_CONFIG, EXIT_INTERRUPTED = 2, 4


def _cfg(args):
    overrides = {k: getattr(args, k, None) for k in
                 ("subreddit", "start", "end", "comments_start", "comments_end", "db_path", "vault_path")}
    path = args.config
    if path is None and Path("config.toml").exists():
        path = "config.toml"
    return load_config(path, overrides=overrides)


def _hours(days: int) -> int | None:
    return days * 24 if days and days > 0 else None


def cmd_import(args):
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    days = 0 if args.no_expiry else (args.max_days if args.max_days is not None
                                      else cfg.retention.max_days_since_check)
    stats = local.import_paths(
        store, cfg, [Path(p) for p in args.paths], source_id=args.source_id,
        description=args.description or f"local files: {', '.join(args.paths)}",
        basis=args.basis, max_hours_since_check=_hours(days), force=args.force,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


def cmd_classify(args):
    cfg = _cfg(args)
    print(json.dumps(classify_all(Store(cfg.db_path), cfg), indent=1))
    return 0


def cmd_export(args):
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    res = export(store, cfg, Path(cfg.obsidian.vault_path) if cfg.obsidian.vault_path else None)
    print(f"written {len(res.written)}, unchanged {len(res.unchanged)}, removed {len(res.removed)}, "
          f"kept with removal notice {len(res.kept_with_notice)}")
    for w in res.warnings:
        print(f"warning: {w}")
    return 0


def cmd_report(args):
    cfg = _cfg(args)
    cov = coverage(Store(cfg.db_path), cfg)
    if args.json:
        cov.pop("post_ids")
        print(json.dumps(cov, ensure_ascii=False, indent=1))
    else:
        print(render_text(cov))
    return 0


def cmd_purge(args):
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    n = 0
    if args.expired:
        if args.dry_run:
            print(f"would purge {len(expired_ids(store, subreddit=cfg.subreddit))} expired item(s)")
            return 0
        n += purge_expired(store, subreddit=cfg.subreddit)
    ids = list(args.id or [])
    if args.ids_file:
        ids += Path(args.ids_file).read_text(encoding="utf-8").split()
    if ids:
        n += purge_ids(store, ids, args.reason)
    print(f"purged {n} item(s). Run `reddit-scout export` to remove them from the vault.")
    return 0


def cmd_search(args):
    cfg = _cfg(args)
    for row in Store(cfg.db_path).search(args.query, args.limit):
        print(f"{row['thing_id']} ({row['post_id']}): {row['snip']}")
    return 0


def cmd_run(args):
    """Full local pipeline: [import] -> purge expired -> classify -> export."""
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    if args.paths:
        if not args.basis:
            raise ConfigError("--basis is required when importing files")
        local.import_paths(store, cfg, [Path(p) for p in args.paths], source_id=args.source_id,
                           description=f"local files: {', '.join(args.paths)}", basis=args.basis,
                           max_hours_since_check=_hours(cfg.retention.max_days_since_check))
    n = purge_expired(store, subreddit=cfg.subreddit)
    if n:
        print(f"purged {n} expired item(s)")
    print(json.dumps(classify_all(store, cfg)))
    return cmd_export(args)


def cmd_demo(args):
    """Build the demo vault from synthetic data."""
    from .synthetic import DEMO_PERIOD, SUBREDDIT, demo_bundle

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="reddit-scout-demo-"))
    try:
        data = work / "demo.json"
        data.write_text(json.dumps(demo_bundle(), ensure_ascii=False), encoding="utf-8")
        example_cfg = Path(__file__).resolve().parents[2] / "examples" / "demo-config.toml"
        overrides = {"subreddit": SUBREDDIT, "start": DEMO_PERIOD[0], "end": DEMO_PERIOD[1],
                     "db_path": str(work / "demo.sqlite3"), "vault_path": str(out)}
        cfg = load_config(example_cfg if example_cfg.exists() else None, overrides=overrides)
        store = Store(cfg.db_path)
        local.import_paths(store, cfg, [data], source_id="synthetic", kind="synthetic",
                           description="synthetic demo data (invented)",
                           basis="synthetic data created for the demo; no third-party rights involved",
                           max_hours_since_check=None, log=lambda m: None)
        html_dir = Path(__file__).resolve().parents[2] / "examples" / "saved-pages"
        if html_dir.exists():
            local.import_paths(store, cfg, [html_dir], source_id="synthetic", kind="synthetic",
                               description="synthetic demo data (invented)",
                               basis="synthetic data created for the demo; no third-party rights involved",
                               max_hours_since_check=None, log=lambda m: None)
        classify_all(store, cfg)
        res = export(store, cfg, out)
        store.close()
        print(f"demo vault written to {out} ({len(res.written)} files written)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reddit-scout", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="TOML config (default: ./config.toml if present)")
    common.add_argument("--subreddit")
    common.add_argument("--start", help="period start YYYY-MM-DD (default: 12 months ago)")
    common.add_argument("--end", help="period end YYYY-MM-DD, inclusive (default: yesterday)")
    common.add_argument("--comments-start", dest="comments_start")
    common.add_argument("--comments-end", dest="comments_end")
    common.add_argument("--db", dest="db_path")
    common.add_argument("--vault", dest="vault_path")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("import", parents=[common],
                       help="import local files: Reddit JSON and (optional) manually saved thread pages .html/.mhtml")
    s.add_argument("paths", nargs="+")
    s.add_argument("--basis", required=True, help="why you may process these files (stored as provenance)")
    s.add_argument("--source-id", default="local")
    s.add_argument("--description")
    s.add_argument("--max-days", type=int,
                   help="retention: purge items not seen in an import for N days (default from config, 0 = off)")
    s.add_argument("--no-expiry", action="store_true", help="no automatic expiry for this source")
    s.add_argument("--force", action="store_true", help="re-import files already imported")
    s.set_defaults(func=cmd_import)

    sub.add_parser("classify", parents=[common], help="classify and score comments locally"
                   ).set_defaults(func=cmd_classify)
    sub.add_parser("export", parents=[common], help="write Markdown notes to the Obsidian vault"
                   ).set_defaults(func=cmd_export)

    s = sub.add_parser("report", parents=[common], help="coverage report")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("purge", parents=[common], help="delete content (deleted at source, expired, by id)")
    s.add_argument("--id", action="append", help="fullname t1_.../t3_..., repeatable")
    s.add_argument("--ids-file", help="file with fullnames separated by whitespace")
    s.add_argument("--expired", action="store_true", help="purge items past their retention limit")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--reason", default="manual purge request")
    s.set_defaults(func=cmd_purge)

    s = sub.add_parser("search", parents=[common], help="full-text search in stored text")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("run", parents=[common], help="[import] -> purge expired -> classify -> export")
    s.add_argument("paths", nargs="*")
    s.add_argument("--basis")
    s.add_argument("--source-id", default="local")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("demo", help="build a demo vault from synthetic data")
    s.add_argument("--out", default="examples/demo-vault")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (FormatError, FileNotFoundError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt as exc:
        print(f"interrupted: {exc}. Progress is saved; run the same command again to resume.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
