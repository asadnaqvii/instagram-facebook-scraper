"""HTTP server for the Meta Scraper dashboard with API endpoints."""
import http.server
import socketserver
import webbrowser
import os
import json
import urllib.parse
import asyncio
import threading
import uuid
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.config import DB_PATH, SESSION_DIR
from app.database.db import PostDatabase

PORT = int(os.getenv("PORT", "8090"))
script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)

# In-memory job tracking
scrape_jobs = {}


def run_scrape_job(job_id, platform, target, max_posts):
    """Background thread for scraping via CDP."""
    from app.main import scrape
    scrape_jobs[job_id]["status"] = "running"
    scrape_jobs[job_id]["message"] = f"Connecting to {platform} Chrome..."
    scrape_jobs[job_id]["current_item"] = target
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(scrape(platform, target, max_posts))
        loop.close()
        if result.get("error"):
            scrape_jobs[job_id]["status"] = "error"
            scrape_jobs[job_id]["message"] = result["error"]
        else:
            scrape_jobs[job_id]["status"] = "done"
            scrape_jobs[job_id]["message"] = f"Scraped {result['posts_scraped']} posts ({result['posts_inserted']} new)"
        scrape_jobs[job_id]["posts_scraped"] = result.get("posts_scraped", 0)
        scrape_jobs[job_id]["posts_inserted"] = result.get("posts_inserted", 0)
    except Exception as e:
        scrape_jobs[job_id]["status"] = "error"
        scrape_jobs[job_id]["message"] = str(e)


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode()) if length else {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/posts":
            self._handle_posts(qs)
        elif path == "/api/stats":
            self._handle_stats(qs)
        elif path == "/api/profiles":
            self._handle_profiles(qs)
        elif path == "/api/hashtags":
            self._handle_hashtags(qs)
        elif path == "/api/scrapes":
            self._handle_scrapes()
        elif path == "/api/sessions":
            self._handle_sessions()
        elif path == "/api/topics":
            self._handle_topics()
        elif path.startswith("/api/topics/") and path.endswith("/posts"):
            self._handle_topic_posts(path, qs)
        elif path.startswith("/api/scrape/status/"):
            job_id = path.split("/")[-1]
            job = scrape_jobs.get(job_id)
            self._json_response(job if job else {"error": "not found"}, 200 if job else 404)
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/scrape":
            self._handle_start_scrape()
        elif path == "/api/scrape-topic":
            self._handle_scrape_topic()
        elif path == "/api/login":
            self._handle_login()
        elif path == "/api/topics/save":
            self._handle_save_topics()
        elif path == "/api/topics/reload":
            self._handle_reload_topics()
        elif path.startswith("/api/logout/"):
            platform = path.split("/")[-1]
            self._handle_logout(platform)
        else:
            self.send_error(404)

    # ── API handlers ──

    def _handle_posts(self, qs):
        platform = qs.get("platform", [None])[0]
        author = qs.get("author", [None])[0]
        search = qs.get("search", [None])[0]
        hashtag = qs.get("hashtag", [None])[0]
        limit = int(qs.get("limit", ["50"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        with PostDatabase(DB_PATH) as db:
            posts = db.get_posts(platform=platform, author=author, search=search,
                                 hashtag=hashtag, limit=limit, offset=offset)
            total = db.get_post_count(platform=platform, author=author)
        self._json_response({"posts": posts, "total": total, "limit": limit, "offset": offset,
                             "has_more": (offset + limit) < total})

    def _handle_stats(self, qs):
        with PostDatabase(DB_PATH) as db:
            stats = db.get_stats()
        self._json_response(stats)

    def _handle_profiles(self, qs):
        platform = qs.get("platform", [None])[0]
        with PostDatabase(DB_PATH) as db:
            profiles = db.get_profiles(platform=platform)
        self._json_response(profiles)

    def _handle_hashtags(self, qs):
        limit = int(qs.get("limit", ["30"])[0])
        with PostDatabase(DB_PATH) as db:
            tags = db.get_top_hashtags(limit=limit)
        self._json_response(tags)

    def _handle_scrapes(self):
        with PostDatabase(DB_PATH) as db:
            scrapes = db.get_recent_scrapes()
        self._json_response(scrapes)

    def _handle_sessions(self):
        """Check if Chrome instances are reachable via CDP."""
        import urllib.request
        status = {}
        for platform, port in [("facebook", 9222), ("instagram", 9223)]:
            connected = False
            try:
                r = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2)
                connected = r.status == 200
            except Exception:
                pass
            status[platform] = {"connected": connected, "port": port}
        self._json_response(status)

    def _handle_start_scrape(self):
        data = self._read_body()
        platform = data.get("platform", "instagram")
        target = data.get("target", "").strip()
        max_posts = int(data.get("max_posts", 10))
        if not target:
            self._json_response({"error": "target is required"}, 400)
            return
        job_id = str(uuid.uuid4())[:8]
        scrape_jobs[job_id] = {
            "id": job_id, "platform": platform, "target": target,
            "max_posts": max_posts, "status": "queued", "message": "Starting...",
            "posts_scraped": 0, "posts_inserted": 0,
        }
        t = threading.Thread(target=run_scrape_job, args=(job_id, platform, target, max_posts), daemon=True)
        t.start()
        self._json_response({"job_id": job_id})

    def _handle_login(self):
        """Handle POST /api/login — check if Chrome instances are running."""
        import urllib.request
        results = {}
        for platform, port in [("facebook", 9222), ("instagram", 9223)]:
            try:
                r = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2)
                results[platform] = True
            except Exception:
                results[platform] = False

        fb = results.get("facebook", False)
        ig = results.get("instagram", False)
        if fb and ig:
            msg = "Both Chrome instances connected (FB:9222, IG:9223)"
        elif fb:
            msg = "Facebook Chrome connected (port 9222). Instagram Chrome not found (port 9223)."
        elif ig:
            msg = "Instagram Chrome connected (port 9223). Facebook Chrome not found (port 9222)."
        else:
            msg = "No Chrome instances found. Run start_chrome_debug.bat first."

        self._json_response({"success": fb or ig, "message": msg, "facebook": fb, "instagram": ig})

    def _handle_topics(self):
        """GET /api/topics"""
        try:
            from app.utils.topics import parse_topics_file
            topics = parse_topics_file("topics.txt")
            with PostDatabase(DB_PATH) as db:
                db.sync_topics(topics)
                db_topics = db.get_topics()
            self._json_response(db_topics)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._json_response({"error": str(e)}, 500)

    def _handle_topic_posts(self, path, qs):
        """GET /api/topics/:id/posts"""
        parts = path.split("/")
        topic_id = int(parts[-2])
        limit = int(qs.get("limit", ["50"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        with PostDatabase(DB_PATH) as db:
            posts = db.get_posts_by_topic(topic_id, limit=limit, offset=offset)
            total = db.get_topic_post_count(topic_id)
        self._json_response({"posts": posts, "total": total})

    def _handle_scrape_topic(self):
        """POST /api/scrape-topic — scrape all items in a topic."""
        data = self._read_body()
        topic_name = data.get("topic_name", "").strip()
        max_posts = int(data.get("max_posts", 10))
        if not topic_name:
            self._json_response({"error": "topic_name required"}, 400)
            return

        from app.utils.topics import parse_topics_file
        topics = parse_topics_file("topics.txt")
        topic = None
        for t in topics:
            if t["name"] == topic_name:
                topic = t
                break
        if not topic:
            self._json_response({"error": f"Topic '{topic_name}' not found"}, 404)
            return

        job_id = str(uuid.uuid4())[:8]
        scrape_jobs[job_id] = {
            "id": job_id, "platform": "multi", "target": topic_name,
            "max_posts": max_posts, "status": "queued",
            "message": f"Starting topic scrape: {topic_name}",
            "posts_scraped": 0, "posts_inserted": 0,
        }

        def run_topic_job():
            from app.main import scrape as _scrape
            from app.database.db import PostDatabase
            scrape_jobs[job_id]["status"] = "running"
            scrape_jobs[job_id]["items"] = []  # Per-item progress
            scrape_jobs[job_id]["current_item"] = ""

            all_items = []
            for kw in topic.get("keywords", []):
                for plat in topic.get("platforms", ["facebook"]):
                    all_items.append({"value": kw, "platform": plat, "type": "keyword"})
            for h in topic.get("handles", []):
                handle = h.lstrip("@")
                for plat in topic.get("platforms", ["facebook"]):
                    all_items.append({"value": handle, "platform": plat, "type": "handle"})

            scrape_jobs[job_id]["total_items"] = len(all_items)
            total_scraped = 0
            total_inserted = 0

            # Get topic ID
            with PostDatabase(DB_PATH) as db:
                db_topic = db.get_topic_by_name(topic_name)
                topic_id = db_topic["id"] if db_topic else None

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                for i, item in enumerate(all_items):
                    label = f"{item['platform']}/{item['value']}"
                    scrape_jobs[job_id]["current_item"] = label
                    scrape_jobs[job_id]["message"] = f"[{i+1}/{len(all_items)}] Scraping {label}..."

                    item_status = {"label": label, "status": "running", "posts": 0}
                    scrape_jobs[job_id]["items"].append(item_status)

                    try:
                        result = loop.run_until_complete(
                            _scrape(item["platform"], item["value"], max_posts, topic_id=topic_id)
                        )
                        item_status["status"] = "done"
                        item_status["posts"] = result.get("posts_scraped", 0)
                        total_scraped += result.get("posts_scraped", 0)
                        total_inserted += result.get("posts_inserted", 0)
                    except Exception as e:
                        item_status["status"] = "error"
                        item_status["error"] = str(e)[:100]

                    scrape_jobs[job_id]["posts_scraped"] = total_scraped
                    scrape_jobs[job_id]["posts_inserted"] = total_inserted

                    import time; time.sleep(2)

                loop.close()
                scrape_jobs[job_id]["status"] = "done"
                scrape_jobs[job_id]["message"] = f"Done! {total_scraped} posts ({total_inserted} new)"
                scrape_jobs[job_id]["current_item"] = ""
            except Exception as e:
                scrape_jobs[job_id]["status"] = "error"
                scrape_jobs[job_id]["message"] = str(e)

        t = threading.Thread(target=run_topic_job, daemon=True)
        t.start()
        self._json_response({"job_id": job_id})

    def _handle_save_topics(self):
        """POST /api/topics/save"""
        data = self._read_body()
        topics = data.get("topics", [])
        from app.utils.topics import save_topics_file
        ok = save_topics_file(topics, "topics.txt")
        if ok:
            with PostDatabase(DB_PATH) as db:
                db.sync_topics(topics)
        self._json_response({"success": ok})

    def _handle_reload_topics(self):
        """POST /api/topics/reload"""
        from app.utils.topics import parse_topics_file
        topics = parse_topics_file("topics.txt")
        with PostDatabase(DB_PATH) as db:
            db.sync_topics(topics)
        self._json_response({"success": True, "count": len(topics)})

    def _handle_logout(self, platform):
        if platform not in ("instagram", "facebook"):
            self._json_response({"error": "invalid platform"}, 400)
            return
        sf = Path(SESSION_DIR) / f"{platform}_session.json"
        if sf.exists():
            sf.unlink()
        self._json_response({"success": True})


if __name__ == "__main__":
    print(f"\n  Meta Scraper Dashboard: http://localhost:{PORT}/dashboard.html\n")
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(f"http://localhost:{PORT}/dashboard.html")
        except Exception:
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), DashboardHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
