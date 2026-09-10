#!/usr/bin/env python3
"""meta_osint CLI — keyword-driven Instagram & Facebook OSINT scraper.

Examples:
    # Full keyword search (accounts + hashtags + posts) on both platforms
    python -m meta_osint.main search -k "climate change" "renewable energy"

    # Keywords from a file, Instagram only, 30 posts each, no comments
    python -m meta_osint.main search -f keywords.txt -p instagram -n 30 --no-comments

    # Scrape specific accounts/pages
    python -m meta_osint.main scrape --mode profile -k natgeo nasa -p instagram

    # Scrape a hashtag feed
    python -m meta_osint.main scrape --mode hashtag -k nuclear -p facebook

    # Category batch runs (the cron entry point)
    python -m meta_osint.main categories --seed          # load the built-in taxonomy
    python -m meta_osint.main categories                 # list them with yields
    python -m meta_osint.main batch --dry-run            # preview the keyword order
    python -m meta_osint.main batch -c e p --since 1 -n 20   # daily run of two categories

    # DB stats / environment check / dashboard
    python -m meta_osint.main stats
    python -m meta_osint.main diagnose
    python -m meta_osint.main serve
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a script (python meta_osint/main.py) as well as a module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to a legacy codepage (cp1252) that can't encode the
# em-dashes / emoji we print, raising UnicodeEncodeError mid-run. Reconfigure
# stdio to UTF-8 with a safe fallback so output never crashes the scraper.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from meta_osint import config
from meta_osint.orchestrator import ScrapeConfig, run_sync


def _load_keywords(args) -> list[str]:
    keywords: list[str] = list(args.keywords or [])
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"Keyword file not found: {path}", file=sys.stderr)
            sys.exit(2)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keywords.append(line)
    # De-dup, keep order.
    seen: dict[str, None] = {}
    for k in keywords:
        seen.setdefault(k, None)
    return list(seen.keys())


def _cmd_scrape(args) -> None:
    keywords = _load_keywords(args)
    if not keywords:
        print("No keywords provided. Use -k/--keywords or -f/--file.", file=sys.stderr)
        sys.exit(2)

    # --sort / --since drive the same config the scrapers read, so a cron
    # entry needs no .env edits.
    if getattr(args, 'sort', None):
        config.SORT_MODE = args.sort
    if getattr(args, 'since', None):
        config.FRESHNESS_DAYS = args.since
    platforms = [args.platform] if args.platform else list(config.PLATFORMS)
    cfg = ScrapeConfig(
        keywords=keywords,
        platforms=platforms,
        mode=args.mode,
        max_posts=args.max_posts,
        with_comments=not args.no_comments,
        analyze=args.analyze,
    )

    print(f"\n{'='*64}")
    print(f"  meta_osint — mode={cfg.mode}  platforms={','.join(platforms)}")
    print(f"  keywords ({len(keywords)}): {', '.join(keywords[:8])}{' ...' if len(keywords) > 8 else ''}")
    print(f"{'='*64}\n")

    # Stream the scrapers' per-surface / per-post progress lines to the
    # terminal. Without a callback the CLI printed only the final summary,
    # hiding exactly the diagnostics that explain a low yield.
    result = run_sync(cfg, progress=lambda m: print(m, flush=True))

    print(f"\n{'='*64}\n  DONE")
    for s in result["summaries"]:
        line = f"  [{s['platform']}] {s['keyword']!r}: "
        if s.get("stored"):
            st = s["stored"]
            line += f"{st['posts']} posts, {st['accounts']} accounts, {st['hashtags']} hashtags"
        if s.get("error"):
            line += f"  (error: {s['error']})"
        print(line)
    print(f"\n  DB totals: {result['db_stats']}")
    print(f"  Database: {config.DB_PATH}\n")


def _cmd_categories(args) -> None:
    """List / seed / enable / disable keyword categories."""
    from meta_osint.database.db import PostDatabase
    from meta_osint.database.seed_categories import load_seed_categories

    with PostDatabase(config.DB_PATH) as db:
        if args.seed:
            stats = load_seed_categories(db, overwrite=args.overwrite)
            print(f"\nSeeded categories: {stats['created']} created, "
                  f"{stats['updated']} updated, {stats['skipped']} left alone "
                  f"({stats['keywords']} keywords bound)")
            if stats["skipped"] and not args.overwrite:
                print("  (existing categories kept as-is; use --overwrite to reset them)")
        if args.enable or args.disable:
            for code in (args.enable or []):
                _set_enabled_by_code(db, code, True)
            for code in (args.disable or []):
                _set_enabled_by_code(db, code, False)

        rows = db.get_category_stats()
        if not rows:
            print("\nNo categories yet. Seed the built-in set with:")
            print("  python -m meta_osint.main categories --seed\n")
            return
        print(f"\nCategories ({len(rows)})")
        print("-" * 78)
        print(f"  {'code':<5} {'wt':<4} {'name':<44} {'kws':>4} {'posts':>6}  on")
        for r in rows:
            mark = "yes" if r["enabled"] else " no"
            print(f"  {(r['code'] or ''):<5} {(r['weight'] or ''):<4} "
                  f"{r['name'][:44]:<44} {r['keyword_count']:>4} {r['posts']:>6}  {mark}")
        enabled = [r for r in rows if r["enabled"]]
        total_kw = len(db.batch_keywords())
        print("-" * 78)
        print(f"  {len(enabled)} enabled -> {total_kw} unique keywords in a full batch")
        if args.show:
            for r in rows:
                if args.show not in (r["code"], "all"):
                    continue
                kws = db.get_category_keywords(r["id"])
                print(f"\n  [{r['code']}] {r['name']} ({r['weight']}) - {len(kws)} keywords:")
                for kw in kws:
                    print(f"      {kw}")
        print()


def _set_enabled_by_code(db, code: str, enabled: bool) -> None:
    match = [c for c in db.get_categories() if c.get("code") == code or c["name"] == code]
    if not match:
        print(f"  ! no category with code/name {code!r}", file=sys.stderr)
        return
    db.set_category_enabled(match[0]["id"], enabled)
    print(f"  {'enabled' if enabled else 'disabled'}: [{match[0].get('code')}] {match[0]['name']}")


def _cmd_batch(args) -> None:
    """Run every keyword in the selected categories, newest-first.

    This is the cron entry point: it expands categories into their keywords
    (W3 first, de-duplicated) and hands them to the ordinary scrape pipeline.
    """
    from meta_osint.database.db import PostDatabase

    with PostDatabase(config.DB_PATH) as db:
        cat_ids = None
        if args.category:
            wanted, missing = [], []
            for token in args.category:
                hit = [c for c in db.get_categories()
                       if c.get("code") == token or c["name"] == token]
                (wanted.append(hit[0]["id"]) if hit else missing.append(token))
            if missing:
                print(f"Unknown category code/name: {', '.join(missing)}", file=sys.stderr)
                print("List them with: python -m meta_osint.main categories", file=sys.stderr)
                sys.exit(2)
            cat_ids = wanted
        keywords = db.batch_keywords(cat_ids, enabled_only=not args.include_disabled)
        cats = db.get_categories(enabled_only=not args.include_disabled)
        if cat_ids:
            cats = [c for c in cats if c["id"] in set(cat_ids)]

    if not keywords:
        print("No keywords to run. Seed or enable a category first:", file=sys.stderr)
        print("  python -m meta_osint.main categories --seed", file=sys.stderr)
        sys.exit(2)

    if args.limit:
        keywords = keywords[: args.limit]

    if args.dry_run:
        print(f"\nBatch would run {len(keywords)} keyword(s) from "
              f"{len(cats)} category(ies), in this order:\n")
        for i, kw in enumerate(keywords, 1):
            print(f"  {i:>3}. {kw}")
        est = len(keywords) * len(args.platform.split(",") if args.platform else config.PLATFORMS)
        print(f"\n  {est} keyword-platform passes. At ~{args.max_posts} posts each "
              f"that is up to {est * args.max_posts} posts.\n")
        return

    # Batch runs are for periodic collection, so default to newest-first.
    config.SORT_MODE = args.sort or "recent"
    if args.since:
        config.FRESHNESS_DAYS = args.since

    platforms = args.platform.split(",") if args.platform else list(config.PLATFORMS)
    cfg = ScrapeConfig(
        keywords=keywords,
        platforms=platforms,
        mode="search",
        max_posts=args.max_posts,
        with_comments=not args.no_comments,
        analyze=args.analyze,
    )
    print(f"\n{'='*64}")
    print(f"  meta_osint BATCH - {len(cats)} category(ies), {len(keywords)} keywords")
    for c in cats:
        print(f"    [{c.get('code')}] {c['weight']}  {c['name'][:46]} ({c['keyword_count']} kws)")
    print(f"  platforms={','.join(platforms)}  sort={config.SORT_MODE}"
          + (f"  since={config.FRESHNESS_DAYS}d" if config.FRESHNESS_DAYS else ""))
    print(f"{'='*64}\n")

    result = run_sync(cfg, progress=lambda m: print(m, flush=True))

    print(f"\n{'='*64}\n  BATCH DONE")
    total = 0
    for s in result["summaries"]:
        if s.get("stored"):
            total += s["stored"]["posts"]
    print(f"  {total} new posts across {len(keywords)} keywords")
    print(f"  DB totals: {result['db_stats']}\n")


def _cmd_stats(args) -> None:
    from meta_osint.database.db import PostDatabase

    with PostDatabase(config.DB_PATH) as db:
        stats = db.get_stats()
        keywords = db.get_keywords()
    print("\nDatabase statistics")
    print("-" * 40)
    for k, v in stats.items():
        print(f"  {k:<14}: {v}")
    if keywords:
        print("\nKeywords tracked:")
        for kw in keywords:
            print(f"  {kw['keyword']:<24} posts={kw['posts'] or 0} accounts={kw['accounts'] or 0} hashtags={kw['hashtags'] or 0}")
    print()


def _cmd_diagnose(args) -> None:
    """Check the environment: Ollama, yt-dlp, Chrome CDP endpoints."""
    import shutil
    import socket

    from meta_osint.llm.ollama_client import OllamaClient

    print("\nmeta_osint environment check")
    print("-" * 40)

    # yt-dlp
    ytdlp = shutil.which("yt-dlp")
    print(f"  yt-dlp           : {'found at ' + ytdlp if ytdlp else 'NOT FOUND (pip install yt-dlp)'}")

    # Ollama
    client = OllamaClient()
    if client.is_available(force=True):
        models = client.available_models()
        star = "*" if client.model in models else " "
        print(f"  Ollama           : up at {client.url}")
        print(f"  Ollama model     : {client.model} {'(available)' if client.model in models else '(NOT pulled — run: ollama pull ' + client.model + ')'}")
        if models:
            print(f"  models present   : {', '.join(models[:6])}")
    else:
        print(f"  Ollama           : NOT reachable at {client.url} (self-healing + analysis will be skipped)")

    # CDP ports
    for platform in config.PLATFORMS:
        port = config.CDP_PORT_INSTAGRAM if platform == "instagram" else config.CDP_PORT_FACEBOOK
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        reachable = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
        hint = "" if reachable else f"  (start: scripts/start_chrome_{platform}.bat)"
        print(f"  Chrome CDP {platform:<9}: {'reachable on ' + str(port) if reachable else 'not running on ' + str(port)}{hint}")
    print()


def _cmd_serve(args) -> None:
    from meta_osint.web.app import create_app

    app = create_app()
    if config.DB_BACKEND == "mysql":
        backend = f"MySQL @ {config.MYSQL_HOST}/{config.MYSQL_DB}"
    else:
        backend = f"SQLite @ {config.DB_PATH}"
    print(f"\nDashboard: http://localhost:{args.port}")
    print(f"Database:  {backend}\n")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meta_osint", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")

    def add_scrape_args(sp):
        sp.add_argument("-k", "--keywords", nargs="*", help="One or more keywords/targets")
        sp.add_argument("-f", "--file", help="File with one keyword per line (# comments allowed)")
        sp.add_argument("-p", "--platform", choices=list(config.PLATFORMS), help="Limit to one platform (default: both)")
        sp.add_argument("-n", "--max-posts", type=int, default=15, help="Max posts per keyword per platform (default 15)")
        sp.add_argument("--no-comments", action="store_true", help="Skip comment extraction (faster)")
        sp.add_argument("--analyze", action="store_true", help="Enable LLM content analysis (sentiment/entities/topics) — slower")
        sp.add_argument("--sort", choices=["recent", "top"], help="recent = newest-first collection (for cron); top = platform ranking")
        sp.add_argument("--since", metavar="DAYS", type=int, help="Only keep posts newer than DAYS (e.g. --since 1 for a daily cron)")

    # search = scrape with mode=search
    sp_search = sub.add_parser("search", help="Full keyword search: accounts + hashtags + posts")
    add_scrape_args(sp_search)
    sp_search.set_defaults(func=_cmd_scrape, mode="search")

    sp_scrape = sub.add_parser("scrape", help="Scrape with an explicit mode (search/profile/hashtag)")
    add_scrape_args(sp_scrape)
    sp_scrape.add_argument("--mode", choices=["search", "profile", "hashtag"], default="search")
    sp_scrape.set_defaults(func=_cmd_scrape)

    sp_cats = sub.add_parser("categories", help="List / seed / enable keyword categories")
    sp_cats.add_argument("--seed", action="store_true",
                         help="Load the built-in strategic taxonomy (16 categories, 335 keywords)")
    sp_cats.add_argument("--overwrite", action="store_true",
                         help="With --seed: reset existing seed categories to the shipped keyword lists")
    sp_cats.add_argument("--enable", nargs="*", metavar="CODE", help="Enable categories by code or name")
    sp_cats.add_argument("--disable", nargs="*", metavar="CODE", help="Disable categories by code or name")
    sp_cats.add_argument("--show", metavar="CODE", help="Print the keywords of one category (or 'all')")
    sp_cats.set_defaults(func=_cmd_categories)

    sp_batch = sub.add_parser(
        "batch", help="Run all keywords in one or more categories (the cron entry point)")
    sp_batch.add_argument("-c", "--category", nargs="*", metavar="CODE",
                          help="Category codes/names to run (default: every enabled category)")
    sp_batch.add_argument("-p", "--platform", help="Comma-separated platforms (default: both)")
    sp_batch.add_argument("-n", "--max-posts", type=int, default=15,
                          help="Max posts per keyword per platform (default 15)")
    sp_batch.add_argument("--sort", choices=["recent", "top"], default="recent",
                          help="Collection order (default: recent - newest first)")
    sp_batch.add_argument("--since", metavar="DAYS", type=int,
                          help="Only keep posts newer than DAYS (e.g. --since 1 for a daily cron)")
    sp_batch.add_argument("--limit", type=int, metavar="N",
                          help="Run only the first N keywords (useful for a short cron window)")
    sp_batch.add_argument("--include-disabled", action="store_true",
                          help="Also run categories marked disabled")
    sp_batch.add_argument("--no-comments", action="store_true", help="Skip comment extraction (faster)")
    sp_batch.add_argument("--analyze", action="store_true", help="Enable LLM content analysis")
    sp_batch.add_argument("--dry-run", action="store_true",
                          help="Print the keyword list and exit without scraping")
    sp_batch.set_defaults(func=_cmd_batch)

    sp_stats = sub.add_parser("stats", help="Show database statistics")
    sp_stats.set_defaults(func=_cmd_stats)

    sp_diag = sub.add_parser("diagnose", help="Check Ollama / yt-dlp / Chrome CDP")
    sp_diag.set_defaults(func=_cmd_diagnose)

    sp_serve = sub.add_parser("serve", help="Start the web dashboard")
    sp_serve.add_argument("--port", type=int, default=5000)
    sp_serve.add_argument("--debug", action="store_true")
    sp_serve.set_defaults(func=_cmd_serve)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        sys.exit(1)
    # search subcommand hard-sets mode via defaults; scrape reads --mode.
    if not hasattr(args, "mode"):
        args.mode = "search"
    args.func(args)


if __name__ == "__main__":
    main()
