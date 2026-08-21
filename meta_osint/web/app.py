"""Minimal Flask dashboard for browsing scraped data and launching scrapes.

Kept intentionally small — it reads from the same PostDatabase the CLI writes
to, and launches scrape jobs in a background thread via the orchestrator.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from .. import config
from ..database.db import PostDatabase
from ..llm.ollama_client import OllamaClient
from ..llm.healer import SelectorHealer
from ..orchestrator import ScrapeConfig, run_sync
from ..scraper.helpers import keyword_relevancy

# Short-lived cache of the login-status check (it navigates the browsers, so
# we don't want to re-run it on every poll). {"ts": float, "data": dict}
_STATUS_CACHE: dict = {"ts": 0.0, "data": None}
_STATUS_TTL = 20.0  # seconds


def _media_name(local_path: str | None) -> str | None:
    """Basename of a downloaded media file, for the /media/<name> route."""
    if not local_path:
        return None
    return Path(local_path).name

# In-memory job registry (fine for a single-user local dashboard).
JOBS: dict[str, dict] = {}


def _run_job(job_id: str, cfg: ScrapeConfig) -> None:
    JOBS[job_id]["status"] = "running"
    lines: list[str] = JOBS[job_id]["log"]

    def progress(msg: str) -> None:
        lines.append(msg)

    def should_stop() -> bool:
        return JOBS.get(job_id, {}).get("cancel", False)

    try:
        result = run_sync(cfg, progress, should_stop)
        # If the user hit stop, mark it 'stopped' rather than 'done'.
        JOBS[job_id]["status"] = "stopped" if JOBS[job_id].get("cancel") else "done"
        JOBS[job_id]["result"] = result
    except Exception as e:  # noqa: BLE001
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)


# Upper bound on posts scored per enrich run — a safety cap, not a limit users
# should normally hit (the corpus is a few hundred posts).
_ENRICH_RUN_CAP = 2000
_ENRICH_BATCH = 50


def _run_enrich_job(job_id: str, rescore: bool = False) -> None:
    """On-demand AI enrichment pass over posts lacking a strategic score.

    Reuses the JOBS pattern (same progress log + cancel flag + /api/job
    endpoints as scraping). For each un-enriched post it asks the LLM for a
    semantic strategic-relevance score (against the saved strategic keywords)
    plus sentiment/entities, and writes the result back. Degrades safely: if
    Ollama is down it aborts cleanly without touching the DB; posts the LLM
    can't score are skipped (tracked in-memory) so the batch fetch can't loop.
    """
    job = JOBS[job_id]
    job["status"] = "running"
    lines: list[str] = job["log"]

    def log(msg: str) -> None:
        lines.append(msg)

    def should_stop() -> bool:
        return JOBS.get(job_id, {}).get("cancel", False)

    try:
        healer = SelectorHealer(client=OllamaClient())
        if not healer.client.is_available():
            log("Ollama is not available — cannot enrich. Start Ollama and retry.")
            job["status"] = "error"
            job["error"] = "Ollama unavailable"
            return

        with PostDatabase(config.DB_PATH) as db:
            if rescore:
                db.reset_strategic_scores()
                log("Cleared existing strategic scores — re-scoring all posts.")

            strat_kws = db.get_strategic_keywords()
            if strat_kws:
                log(f"Analysis lens: {', '.join(strat_kws)}")
            else:
                log("No strategic keywords defined — scoring generic relevance (0). "
                    "Add keywords on the Strategic Intelligence page for real scores.")

            total = db.count_posts_needing_analysis()
            log(f"{total} post(s) to enrich.")
            if total == 0:
                job["status"] = "done"
                job["result"] = {"enriched": 0, "skipped": 0}
                return

            enriched = 0
            skipped_ids: set[int] = set()
            processed = 0
            while processed < _ENRICH_RUN_CAP:
                if should_stop():
                    log("[stopped by user]")
                    break
                batch = db.get_posts_needing_analysis(
                    limit=_ENRICH_BATCH, exclude_ids=skipped_ids
                )
                if not batch:
                    break
                for row in batch:
                    if should_stop():
                        log("[stopped by user]")
                        break
                    processed += 1
                    context = {
                        "platform": row.get("platform", ""),
                        "author": row.get("author_username")
                        or row.get("author_display_name") or "",
                    }
                    analysis = healer.analyze_strategic(
                        row.get("text"), strat_kws, context
                    )
                    if analysis is None:
                        # LLM couldn't score this one. If Ollama went down
                        # mid-run, abort the whole pass (re-check); otherwise
                        # skip just this post so we don't refetch it forever.
                        if not healer.client.is_available():
                            log("Ollama went offline mid-run — aborting. "
                                f"{enriched} post(s) saved so far.")
                            job["status"] = "error"
                            job["error"] = "Ollama went offline"
                            return
                        skipped_ids.add(row["id"])
                        continue
                    db.update_post_analysis(row["id"], analysis)
                    enriched += 1
                    if enriched % 10 == 0 or enriched == 1:
                        rel = analysis.get("strategic_relevance")
                        log(f"  [{enriched}/{total}] scored post {row['id']} "
                            f"→ strategic {rel}")
                if should_stop():
                    break

            job["status"] = "stopped" if job.get("cancel") else "done"
            job["result"] = {"enriched": enriched, "skipped": len(skipped_ids)}
            log(f"Done — {enriched} enriched, {len(skipped_ids)} skipped.")
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)
        lines.append(f"Error: {e}")


def _attach_media_urls(posts: list[dict]) -> list[dict]:
    """Add browser-servable URLs for each downloaded media file + thumbnail."""
    for p in posts:
        for m in p.get("media", []):
            name = _media_name(m.get("local_path"))
            m["src"] = url_for("media", filename=name) if name else (m.get("url") or None)
            thumb_name = _media_name(m.get("local_thumbnail"))
            m["thumb_src"] = (
                url_for("media", filename=thumb_name) if thumb_name
                else (m.get("thumbnail_url") or m["src"])
            )
        p["images"] = [m for m in p.get("media", []) if m.get("type") == "image"]
        p["videos"] = [m for m in p.get("media", []) if m.get("type") in ("video", "reel")]
    return posts


def _attach_relevancy(posts: list[dict], ai_scores: dict | None = None) -> list[dict]:
    """Attach a per-post relevancy (0-100) + its source label.

    Prefers the stored AI strategic score ('AI') where a post has been
    enriched; otherwise falls back to the display-time NLP keyword match
    ('NLP'). `ai_scores` maps post_id -> {relevance, rationale}; when omitted
    the AI overlay is fetched from the row's own strategic_relevance column if
    present (top-strategic feed hydrates it directly)."""
    ai_scores = ai_scores or {}
    for p in posts:
        ai = ai_scores.get(p.get("id"))
        ai_rel = ai["relevance"] if ai else p.get("strategic_relevance")
        ai_rationale = ai["rationale"] if ai else p.get("strategic_rationale")
        if ai_rel is not None:
            p["relevancy"] = ai_rel
            p["relevancy_source"] = "AI"
            p["relevancy_rationale"] = ai_rationale
        else:
            p["relevancy"] = keyword_relevancy(
                p.get("text"),
                p.get("hashtags"),
                p.get("author_display_name") or p.get("author_username"),
                p.get("keywords"),
            )
            p["relevancy_source"] = "NLP"
            p["relevancy_rationale"] = None
    return posts


def _related_keywords(db, top: int = 10, per: int = 5, min_shared: int = 2) -> list[dict]:
    """Which keywords relate to each other, via entities they share in common
    (result_links). Case-normalised so 'Pakistan army'/'pakistan army' merge.
    Returns [{keyword, related: [{keyword, shared}, ...]}, ...] for the top
    keywords by total shared-entity strength. Read-only, small data."""
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT LOWER(TRIM(k1.keyword)) a, LOWER(TRIM(k2.keyword)) b, COUNT(*) shared
        FROM result_links r1
        JOIN result_links r2 ON r1.entity_type=r2.entity_type AND r1.entity_id=r2.entity_id
        JOIN keywords k1 ON k1.id=r1.keyword_id
        JOIN keywords k2 ON k2.id=r2.keyword_id
        WHERE LOWER(TRIM(k1.keyword)) < LOWER(TRIM(k2.keyword))
        GROUP BY a, b HAVING shared >= ? ORDER BY shared DESC
        """,
        (min_shared,),
    ).fetchall()
    if not rows:
        return []
    # Canonical display label per normalised keyword (a real keyword row so
    # /keyword/<name> links resolve).
    label = {r["norm"]: r["kw"] for r in conn.execute(
        "SELECT LOWER(TRIM(keyword)) norm, MIN(keyword) kw FROM keywords GROUP BY 1"
    )}
    # Build symmetric adjacency.
    adj: dict[str, list[tuple[str, int]]] = {}
    for r in rows:
        adj.setdefault(r["a"], []).append((r["b"], r["shared"]))
        adj.setdefault(r["b"], []).append((r["a"], r["shared"]))
    # Rank anchor keywords by total shared strength; take the top ones.
    ranked = sorted(adj.items(), key=lambda kv: sum(s for _, s in kv[1]), reverse=True)
    out = []
    for norm, sibs in ranked[:top]:
        sibs_sorted = sorted(sibs, key=lambda x: x[1], reverse=True)[:per]
        out.append({
            "keyword": label.get(norm, norm),
            "related": [{"keyword": label.get(n, n), "shared": s} for n, s in sibs_sorted],
        })
    return out


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "meta-osint-dev"
    # Pick up template edits without a server restart (local dashboard).
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    @app.route("/media/<path:filename>")
    def media(filename):
        """Serve a downloaded media file from the media dir (no path escape)."""
        target = (config.MEDIA_DIR / filename).resolve()
        if config.MEDIA_DIR.resolve() not in target.parents or not target.exists():
            abort(404)
        return send_file(str(target))

    @app.route("/")
    def index():
        with PostDatabase(config.DB_PATH) as db:
            stats = db.get_stats()
            keywords = db.get_keywords()
            ai_scores = db.get_strategic_scores()
            recent_posts = _attach_relevancy(
                _attach_media_urls(db.get_posts(limit=25)), ai_scores)
            related = _related_keywords(db)
        # Weight each keyword by how much data it surfaced, then bucket into
        # mosaic tiers (bigger tile = more data) like the design's keyword
        # mosaic. weight = posts*3 + accounts + hashtags (posts count most).
        for k in keywords:
            k["weight"] = (k.get("posts") or 0) * 3 + (k.get("accounts") or 0) + (k.get("hashtags") or 0)
        max_w = max((k["weight"] for k in keywords), default=1) or 1
        for k in keywords:
            r = k["weight"] / max_w
            k["tier"] = "xl" if r > 0.75 else "l" if r > 0.45 else "m" if r > 0.22 else "s"
            k["bar_pct"] = round(r * 100)
        keywords.sort(key=lambda k: k["weight"], reverse=True)
        return render_template("index.html", stats=stats, keywords=keywords,
                               related=related, posts=recent_posts, config=config.as_dict())

    @app.route("/posts")
    def posts():
        platform = request.args.get("platform") or None
        keyword = request.args.get("keyword") or None
        sort = request.args.get("sort") or "latest"
        # 'relevancy' is a display-time derived value, so fetch by a real sort
        # then re-order in Python.
        db_sort = "latest" if sort == "relevancy" else sort
        with PostDatabase(config.DB_PATH) as db:
            ai_scores = db.get_strategic_scores()
            rows = _attach_relevancy(_attach_media_urls(
                db.get_posts(platform=platform, keyword=keyword, sort=db_sort, limit=100)),
                ai_scores)
            all_keywords = [k["keyword"] for k in db.get_keywords()]
        if sort == "relevancy":
            rows.sort(key=lambda p: p.get("relevancy") or 0, reverse=True)
        return render_template("posts.html", posts=rows, platform=platform, keyword=keyword,
                               sort=sort, all_keywords=all_keywords)

    @app.route("/keyword/<path:keyword>")
    def keyword_detail(keyword):
        sort = request.args.get("sort") or "latest"
        db_sort = "latest" if sort == "relevancy" else sort
        with PostDatabase(config.DB_PATH) as db:
            detail = db.get_keyword_detail(keyword, sort=db_sort)
            ai_scores = db.get_strategic_scores()
            detail["posts"] = _attach_relevancy(
                _attach_media_urls(detail["posts"]), ai_scores)
        if sort == "relevancy":
            detail["posts"].sort(key=lambda p: p.get("relevancy") or 0, reverse=True)
        return render_template("keyword.html", d=detail, keyword=keyword, sort=sort)

    @app.route("/accounts")
    def accounts():
        platform = request.args.get("platform") or None
        keyword = request.args.get("keyword") or None
        with PostDatabase(config.DB_PATH) as db:
            rows = db.get_accounts(platform=platform, keyword=keyword, limit=300)
        return render_template("accounts.html", accounts=rows, platform=platform, keyword=keyword)

    @app.route("/hashtags")
    def hashtags():
        with PostDatabase(config.DB_PATH) as db:
            rows = db.get_hashtags(limit=200)
        return render_template("hashtags.html", hashtags=rows)

    # ── Strategic Intelligence (AI enrichment) ───────────────────────

    @app.route("/strategic")
    def strategic():
        from collections import Counter

        with PostDatabase(config.DB_PATH) as db:
            strat_kws = db.get_strategic_keywords()
            pending = db.count_posts_needing_analysis()
            summary = db.get_strategic_summary()
            leaderboard = db.get_strategic_leaderboard()
            timeline = db.get_strategic_timeline()
            sentiment = db.get_strategic_sentiment_breakdown()
            top_posts = _attach_relevancy(
                _attach_media_urls(db.get_top_strategic_posts(12)))

        # Themes + entities: count topics/orgs across the top strategic posts.
        theme_counts: Counter = Counter()
        entity_counts: Counter = Counter()
        for p in top_posts:
            for t in (p.get("topics") or []):
                theme_counts[str(t).strip().lower()] += 1
            for e in (p.get("entities_orgs") or []):
                entity_counts[str(e).strip()] += 1
        themes = theme_counts.most_common(18)
        entities = entity_counts.most_common(18)

        # A running enrich job (if any) so the page can show live progress.
        active_job = next(
            (j for j in JOBS.values()
             if j.get("kind") == "enrich" and j.get("status") in ("queued", "running")),
            None,
        )

        ollama_up = OllamaClient().is_available()
        return render_template(
            "strategic.html",
            strategic_keywords=strat_kws,
            pending=pending,
            summary=summary,
            leaderboard=leaderboard,
            timeline=timeline,
            sentiment=sentiment,
            top_posts=top_posts,
            themes=themes,
            entities=entities,
            ollama_up=ollama_up,
            active_job=active_job,
        )

    @app.route("/strategic/keyword/add", methods=["POST"])
    def strategic_keyword_add():
        raw = request.form.get("keyword", "")
        # Accept comma/newline-separated multiple entries.
        kws = [k.strip() for k in raw.replace(",", "\n").splitlines() if k.strip()]
        with PostDatabase(config.DB_PATH) as db:
            for k in kws:
                db.add_strategic_keyword(k)
        return redirect(url_for("strategic"))

    @app.route("/strategic/keyword/remove", methods=["POST"])
    def strategic_keyword_remove():
        kw = request.form.get("keyword", "").strip()
        if kw:
            with PostDatabase(config.DB_PATH) as db:
                db.remove_strategic_keyword(kw)
        return redirect(url_for("strategic"))

    @app.route("/enrich", methods=["POST"])
    def enrich():
        """Kick off an AI enrichment pass (background job, reuses job UI)."""
        # Don't stack enrich jobs.
        existing = next(
            (j for j in JOBS.values()
             if j.get("kind") == "enrich" and j.get("status") in ("queued", "running")),
            None,
        )
        if existing:
            return redirect(url_for("job_status", job_id=existing["id"]))

        rescore = request.form.get("rescore") == "1"
        job_id = uuid.uuid4().hex[:8]
        JOBS[job_id] = {
            "id": job_id, "kind": "enrich", "status": "queued", "log": [],
            "keywords": ["AI enrichment"], "started": time.time(),
        }
        threading.Thread(
            target=_run_enrich_job, args=(job_id, rescore), daemon=True
        ).start()
        return redirect(url_for("job_status", job_id=job_id))

    @app.route("/scrape", methods=["GET", "POST"])
    def scrape():
        if request.method == "POST":
            raw = request.form.get("keywords", "")
            keywords = [k.strip() for k in raw.replace(",", "\n").splitlines() if k.strip()]
            # Guard against an empty submit (e.g. the placeholder-only mistake):
            # don't create a no-op job — send the user back with an explanation.
            if not keywords:
                return render_template(
                    "scrape.html", platforms=config.PLATFORMS,
                    error="Please type at least one keyword. The grey example text is not submitted.",
                )
            platforms = request.form.getlist("platforms") or list(config.PLATFORMS)
            cfg = ScrapeConfig(
                keywords=keywords,
                platforms=platforms,
                mode=request.form.get("mode", "search"),
                max_posts=int(request.form.get("max_posts", 15)),
                with_comments=request.form.get("comments") == "on",
                analyze=request.form.get("analyze") == "on",
            )
            job_id = uuid.uuid4().hex[:8]
            JOBS[job_id] = {"id": job_id, "status": "queued", "log": [],
                            "keywords": keywords, "started": time.time()}
            threading.Thread(target=_run_job, args=(job_id, cfg), daemon=True).start()
            return redirect(url_for("job_status", job_id=job_id))
        return render_template("scrape.html", platforms=config.PLATFORMS)

    @app.route("/job/<job_id>")
    def job_status(job_id):
        job = JOBS.get(job_id)
        if not job:
            return redirect(url_for("scrape"))
        return render_template("job.html", job=job)

    @app.route("/api/job/<job_id>")
    def api_job(job_id):
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        return jsonify(job)

    @app.route("/api/job/<job_id>/stop", methods=["POST"])
    def api_job_stop(job_id):
        """Request a graceful stop: the scrape finishes the keyword it's on,
        then skips the rest. (Cooperative — avoids killing the browser
        mid-navigation.)"""
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        if job.get("status") in ("queued", "running"):
            job["cancel"] = True
            job.setdefault("log", []).append("[stop requested — finishing current keyword, then stopping…]")
            return jsonify({"ok": True, "status": "stopping"})
        return jsonify({"ok": False, "status": job.get("status")})

    @app.route("/api/jobs/stop-all", methods=["POST"])
    def api_jobs_stop_all():
        n = 0
        for j in JOBS.values():
            if j.get("status") in ("queued", "running"):
                j["cancel"] = True
                n += 1
        return jsonify({"ok": True, "stopping": n})

    @app.route("/api/jobs/active")
    def api_jobs_active():
        """Any scrape jobs still running — powers the header 'scraping…' badge
        so you can tell a scrape is going even when you've navigated away."""
        active = [
            {"id": j["id"],
             "last": (j.get("log") or ["starting…"])[-1],
             "keywords": j.get("keywords", [])}
            for j in JOBS.values()
            if j.get("status") in ("queued", "running")
        ]
        return jsonify({"active": active, "count": len(active)})

    @app.route("/api/login-status")
    def api_login_status():
        """Per-platform status.

        Default (what the dashboard polls): a CHEAP check — is each platform's
        Chrome running? This does NOT navigate the browser, so polling it is
        harmless. It reports chrome_up / chrome_down only.

        ?deep=1 (only on explicit user action, e.g. clicking "verify login"):
        additionally attaches to Chrome and checks the logged-in state. This
        drives the browser, so it is NEVER called on a timer.
        """
        deep = request.args.get("deep") == "1"
        if not deep:
            # Cheap, no browser navigation, no caching needed.
            try:
                from ..browser.status import get_status
                return jsonify({"platforms": get_status(deep=False), "cached": False})
            except Exception as e:  # noqa: BLE001
                return jsonify({"platforms": None, "error": str(e)}), 200

        # Deep check — don't run while a scrape has the browsers busy.
        if any(j.get("status") in ("queued", "running") for j in JOBS.values()):
            return jsonify({"platforms": _STATUS_CACHE.get("data"), "busy": True}), 200
        try:
            from ..browser.status import get_status
            data = get_status(deep=True)
            _STATUS_CACHE.update(ts=time.time(), data=data)
            return jsonify({"platforms": data, "cached": False, "deep": True})
        except Exception as e:  # noqa: BLE001
            return jsonify({"platforms": _STATUS_CACHE.get("data"), "error": str(e)}), 200

    @app.route("/login/<platform>")
    def login_action(platform):
        """Get the user to the platform's login page in its own Chrome window.

        Two cases, handled automatically:
          * Chrome already running (CDP port open) — just drive it to the login
            page (log out / switch account / re-login there).
          * Chrome NOT running — launch its start_chrome_<platform>.bat, which
            opens Chrome on the right CDP port already pointed at the site's
            login. No terminal needed.

        The scraper never handles the password itself — this only opens the
        page for the user to type into.
        """
        if platform not in config.PLATFORMS:
            return redirect(url_for("index"))
        url = ("https://www.instagram.com/accounts/login/" if platform == "instagram"
               else "https://www.facebook.com/login/")

        port = (config.CDP_PORT_INSTAGRAM if platform == "instagram"
                else config.CDP_PORT_FACEBOOK)
        launched = False
        if not _port_open(port):
            # Chrome isn't up — launch it via the helper script (it opens the
            # site itself, so no separate navigation is needed).
            opened, err = _launch_chrome_script(platform)
            launched = True
        else:
            opened, err = _drive_browser_to(platform, url)
        _STATUS_CACHE.update(ts=0.0, data=_STATUS_CACHE.get("data"))  # force re-check next poll
        return render_template("login_action.html", platform=platform,
                               opened=opened, error=err, target=url,
                               launched=launched)

    # ── JSON API (for integrating the backend into other apps) ───────
    # The API blueprint reuses the SAME JOBS registry + relevancy/media/job
    # functions defined above, so the HTML dashboard and the API can never
    # drift apart. Mounted at /api/v1.
    from . import api as _api
    _api.init_api(
        jobs=JOBS,
        attach_media_urls=_attach_media_urls,
        attach_relevancy=_attach_relevancy,
        related_keywords=_related_keywords,
        run_enrich_job=_run_enrich_job,
        run_scrape_job=_run_job,
        scrape_config_cls=ScrapeConfig,
    )
    app.register_blueprint(_api.api, url_prefix="/api/v1")
    _api.register_error_handlers(app)

    return app


def _port_open(port: int, timeout: float = 1.0) -> bool:
    """Is something (Chrome's CDP) listening on this local port?"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _launch_chrome_script(platform: str) -> tuple[bool, str | None]:
    """Launch start_chrome_<platform>.bat to bring up that platform's CDP
    Chrome (already pointed at the login page). Returns (started, error)."""
    import subprocess

    script = config.BASE_DIR / "scripts" / f"start_chrome_{platform}.bat"
    if not script.exists():
        return False, f"Launcher not found: {script}"
    try:
        # Fully detached so the request returns immediately and Chrome outlives
        # it. DETACHED_PROCESS + no inherited handles keeps the web worker from
        # blocking on the child. The .bat itself uses `start` to spawn Chrome,
        # so it exits right away.
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            cwd=str(script.parent),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _drive_browser_to(platform: str, url: str) -> tuple[bool, str | None]:
    """Navigate the platform's CDP Chrome to `url` (its login page)."""
    import asyncio

    async def _go():
        from ..browser.manager import BrowserManager, CDPConnectionError
        try:
            async with BrowserManager(platform) as bm:
                page = await bm.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(1)
            return True, None
        except CDPConnectionError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    try:
        return asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        return False, str(e)
