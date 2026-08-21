"""Minimal Python client for the meta_osint JSON API.

Drop this into another app to talk to the backend. No dependency beyond
`requests`. Every method returns the parsed `data` payload (or raises for HTTP
errors). See API.md for the full endpoint list.

    from example_client import MetaOsintClient
    api = MetaOsintClient("http://localhost:5002", api_key=None)
    print(api.stats())
    posts = api.posts(sort="relevancy", limit=10)
    api.add_strategic_keywords(["nuclear", "missile defense"])
    job = api.enrich()
    api.wait_for_job(job["id"])          # blocks until done
    print(api.strategic_overview())

Run directly for a quick smoke test against a running backend:
    python example_client.py http://localhost:5002
"""
from __future__ import annotations

import sys
import time
from typing import Any, Optional

import requests


class MetaOsintError(RuntimeError):
    pass


class MetaOsintClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: int = 30):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.timeout = timeout
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-API-Key"] = api_key

    # ── core ──────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, **kw) -> Any:
        r = self.session.request(method, self.base + path, timeout=self.timeout, **kw)
        try:
            body = r.json()
        except ValueError:
            r.raise_for_status()
            raise MetaOsintError(f"Non-JSON response from {path}")
        if not r.ok:
            err = (body or {}).get("error", {})
            raise MetaOsintError(f"{r.status_code} {err.get('code','')}: {err.get('message', r.text)}")
        return body.get("data")

    def _get(self, path, **params):
        params = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", path, params=params)

    def _post(self, path, json=None):
        return self._request("POST", path, json=json or {})

    def _delete(self, path):
        return self._request("DELETE", path)

    # ── meta / data ───────────────────────────────────────────────────
    def info(self):                       return self._get("/")
    def health(self):                     return self._get("/health")
    def stats(self):                      return self._get("/stats")
    def login_status(self, deep=False):   return self._get("/login-status", deep=1 if deep else None)

    def posts(self, platform=None, keyword=None, author=None, sort="latest",
              limit=50, offset=0):
        return self._get("/posts", platform=platform, keyword=keyword,
                         author=author, sort=sort, limit=limit, offset=offset)

    def post(self, post_id):              return self._get(f"/posts/{post_id}")
    def accounts(self, **q):              return self._get("/accounts", **q)
    def hashtags(self, **q):              return self._get("/hashtags", **q)
    def keywords(self):                   return self._get("/keywords")
    def related_keywords(self):           return self._get("/keywords/related")
    def keyword(self, kw, sort="latest"): return self._get(f"/keywords/{kw}", sort=sort)

    # ── strategic ─────────────────────────────────────────────────────
    def strategic_keywords(self):         return self._get("/strategic/keywords")
    def add_strategic_keywords(self, kws):
        return self._post("/strategic/keywords", {"keywords": kws})
    def remove_strategic_keyword(self, kw):
        return self._delete(f"/strategic/keywords/{kw}")
    def strategic_summary(self):          return self._get("/strategic/summary")
    def strategic_leaderboard(self, limit=15): return self._get("/strategic/leaderboard", limit=limit)
    def strategic_timeline(self):         return self._get("/strategic/timeline")
    def strategic_sentiment(self):        return self._get("/strategic/sentiment")
    def strategic_top_posts(self, limit=12): return self._get("/strategic/top-posts", limit=limit)
    def strategic_overview(self):         return self._get("/strategic/overview")

    # ── jobs ──────────────────────────────────────────────────────────
    def enrich(self, rescore=False):      return self._post("/enrich", {"rescore": rescore})
    def scrape(self, keywords, platforms=None, mode="search", max_posts=15,
               with_comments=False, analyze=False):
        return self._post("/scrape", {
            "keywords": keywords, "platforms": platforms, "mode": mode,
            "max_posts": max_posts, "with_comments": with_comments, "analyze": analyze,
        })
    def jobs(self):                       return self._get("/jobs")
    def job(self, job_id):                return self._get(f"/jobs/{job_id}")
    def stop_job(self, job_id):           return self._post(f"/jobs/{job_id}/stop")

    def wait_for_job(self, job_id, poll=2.0, on_progress=None):
        """Block until a job finishes; returns the final job dict."""
        seen = 0
        while True:
            j = self.job(job_id)
            if on_progress:
                for line in j.get("log", [])[seen:]:
                    on_progress(line)
                seen = len(j.get("log", []))
            if j.get("status") in ("done", "stopped", "error"):
                return j
            time.sleep(poll)


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5002"
    api = MetaOsintClient(base)
    print("info:", api.info().get("db_backend"), "| ollama:", api.info().get("ollama_available"))
    print("stats:", api.stats())
    print("strategic summary:", api.strategic_summary())
    ps = api.posts(sort="relevancy", limit=3)
    print(f"top {len(ps)} posts by relevancy:")
    for p in ps:
        print(f"  [{p.get('relevancy')} {p.get('relevancy_source')}] "
              f"{(p.get('text') or '')[:60]!r}")
