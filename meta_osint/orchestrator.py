"""Multi-keyword orchestration.

Takes a LIST of keywords and, for each, runs the requested search modes
across the requested platforms, persisting everything to the DB as it goes.
This is the top-level entry the CLI and dashboard call.

A single BrowserManager (one CDP connection) per platform is reused across
all keywords, and a single SelectorHealer is shared so a heal learned on the
first keyword benefits the rest of the run.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config
from .browser.cookies import export_cookies
from .browser.manager import BrowserManager, CDPConnectionError
from .database.db import PostDatabase
from .llm.healer import SelectorHealer
from .llm.ollama_client import OllamaClient
from .llm.selector_store import SelectorStore
from .models import Platform, SearchResult
from .pipeline.store import store_search_result
from .scraper import facebook as fb
from .scraper import instagram as ig
from .scraper import media as media_mod

ProgressFn = Callable[[str], None]
StopFn = Callable[[], bool]   # returns True when the user asked to stop


def _stopped(should_stop: Optional[StopFn]) -> bool:
    return bool(should_stop and should_stop())


@dataclass
class ScrapeConfig:
    keywords: list[str]
    platforms: list[str] = field(default_factory=lambda: list(config.PLATFORMS))
    mode: str = "search"          # 'search' | 'profile' | 'hashtag'
    max_posts: int = 15
    with_comments: bool = True
    analyze: bool = False


def _log(progress: Optional[ProgressFn], msg: str) -> None:
    if progress:
        progress(msg)
    else:
        print(msg)


async def _run_one(
    platform: str,
    keyword: str,
    mode: str,
    page,
    healer: SelectorHealer,
    cfg: ScrapeConfig,
    progress: Optional[ProgressFn] = None,
    known_urls: set | None = None,
) -> SearchResult:
    """Dispatch a single (platform, keyword) to the right extractor."""
    module = ig if platform == "instagram" else fb
    kw = keyword.strip()

    if mode == "profile":
        fn = module.scrape_profile if platform == "instagram" else module.scrape_page
        return await fn(page, kw.lstrip("@"), healer, cfg.max_posts, cfg.with_comments)
    if mode == "hashtag":
        return await module.scrape_hashtag(page, kw, healer, cfg.max_posts, cfg.with_comments)
    # default: full keyword search (streams per-post progress, delta-aware)
    return await module.search_keyword(
        page, kw, healer, cfg.max_posts, cfg.with_comments,
        progress=progress, known_urls=known_urls,
    )


async def scrape_platform(
    platform: str,
    cfg: ScrapeConfig,
    db: PostDatabase,
    healer: SelectorHealer,
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[StopFn] = None,
) -> list[dict]:
    """Run every keyword for one platform on a single CDP connection."""
    summaries: list[dict] = []
    if _stopped(should_stop):
        return summaries
    try:
        manager = await BrowserManager(platform).start()
    except CDPConnectionError as e:
        _log(progress, f"[{platform}] SKIPPED — {e}")
        for kw in cfg.keywords:
            summaries.append({"platform": platform, "keyword": kw, "error": "cdp_unavailable"})
        return summaries

    try:
        page = await manager.new_page()

        # Export authenticated cookies so yt-dlp can read gated content
        # (essential for Instagram — without the session cookie yt-dlp gets
        # nothing, which is why IG posts came back empty otherwise).
        cookie_path = await export_cookies(manager.context, platform)
        media_mod.set_cookie_file(platform, cookie_path)
        if cookie_path:
            _log(progress, f"[{platform}] cookies exported for yt-dlp")
        else:
            _log(progress, f"[{platform}] WARNING: no cookies exported — logged in?")

        # Load every post URL we already have for this platform once, so each
        # keyword can skip-and-refresh known posts (delta scraping). The set is
        # updated after each keyword so posts found earlier this run aren't
        # re-fetched by a later keyword.
        known_urls = db.known_post_urls(platform)

        for kw in cfg.keywords:
            if _stopped(should_stop):
                _log(progress, f"[{platform}] stopped by user — {len(summaries)} keyword(s) done, remaining skipped")
                break
            _log(progress, f"[{platform}] {cfg.mode}: {kw!r} ...")
            run_id = db.start_run(kw, platform, _now_iso())
            result = None
            # Attempt the keyword; if the page has died (crashed tab, hung nav),
            # recover with a fresh page and retry once. This is what stops one
            # keyword's transient failure from silently skipping the rest —
            # exactly the "only Facebook ran" symptom.
            for attempt in (1, 2):
                try:
                    if page.is_closed():
                        raise RuntimeError("page was closed")
                    result = await _run_one(platform, kw, cfg.mode, page, healer, cfg, progress, known_urls)
                    break
                except Exception as e:  # noqa: BLE001 — never let one keyword kill the batch
                    _log(progress, f"[{platform}] {kw!r} attempt {attempt} failed: {str(e)[:120]}")
                    if attempt == 1:
                        # Try to revive the browser page for the retry + next keywords.
                        # Close the dead one first — otherwise every failed keyword
                        # orphans a tab in Chrome for the rest of the run (a real
                        # memory leak when many keywords fail).
                        try:
                            await manager.close_page(page)
                        except Exception:
                            pass
                        try:
                            page = await manager.new_page()
                        except Exception as e2:
                            _log(progress, f"[{platform}] could not open a fresh page: {str(e2)[:80]}")
                    else:
                        result = SearchResult(platform=Platform(platform), keyword=kw, error=f"crashed: {e}")
                        result.finished_at = _now_iso()
            if result is None:
                result = SearchResult(platform=Platform(platform), keyword=kw, error="crashed: unknown")
                result.finished_at = _now_iso()

            counts = store_search_result(db, result, healer=healer, run_id=run_id, analyze=cfg.analyze)

            # Apply cheap engagement refresh for re-encountered posts, and link
            # them to this keyword (classification), without re-storing them.
            refreshed_n = 0
            for r in result.refreshed:
                if db.refresh_engagement(
                    platform, r["post_url"],
                    likes=r.get("likes"), comments_count=r.get("comments_count"),
                    shares=r.get("shares"), views=r.get("views"),
                ):
                    refreshed_n += 1
                db.link_existing_post(platform, r["post_url"], kw, run_id)

            # New posts this run become "known" for subsequent keywords.
            for p in result.posts:
                if p.post_url:
                    known_urls.add(p.post_url)

            db.finish_run(run_id, result)
            summary = {**result.summary(), "stored": counts, "refreshed": refreshed_n}
            summaries.append(summary)
            _log(
                progress,
                f"[{platform}] {kw!r}: {counts['posts']} new posts, {refreshed_n} refreshed, "
                f"{counts['accounts']} accounts, {counts['hashtags']} hashtags"
                + (f" (error: {result.error})" if result.error else ""),
            )
    finally:
        await manager.close()

    return summaries


async def run(cfg: ScrapeConfig, progress: Optional[ProgressFn] = None,
              should_stop: Optional[StopFn] = None) -> dict:
    """Top-level: scrape all keywords across all requested platforms.

    Platforms run CONCURRENTLY — Instagram and Facebook are independent CDP
    connections / browsers, so scraping both at once roughly halves wall-clock
    with no added detection risk (each platform stays human-paced internally).
    Each platform uses its own SQLite connection; WAL mode handles the
    concurrent writers safely.

    `should_stop` is a callback checked at each keyword boundary; when it
    returns True the run winds down gracefully (finishes the current keyword,
    skips the rest) rather than hard-killing mid-navigation.
    """
    store = SelectorStore()
    client = OllamaClient()
    healer = SelectorHealer(store=store, client=client)

    if config.LLM_ENABLED:
        if client.is_available():
            _log(progress, f"[llm] Ollama up ({client.model}); self-healing + analysis enabled")
        else:
            _log(progress, "[llm] Ollama unreachable — using heuristic selectors, skipping analysis")

    platforms = [p for p in cfg.platforms if p in config.PLATFORMS]
    for bad in (set(cfg.platforms) - set(platforms)):
        _log(progress, f"[warn] unknown platform {bad!r}, skipping")

    # Fan out: one task per platform, each with its own DB connection.
    async def _platform_task(platform: str) -> list[dict]:
        with PostDatabase(config.DB_PATH) as db:
            return await scrape_platform(platform, cfg, db, healer, progress, should_stop)

    results = await asyncio.gather(
        *(_platform_task(p) for p in platforms), return_exceptions=True
    )

    all_summaries: list[dict] = []
    for platform, res in zip(platforms, results):
        if isinstance(res, Exception):
            _log(progress, f"[{platform}] FAILED: {res}")
            all_summaries.append({"platform": platform, "error": f"platform_crashed: {res}"})
        else:
            all_summaries.extend(res)

    with PostDatabase(config.DB_PATH) as db:
        stats = db.get_stats()

    return {"summaries": all_summaries, "db_stats": stats}


def run_sync(cfg: ScrapeConfig, progress: Optional[ProgressFn] = None,
             should_stop: Optional[StopFn] = None) -> dict:
    return asyncio.run(run(cfg, progress, should_stop))


def _now_iso() -> str:
    from .scraper.helpers import now_iso

    return now_iso()
