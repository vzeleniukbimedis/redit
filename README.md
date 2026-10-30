# Bimedis Reddit buyer monitor

A small read-only tool used by [Bimedis](https://bimedis.com), a marketplace for new and used medical equipment.
It finds public Reddit posts where a clinic, lab, vet or dental practice is **looking to buy** medical equipment
(ultrasound, X-ray, dental chairs, lab analyzers, etc.) and notifies our sales team in a private Telegram chat, so a
person can read the post and decide whether to reply manually.

## How it uses the Reddit API

* **Official Data API only** – OAuth2 application-only token (`grant_type=client_credentials`) against
  `oauth.reddit.com`. No scraping, no logged-in sessions, no proxies.
* **Read-only** – only two endpoints are called:
  * `GET /search?q=<query>&sort=new&t=week&type=link&limit=100` – keyword search for buying-intent phrases
    (`monitor/config.json → search_queries`)
  * `GET /r/<subreddit>/new?limit=100` – newest posts in ~50 professional subreddits (`subreddits`)
* **Nothing is ever posted, voted, messaged or modified** on Reddit by this tool. Replies, if any, are written by a
  human from their own account through the normal Reddit UI.
* **Rate limits** – one request at a time with a 1 s pause, one cycle every 30 minutes (~210 requests per cycle,
  ≈7 requests/minute on average). The client reads `X-Ratelimit-Remaining` / `X-Ratelimit-Reset` and sleeps when the
  quota is close to exhausted; a 429 is retried once after the reset window.
* **Descriptive User-Agent** – `server:com.bimedis.buyer-monitor:2.0 (by /u/<app owner>)`.

## Data handling

* Every fetched post is regex-scored locally (equipment terms × buying-intent phrases − negatives such as sellers,
  job posts, students). Posts below the threshold are discarded; only their IDs are kept for de-duplication.
* Candidates above the threshold get a second opinion from an LLM (via OpenRouter) that answers "is this a buyer,
  what equipment, which country". Reddit content is **not** used to train or fine-tune any model.
* Only matching posts (title, text, permalink, subreddit, author name, score) are stored in a local SQLite file and
  sent to a private Telegram chat of the sales team. Data is not sold, shared or published. Posts older than
  `max_age_days` (30) are ignored.

```
oauth.reddit.com ──▶ monitor.py ──▶ regex score ──▶ LLM check ──▶ SQLite + private Telegram chat ──▶ human
```

## Run

```bash
cp .env.example .env      # Reddit app credentials, OpenRouter key, Telegram bot
docker compose up -d --build
docker compose logs -f monitor
```

Without Docker (Python 3.10+, standard library only):

```bash
cd monitor && set -a && . ../.env && set +a
python3 monitor.py run                 # one cycle
python3 monitor.py loop                # every interval_minutes
python3 monitor.py leads 20            # last 20 accepted leads
python3 monitor.py test "some text"    # regex score only
python3 monitor.py ai "some text"      # LLM verdict only
```

## Files

| path | purpose |
|---|---|
| `monitor/monitor.py` | Reddit API client, scoring, LLM check, storage, Telegram |
| `monitor/config.json` | search queries, subreddits, equipment terms, intent/negative patterns, thresholds, LLM prompt |
| `.env` | secrets: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| `docker-compose.yml` | single `monitor` service |
| `monitor/data/leads.db` | SQLite: `seen` (dedupe IDs) + `leads` (matched posts with the LLM verdict) |

## Tuning

* Too much noise → raise `min_score` or `ai.min_confidence`, add patterns to `negative` / `soft_negative`.
* Missing leads → add search queries or subreddits, lower `min_score` (the LLM is the final gate).

## Alerts

Health problems are sent to the same Telegram chat (max one per problem per hour, plus a recovery message);
`ALERTS_MUTED=all` or a comma-separated list of alert names keeps them in the log only:

* **Reddit API не відповідає** — ≥50% of API requests in a cycle failed (bad credentials, app suspended, outage)
* **OpenRouter не відповідає** — every LLM call in a cycle failed (leads still go out, unfiltered)
* **цикл впав** — unexpected exception in the crawl loop

## Contact

Bimedis · https://bimedis.com
