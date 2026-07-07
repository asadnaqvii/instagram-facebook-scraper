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
from ..orchestrator import ScrapeConfig, run_sync


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

    try:
        result = run_sync(cfg, progress)
        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["result"] = result
    except Exception as e:  # noqa: BLE001
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)


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
            recent_posts = _attach_media_urls(db.get_posts(limit=25))
        return render_template("index.html", stats=stats, keywords=keywords, posts=recent_posts, config=config.as_dict())

    @app.route("/posts")
    def posts():
        platform = request.args.get("platform") or None
        keyword = request.args.get("keyword") or None
        sort = request.args.get("sort") or "latest"
        with PostDatabase(config.DB_PATH) as db:
            rows = _attach_media_urls(db.get_posts(platform=platform, keyword=keyword, sort=sort, limit=100))
            all_keywords = [k["keyword"] for k in db.get_keywords()]
        return render_template("posts.html", posts=rows, platform=platform, keyword=keyword,
                               sort=sort, all_keywords=all_keywords)

    @app.route("/keyword/<path:keyword>")
    def keyword_detail(keyword):
        sort = request.args.get("sort") or "latest"
        with PostDatabase(config.DB_PATH) as db:
            detail = db.get_keyword_detail(keyword, sort=sort)
            detail["posts"] = _attach_media_urls(detail["posts"])
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

    return app
