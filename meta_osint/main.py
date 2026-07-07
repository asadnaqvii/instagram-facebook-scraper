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

    result = run_sync(cfg)

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
    print(f"\nDashboard: http://localhost:{args.port}\n")
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

    # search = scrape with mode=search
    sp_search = sub.add_parser("search", help="Full keyword search: accounts + hashtags + posts")
    add_scrape_args(sp_search)
    sp_search.set_defaults(func=_cmd_scrape, mode="search")

    sp_scrape = sub.add_parser("scrape", help="Scrape with an explicit mode (search/profile/hashtag)")
    add_scrape_args(sp_scrape)
    sp_scrape.add_argument("--mode", choices=["search", "profile", "hashtag"], default="search")
    sp_scrape.set_defaults(func=_cmd_scrape)

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
