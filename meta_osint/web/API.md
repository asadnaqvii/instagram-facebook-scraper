# meta_osint JSON API

A REST API over the scraper + Strategic Intelligence backend, for integrating
into another app. Every dashboard capability is exposed as JSON. It reuses the
same database and background-job engine as the dashboard, so both stay in sync,
and it works identically on the SQLite or MySQL backend.

Base URL: `http://<host>:<port>/api/v1` (the dashboard serves it — default port
5002). Start it with:

```bash
python -m meta_osint.main serve --port 5002
```

## Response shape

- Success: `{ "data": <payload>, "meta": { ... } }`
- Error:   `{ "error": { "message": "...", "code": "..." } }` with an HTTP status
- Job start returns `202` with `{ "data": { job } }`

## Auth (optional)

Set `META_OSINT_API_KEY` in the environment to require a key; callers send it as
`X-API-Key: <key>` or `Authorization: Bearer <key>`. Unset ⇒ open (typical for a
local backend). `GET /health` is always open. For a browser app on another
origin, set `META_OSINT_API_CORS=true`.

## Endpoints

### Meta
| Method & path | Purpose |
|---|---|
| `GET /` | API descriptor: version, backend, endpoint map |
| `GET /health` | Liveness (open, no key) |
| `GET /stats` | Row-count totals |
| `GET /login-status?deep=0\|1` | Browser/login status per platform |

### Posts
| Method & path | Purpose |
|---|---|
| `GET /posts` | List posts. Query: `platform`, `keyword`, `author`, `sort` (`latest`\|`posted`\|`scraped`\|`relevancy`), `limit` (≤500), `offset` |
| `GET /posts/<id>` | One hydrated post (media, comments, hashtags, keywords, relevancy) |

Each post includes `relevancy` (0–100) and `relevancy_source` (`"AI"` when
enriched, else `"NLP"`), plus `media[]` with **absolute** `src` / `thumb_src`
URLs you can load directly.

### Accounts / hashtags / keywords
| Method & path | Purpose |
|---|---|
| `GET /accounts` | Query: `platform`, `keyword`, `limit` |
| `GET /hashtags` | Query: `platform`, `keyword`, `limit` |
| `GET /keywords` | Search keywords with per-keyword counts |
| `GET /keywords/related` | Keyword co-occurrence clusters |
| `GET /keywords/<kw>` | Full drill-down (posts/accounts/hashtags/places) for one keyword |

### Strategic Intelligence (AI)
| Method & path | Purpose |
|---|---|
| `GET /strategic/keywords` | List strategic keywords (the analysis lens) |
| `POST /strategic/keywords` | Add. Body: `{"keyword":"nuclear"}` or `{"keywords":["a","b"]}` |
| `DELETE /strategic/keywords/<kw>` | Remove one |
| `GET /strategic/summary` | Headline numbers + `meta.pending` |
| `GET /strategic/leaderboard?limit=` | Accounts ranked by strategic relevance |
| `GET /strategic/timeline?min_relevance=` | Strategic activity per day |
| `GET /strategic/sentiment` | Sentiment breakdown of strategic posts |
| `GET /strategic/top-posts?limit=` | Highest-scoring strategic posts |
| `GET /strategic/overview` | **Everything strategic in one call** (for a dashboard widget) |

### Jobs (enrichment + scraping)
| Method & path | Purpose |
|---|---|
| `POST /enrich` | Start an AI enrichment pass. Body: `{"rescore": false}`. `409` if Ollama is down |
| `POST /scrape` | Start a scrape. Body: `{"keywords":[...], "platforms":["instagram"], "mode":"search", "max_posts":15, "with_comments":false, "analyze":false}` |
| `GET /jobs` | Active jobs |
| `GET /jobs/<id>` | One job — poll `status` (`queued`\|`running`\|`done`\|`stopped`\|`error`) and `log[]` for progress |
| `POST /jobs/<id>/stop` | Cooperatively stop a running job |

Enrichment and scraping run in the background; the response returns a job handle
immediately (`202`). Poll `GET /jobs/<id>` until `status` is `done`/`error`.

## Examples

```bash
# Totals
curl http://localhost:5002/api/v1/stats

# Most strategically-relevant posts
curl "http://localhost:5002/api/v1/posts?sort=relevancy&limit=10"

# Add strategic keywords, then enrich
curl -X POST http://localhost:5002/api/v1/strategic/keywords \
     -H "Content-Type: application/json" \
     -d '{"keywords":["nuclear","missile defense"]}'
curl -X POST http://localhost:5002/api/v1/enrich \
     -H "Content-Type: application/json" -d '{}'

# Poll the job it returned
curl http://localhost:5002/api/v1/jobs/<job_id>

# One call for a strategic dashboard
curl http://localhost:5002/api/v1/strategic/overview

# With an API key
curl http://localhost:5002/api/v1/stats -H "X-API-Key: your_key"
```

## Notes for integrators

- **Media**: `src`/`thumb_src` are absolute URLs served by this backend
  (`/media/<file>`). The files must be present in the backend's `MEDIA_DIR`.
- **Backend**: identical responses whether the backend runs on SQLite or MySQL
  (`GET /` reports which). Point it at MySQL with `META_OSINT_DB_BACKEND=mysql`
  and the `MYSQL_*` env vars.
- **Scraping requirements**: a scrape needs the platform's CDP Chrome running and
  logged in on the backend host; enrichment needs a local Ollama. Both degrade
  with clear errors when unavailable.
- See `example_client.py` in this folder for a ready-to-use Python client.
