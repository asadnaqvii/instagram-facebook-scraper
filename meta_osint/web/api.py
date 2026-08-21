"""JSON REST API for meta_osint — for integrating the backend into other apps.

Every dashboard capability is exposed here as clean JSON under /api/v1, reusing
the exact same PostDatabase, relevancy helpers, and background-job machinery the
HTML dashboard uses (so the two never diverge). Another app — in any language —
can drive scraping, run AI enrichment, and read all data over HTTP.

Design:
  * Read endpoints return `{ "data": ..., "meta": {...} }`.
  * Mutations return the created/updated resource or a `{ "job": {...} }` handle.
  * Errors return `{ "error": {"message": ..., "code": ...} }` with a real HTTP
    status. A JSON 404/400/500 handler keeps every response JSON.
  * Optional API-key auth: set META_OSINT_API_KEY and send it as
    `X-API-Key: <key>` (or `Authorization: Bearer <key>`). Unset = open (local).
  * Optional permissive CORS (META_OSINT_API_CORS=true) so a browser app on
    another origin can call it. No external dependency — headers set by hand.

Mount point and job/DB helpers are wired in app.create_app().
"""
from __future__ import annotations

import functools
import threading
import time
import uuid

from flask import Blueprint, current_app, jsonify, request

from .. import config
from ..database.db import PostDatabase
from ..llm.ollama_client import OllamaClient

api = Blueprint("api", __name__)

API_VERSION = "1.0"


# ── helpers injected by create_app (so we reuse the same JOBS + funcs) ───────
# app.create_app() sets these on the blueprint so the API shares the dashboard's
# in-memory job registry and the same relevancy/media/job-runner functions
# instead of importing them (which would risk a cycle).
_ctx: dict = {}


def init_api(*, jobs, attach_media_urls, attach_relevancy, related_keywords,
             run_enrich_job, run_scrape_job, scrape_config_cls,
             open_login_page=None):
    _ctx.update(
        jobs=jobs,
        attach_media_urls=attach_media_urls,
        attach_relevancy=attach_relevancy,
        related_keywords=related_keywords,
        run_enrich_job=run_enrich_job,
        run_scrape_job=run_scrape_job,
        scrape_config_cls=scrape_config_cls,
        open_login_page=open_login_page,
    )


def _db() -> PostDatabase:
    return PostDatabase(config.DB_PATH)


def _absolutize_media(posts):
    """Rewrite relative media URLs (/media/x.jpg) to absolute
    (http://host/media/x.jpg) so a DIFFERENT app consuming the API can load
    them directly. Uses the request's own host, so it's correct behind any
    host/port. Idempotent — leaves already-absolute (external) URLs alone."""
    base = request.host_url.rstrip("/")
    for p in posts:
        for m in p.get("media", []):
            for key in ("src", "thumb_src"):
                v = m.get(key)
                if isinstance(v, str) and v.startswith("/"):
                    m[key] = base + v
    return posts


# ── auth + CORS ──────────────────────────────────────────────────────────────

def _api_key_ok() -> bool:
    required = getattr(config, "API_KEY", "") or ""
    if not required:
        return True  # no key configured → open (typical for local use)
    sent = request.headers.get("X-API-Key", "")
    if not sent:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            sent = auth[7:]
    return sent == required


def require_key(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        if not _api_key_ok():
            return _err("Unauthorized — missing or invalid API key.", 401, "unauthorized")
        return fn(*a, **k)
    return wrapper


@api.after_request
def _cors(resp):
    if getattr(config, "API_CORS", False):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


@api.route("/<path:_any>", methods=["OPTIONS"])
@api.route("/", methods=["OPTIONS"])
def _preflight(_any=None):
    return ("", 204)


# ── response helpers ─────────────────────────────────────────────────────────

def _ok(data, **meta):
    return jsonify({"data": data, "meta": meta})


def _err(message, status=400, code="bad_request"):
    return jsonify({"error": {"message": message, "code": code}}), status


def _job_public(job: dict) -> dict:
    """Trim the internal job dict to a stable public shape."""
    return {
        "id": job.get("id"),
        "kind": job.get("kind", "scrape"),
        "status": job.get("status"),
        "keywords": job.get("keywords", []),
        "log": job.get("log", []),
        "result": job.get("result"),
        "error": job.get("error"),
        "started": job.get("started"),
    }


# ── meta ─────────────────────────────────────────────────────────────────────

@api.get("/")
@require_key
def root():
    """API descriptor — version, backend, and the endpoint map."""
    return _ok({
        "service": "meta_osint",
        "api_version": API_VERSION,
        "db_backend": config.DB_BACKEND,
        "ollama_available": OllamaClient().is_available(),
        "endpoints": {
            "GET  /api/v1/health": "liveness",
            "GET  /api/v1/stats": "row-count totals",
            "GET  /api/v1/posts": "posts (platform, keyword, author, sort, limit, offset)",
            "GET  /api/v1/posts/<id>": "single hydrated post",
            "GET  /api/v1/accounts": "accounts (platform, keyword, limit)",
            "GET  /api/v1/hashtags": "hashtags (platform, keyword, limit)",
            "GET  /api/v1/places": "locations/places (keyword, limit)",
            "GET  /api/v1/keywords": "search keywords with counts",
            "GET  /api/v1/keywords/related": "keyword co-occurrence clusters",
            "GET  /api/v1/keywords/<kw>": "full drill-down for one keyword",
            "GET  /api/v1/strategic/keywords": "list strategic keywords",
            "POST /api/v1/strategic/keywords": "add {keyword} or {keywords:[...]}",
            "DELETE /api/v1/strategic/keywords/<kw>": "remove one",
            "GET  /api/v1/strategic/summary": "headline strategic numbers",
            "GET  /api/v1/strategic/leaderboard": "accounts by strategic relevance",
            "GET  /api/v1/strategic/timeline": "strategic activity over time",
            "GET  /api/v1/strategic/sentiment": "sentiment breakdown",
            "GET  /api/v1/strategic/top-posts": "top strategic posts (limit)",
            "GET  /api/v1/strategic/overview": "everything strategic in one call",
            "POST /api/v1/enrich": "start AI enrichment {rescore?}",
            "POST /api/v1/scrape": "start a scrape {keywords[], platforms[], ...}",
            "GET  /api/v1/jobs": "active jobs",
            "GET  /api/v1/jobs/<id>": "one job (poll for progress)",
            "POST /api/v1/jobs/<id>/stop": "cooperatively stop a job",
            "POST /api/v1/jobs/stop-all": "stop all running jobs",
            "POST /api/v1/login/<platform>": "open a platform's login page in its Chrome",
        },
    })


@api.get("/health")
def health():
    return _ok({"status": "ok", "time": time.time()})


@api.get("/login-status")
@require_key
def login_status():
    """Per-platform browser status. Cheap by default (is each platform's Chrome
    running? — a TCP port check, no navigation). Pass ?deep=1 to also verify the
    logged-in state (this drives the browser, so only call it on user action)."""
    deep = request.args.get("deep") == "1"
    try:
        from ..browser.status import get_status
        return _ok(get_status(deep=deep), deep=deep)
    except Exception as e:  # noqa: BLE001
        return _err(f"status check failed: {e}", 200, "status_error")


@api.post("/login/<platform>")
@require_key
def login_open(platform):
    """Open a platform's login page in its CDP Chrome (launching Chrome via the
    start script if it isn't running). The scraper never handles the password —
    this only gets the user to the page to log in / switch account. Mirrors the
    dashboard's /login/<platform>."""
    if platform not in config.PLATFORMS:
        return _err(f"Unknown platform '{platform}'. Use: {', '.join(config.PLATFORMS)}",
                    400, "bad_request")
    opener = _ctx.get("open_login_page")
    if not opener:
        return _err("Login control not available.", 501, "not_implemented")
    opened, err, launched = opener(platform)
    return _ok({"platform": platform, "opened": opened,
                "chrome_launched": launched, "error": err})


@api.get("/stats")
@require_key
def stats():
    with _db() as db:
        return _ok(db.get_stats())


# ── posts ────────────────────────────────────────────────────────────────────

def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


@api.get("/posts")
@require_key
def posts():
    platform = request.args.get("platform") or None
    keyword = request.args.get("keyword") or None
    author = request.args.get("author") or None
    sort = request.args.get("sort") or "latest"
    limit = min(_int_arg("limit", 50), 500)
    offset = _int_arg("offset", 0)
    db_sort = "latest" if sort == "relevancy" else sort
    with _db() as db:
        ai_scores = db.get_strategic_scores()
        rows = _ctx["attach_relevancy"](
            _ctx["attach_media_urls"](
                db.get_posts(platform=platform, keyword=keyword, author=author,
                             sort=db_sort, limit=limit, offset=offset)),
            ai_scores,
        )
    if sort == "relevancy":
        rows.sort(key=lambda p: p.get("relevancy") or 0, reverse=True)
    _absolutize_media(rows)
    return _ok(rows, count=len(rows), limit=limit, offset=offset,
               filters={"platform": platform, "keyword": keyword,
                        "author": author, "sort": sort})


@api.get("/posts/<int:post_id>")
@require_key
def post_detail(post_id):
    with _db() as db:
        row = db.get_post(post_id)
        if not row:
            return _err(f"Post {post_id} not found.", 404, "not_found")
        ai_scores = db.get_strategic_scores()
        rows = _ctx["attach_relevancy"](_ctx["attach_media_urls"]([row]), ai_scores)
    _absolutize_media(rows)
    return _ok(rows[0])


# ── accounts / hashtags ──────────────────────────────────────────────────────

@api.get("/accounts")
@require_key
def accounts():
    with _db() as db:
        rows = db.get_accounts(
            platform=request.args.get("platform") or None,
            keyword=request.args.get("keyword") or None,
            limit=min(_int_arg("limit", 200), 1000),
        )
    return _ok(rows, count=len(rows))


@api.get("/hashtags")
@require_key
def hashtags():
    with _db() as db:
        rows = db.get_hashtags(
            platform=request.args.get("platform") or None,
            keyword=request.args.get("keyword") or None,
            limit=min(_int_arg("limit", 200), 1000),
        )
    return _ok(rows, count=len(rows))


@api.get("/places")
@require_key
def places():
    with _db() as db:
        rows = db.get_places(
            keyword=request.args.get("keyword") or None,
            limit=min(_int_arg("limit", 200), 1000),
        )
    return _ok(rows, count=len(rows))


# ── keywords ─────────────────────────────────────────────────────────────────

@api.get("/keywords")
@require_key
def keywords():
    with _db() as db:
        return _ok(db.get_keywords())


@api.get("/keywords/related")
@require_key
def keywords_related():
    with _db() as db:
        return _ok(_ctx["related_keywords"](db))


@api.get("/keywords/<path:keyword>")
@require_key
def keyword_detail(keyword):
    sort = request.args.get("sort") or "latest"
    db_sort = "latest" if sort == "relevancy" else sort
    with _db() as db:
        detail = db.get_keyword_detail(keyword, sort=db_sort)
        ai_scores = db.get_strategic_scores()
        detail["posts"] = _ctx["attach_relevancy"](
            _ctx["attach_media_urls"](detail["posts"]), ai_scores)
    if sort == "relevancy":
        detail["posts"].sort(key=lambda p: p.get("relevancy") or 0, reverse=True)
    _absolutize_media(detail["posts"])
    return _ok(detail)


# ── strategic intelligence ───────────────────────────────────────────────────

@api.get("/strategic/keywords")
@require_key
def strategic_keywords_list():
    with _db() as db:
        return _ok(db.get_strategic_keywords())


@api.post("/strategic/keywords")
@require_key
def strategic_keywords_add():
    body = request.get_json(silent=True) or {}
    raw = body.get("keywords") or body.get("keyword") or ""
    if isinstance(raw, str):
        items = [k.strip() for k in raw.replace(",", "\n").splitlines() if k.strip()]
    else:
        items = [str(k).strip() for k in raw if str(k).strip()]
    if not items:
        return _err("Provide 'keyword' (string) or 'keywords' (list).", 400, "bad_request")
    with _db() as db:
        for k in items:
            db.add_strategic_keyword(k)
        return _ok(db.get_strategic_keywords(), added=items)


@api.delete("/strategic/keywords/<path:keyword>")
@require_key
def strategic_keywords_remove(keyword):
    with _db() as db:
        db.remove_strategic_keyword(keyword)
        return _ok(db.get_strategic_keywords(), removed=keyword)


@api.get("/strategic/summary")
@require_key
def strategic_summary():
    with _db() as db:
        return _ok(db.get_strategic_summary(),
                   pending=db.count_posts_needing_analysis())


@api.get("/strategic/leaderboard")
@require_key
def strategic_leaderboard():
    with _db() as db:
        return _ok(db.get_strategic_leaderboard(min(_int_arg("limit", 15), 100)))


@api.get("/strategic/timeline")
@require_key
def strategic_timeline():
    with _db() as db:
        return _ok(db.get_strategic_timeline(_int_arg("min_relevance", 1)))


@api.get("/strategic/sentiment")
@require_key
def strategic_sentiment():
    with _db() as db:
        return _ok(db.get_strategic_sentiment_breakdown())


@api.get("/strategic/top-posts")
@require_key
def strategic_top_posts():
    with _db() as db:
        rows = _ctx["attach_relevancy"](
            _ctx["attach_media_urls"](
                db.get_top_strategic_posts(min(_int_arg("limit", 12), 100))))
    _absolutize_media(rows)
    return _ok(rows, count=len(rows))


@api.get("/strategic/overview")
@require_key
def strategic_overview():
    """Everything the Strategic Intelligence page shows, in one call — handy for
    a dashboard widget in the other app."""
    from collections import Counter
    with _db() as db:
        top_posts = _ctx["attach_relevancy"](
            _ctx["attach_media_urls"](db.get_top_strategic_posts(12)))
        _absolutize_media(top_posts)
        payload = {
            "strategic_keywords": db.get_strategic_keywords(),
            "pending": db.count_posts_needing_analysis(),
            "summary": db.get_strategic_summary(),
            "leaderboard": db.get_strategic_leaderboard(),
            "timeline": db.get_strategic_timeline(),
            "sentiment": db.get_strategic_sentiment_breakdown(),
            "top_posts": top_posts,
        }
    themes, entities = Counter(), Counter()
    for p in top_posts:
        for t in (p.get("topics") or []):
            themes[str(t).strip().lower()] += 1
        for e in (p.get("entities_orgs") or []):
            entities[str(e).strip()] += 1
    payload["themes"] = themes.most_common(18)
    payload["entities"] = entities.most_common(18)
    payload["ollama_available"] = OllamaClient().is_available()
    return _ok(payload)


# ── jobs: enrichment + scraping ──────────────────────────────────────────────

def _active_job(kind: str):
    return next(
        (j for j in _ctx["jobs"].values()
         if j.get("kind") == kind and j.get("status") in ("queued", "running")),
        None,
    )


@api.post("/enrich")
@require_key
def enrich():
    if not OllamaClient().is_available():
        return _err("Ollama is not available — start it to enrich.", 409, "ollama_unavailable")
    existing = _active_job("enrich")
    if existing:
        return _ok(_job_public(existing), already_running=True)
    body = request.get_json(silent=True) or {}
    rescore = bool(body.get("rescore"))
    job_id = uuid.uuid4().hex[:8]
    _ctx["jobs"][job_id] = {
        "id": job_id, "kind": "enrich", "status": "queued", "log": [],
        "keywords": ["AI enrichment"], "started": time.time(),
    }
    threading.Thread(target=_ctx["run_enrich_job"], args=(job_id, rescore),
                     daemon=True).start()
    return _ok(_job_public(_ctx["jobs"][job_id])), 202


@api.post("/scrape")
@require_key
def scrape():
    body = request.get_json(silent=True) or {}
    kws = body.get("keywords") or []
    if isinstance(kws, str):
        kws = [k.strip() for k in kws.replace(",", "\n").splitlines() if k.strip()]
    kws = [str(k).strip() for k in kws if str(k).strip()]
    if not kws:
        return _err("Provide 'keywords' (non-empty list or string).", 400, "bad_request")
    platforms = body.get("platforms") or list(config.PLATFORMS)
    try:
        cfg = _ctx["scrape_config_cls"](
            keywords=kws,
            platforms=platforms,
            mode=body.get("mode", "search"),
            max_posts=int(body.get("max_posts", 15)),
            with_comments=bool(body.get("with_comments", False)),
            analyze=bool(body.get("analyze", False)),
        )
    except Exception as e:  # noqa: BLE001
        return _err(f"Invalid scrape config: {e}", 400, "bad_request")
    job_id = uuid.uuid4().hex[:8]
    _ctx["jobs"][job_id] = {
        "id": job_id, "kind": "scrape", "status": "queued", "log": [],
        "keywords": kws, "started": time.time(),
    }
    threading.Thread(target=_ctx["run_scrape_job"], args=(job_id, cfg),
                     daemon=True).start()
    return _ok(_job_public(_ctx["jobs"][job_id])), 202


@api.get("/jobs")
@require_key
def jobs_list():
    active = [_job_public(j) for j in _ctx["jobs"].values()
              if j.get("status") in ("queued", "running")]
    return _ok(active, count=len(active))


@api.post("/jobs/stop-all")
@require_key
def jobs_stop_all():
    """Cooperatively stop every running/queued job (scrape + enrich)."""
    n = 0
    for j in _ctx["jobs"].values():
        if j.get("status") in ("queued", "running"):
            j["cancel"] = True
            j.setdefault("log", []).append("[stop-all requested via API]")
            n += 1
    return _ok({"stopping": n})


@api.get("/jobs/<job_id>")
@require_key
def job_get(job_id):
    job = _ctx["jobs"].get(job_id)
    if not job:
        return _err(f"Job {job_id} not found.", 404, "not_found")
    return _ok(_job_public(job))


@api.post("/jobs/<job_id>/stop")
@require_key
def job_stop(job_id):
    job = _ctx["jobs"].get(job_id)
    if not job:
        return _err(f"Job {job_id} not found.", 404, "not_found")
    if job.get("status") in ("queued", "running"):
        job["cancel"] = True
        job.setdefault("log", []).append("[stop requested via API]")
        return _ok(_job_public(job), stopping=True)
    return _ok(_job_public(job), stopping=False)


# ── JSON error handlers (so the API never returns an HTML error page) ────────

def register_error_handlers(app):
    @app.errorhandler(404)
    def _404(e):
        if request.path.startswith("/api/"):
            return _err("Not found.", 404, "not_found")
        return e

    @app.errorhandler(405)
    def _405(e):
        if request.path.startswith("/api/"):
            return _err("Method not allowed.", 405, "method_not_allowed")
        return e

    @app.errorhandler(500)
    def _500(e):
        if request.path.startswith("/api/"):
            return _err("Internal server error.", 500, "server_error")
        return e
