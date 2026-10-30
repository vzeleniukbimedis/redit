#!/usr/bin/env python3
"""Reddit buyer-intent monitor for medical equipment.

Sources:
  * Reddit, through the official OAuth Data API (read-only, app-only token):
      global search for intent queries       GET /search?q=...&sort=new&t=week
      newest posts of selected subreddits    GET /r/<sub>/new
  * Threads (optional, internal threads_source.py module; skipped when absent),
    every `threads.interval_minutes` inside the same loop when enabled.

Every post is scored: equipment terms x buyer-intent patterns - negatives.
Posts above `min_score` are stored in SQLite and pushed to Telegram (optional).

Usage:
  monitor.py run              one crawl cycle
  monitor.py loop             crawl forever every `interval_minutes`
  monitor.py leads [N]        print last N leads (default 30)
  monitor.py test "some text" regex-score arbitrary text (for tuning config.json)
  monitor.py ai "some text"   ask the LLM about arbitrary text
  monitor.py threads          one Threads collection now (ignores the interval)
"""
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import base64
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import threads_source  # optional second source, not needed for Reddit
except ImportError:
    threads_source = None

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MONITOR_CONFIG", HERE / "config.json"))
DB_PATH = Path(os.environ.get("MONITOR_DB", HERE / "data" / "leads.db"))
# Reddit OAuth app (reddit.com/prefs/apps, type "script"). Read-only, application-only token.
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME", "")  # owner of the app, used only in the User-Agent
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
# Who to ping on failures: "@username" or a numeric Telegram user id (works without username)
TG_ALERT_MENTION = os.environ.get("TELEGRAM_ALERT_MENTION", "")
ALERT_COOLDOWN_SEC = int(os.environ.get("ALERT_COOLDOWN_SEC", "3600"))
# Alerts that only go to the log, not Telegram: "all" or comma-separated keys (e.g. "Reddit API не відповідає")
ALERTS_MUTED = {k.strip() for k in os.environ.get("ALERTS_MUTED", "").split(",") if k.strip()}
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OR_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
# Reddit API rules ask for a unique, descriptive User-Agent: <platform>:<app id>:<version> (by /u/<username>)
USER_AGENT = f"server:com.bimedis.buyer-monitor:2.0 (by /u/{REDDIT_USERNAME or 'unknown'})"

log = logging.getLogger("monitor")


# --------------------------------------------------------------------------- model
@dataclass
class Post:
    id: str
    subreddit: str                 # reddit: sub name · threads: "threads"
    author: str
    title: str
    body: str
    path: str                      # reddit: /r/sub/comments/<id>/slug/ · threads: ""
    created: str = ""              # ISO-8601 UTC if known
    source: str = ""               # "search:<query>" | "new:<sub>" | "threads:<query>"
    platform: str = "reddit"
    link: str = ""                 # explicit URL when `path` is not a reddit path

    @property
    def url(self) -> str:
        return self.link or "https://www.reddit.com" + self.path

    @property
    def where(self) -> str:
        return "threads" if self.platform == "threads" else f"r/{self.subreddit}"

    @property
    def handle(self) -> str:
        return ("@" if self.platform == "threads" else "u/") + self.author

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"


@dataclass
class Score:
    total: int = 0
    equipment: list = field(default_factory=list)
    intent: list = field(default_factory=list)
    negative: list = field(default_factory=list)


# --------------------------------------------------------------------------- config / scoring
class Scorer:
    def __init__(self, cfg: dict):
        self.min_score = cfg.get("min_score", 4)
        eq = cfg["equipment"]
        # longest terms first so "ultrasound machine" wins over "ultrasound"
        self.equipment = sorted(
            [(t.lower(), 2) for t in eq.get("weight_2", [])] + [(t.lower(), 1) for t in eq.get("weight_1", [])],
            key=lambda x: -len(x[0]),
        )
        self.intent = []
        for key, w in (("weight_3", 3), ("weight_2", 2), ("weight_1", 1)):
            for pat in cfg["intent"].get(key, []):
                self.intent.append((re.compile(pat, re.I), w, pat))
        self.negative = [(re.compile(p, re.I), p) for p in cfg.get("negative", [])]
        self.soft_negative = [(re.compile(p, re.I), p) for p in cfg.get("soft_negative", [])]

    def score(self, text: str) -> Score:
        s = Score()
        low = text.lower()
        seen_spans = []
        eq_score = 0
        for term, w in self.equipment:
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(term), low):
                span = (m.start(), m.end())
                # skip if inside an already-counted longer term
                if any(a <= span[0] and span[1] <= b for a, b in seen_spans):
                    continue
                seen_spans.append(span)
                if term not in s.equipment:
                    s.equipment.append(term)
                    eq_score += w
        if not s.equipment:
            return s  # no equipment -> not a lead, no matter the intent
        eq_score = min(eq_score, 4)

        intent_score = 0
        strong3 = strong2 = 0
        for rx, w, pat in self.intent:
            m = rx.search(text)
            if m:
                s.intent.append(m.group(0).strip())
                intent_score += w
                strong3 += w == 3
                strong2 += w == 2
        # weak words alone ("price", "brand", "used") are not buyer intent -- unless a specific device is named,
        # then the LLM gets to decide (it costs ~$0.0001 per post)
        if not (strong3 or strong2 >= 1 or (eq_score >= 2 and intent_score >= 1)):
            s.total = 0
            return s
        intent_score = min(intent_score, 8)

        neg = 0
        for rx, pat in self.negative:
            m = rx.search(text)
            if m:
                s.negative.append(m.group(0).strip())
                neg += 3
        for rx, pat in self.soft_negative:
            m = rx.search(text)
            if m:
                s.negative.append(m.group(0).strip())
                neg += 2

        s.total = eq_score + intent_score - neg
        return s


# --------------------------------------------------------------------------- reddit api
class RedditError(Exception):
    pass


class Reddit:
    """Minimal read-only client for the official Reddit Data API.

    Application-only OAuth (client_credentials), honours X-Ratelimit-* headers, never writes anything.
    """
    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    API = "https://oauth.reddit.com"

    def __init__(self):
        if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
            raise RedditError("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set")
        self.token = ""
        self.token_expires = 0.0

    def _auth(self) -> None:
        basic = base64.b64encode(f"{REDDIT_CLIENT_ID}:{REDDIT_CLIENT_SECRET}".encode()).decode()
        req = urllib.request.Request(
            self.TOKEN_URL, data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RedditError(f"token request failed: {e}") from e
        if "access_token" not in data:
            raise RedditError(f"token request rejected: {data}")
        self.token = data["access_token"]
        self.token_expires = time.time() + int(data.get("expires_in", 3600)) - 60

    def get(self, path: str, params: dict) -> dict:
        for attempt in (1, 2):
            if time.time() >= self.token_expires:
                self._auth()
            url = f"{self.API}{path}?{urllib.parse.urlencode({**params, 'raw_json': 1})}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self._respect_ratelimit(r.headers)
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:  # token revoked/expired early
                    self.token_expires = 0
                    continue
                if e.code == 429 and attempt == 1:
                    wait = float(e.headers.get("X-Ratelimit-Reset") or 60)
                    log.warning("reddit rate limit hit, sleeping %.0fs", wait)
                    time.sleep(wait + 1)
                    continue
                raise RedditError(f"HTTP {e.code} for {path}") from e
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise RedditError(f"{path}: {e}") from e
        raise RedditError(f"giving up on {path}")

    @staticmethod
    def _respect_ratelimit(headers) -> None:
        try:
            remaining = float(headers.get("X-Ratelimit-Remaining") or 100)
            reset = float(headers.get("X-Ratelimit-Reset") or 0)
        except ValueError:
            return
        if remaining < 2 and reset:
            log.info("reddit quota exhausted, sleeping %.0fs until reset", reset)
            time.sleep(reset + 1)

    def search(self, query: str, limit: int = 100) -> list[Post]:
        data = self.get("/search", {"q": query, "sort": "new", "t": "week", "type": "link", "limit": limit})
        return parse_listing(data, f"search:{query}")

    def new_posts(self, subreddit: str, limit: int = 100) -> list[Post]:
        data = self.get(f"/r/{subreddit}/new", {"limit": limit})
        return parse_listing(data, f"new:{subreddit}")


def parse_listing(data: dict, source: str) -> list[Post]:
    posts = []
    for child in (data.get("data") or {}).get("children", []):
        d = child.get("data") or {}
        if child.get("kind") != "t3" or not d.get("id"):
            continue
        created = d.get("created_utc")
        posts.append(Post(
            d["id"], d.get("subreddit", ""), d.get("author", ""), d.get("title", ""), d.get("selftext", ""),
            d.get("permalink", ""),
            datetime.fromtimestamp(created, timezone.utc).isoformat() if created else "", source,
        ))
    return posts


# --------------------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY, found_at TEXT NOT NULL, created TEXT, subreddit TEXT, author TEXT,
  title TEXT, body TEXT, url TEXT, score INTEGER, equipment TEXT, intent TEXT, negative TEXT,
  source TEXT, notified INTEGER DEFAULT 0,
  ai_is_buyer INTEGER, ai_confidence REAL, ai_equipment TEXT, ai_location TEXT, ai_buyer_type TEXT, ai_summary TEXT
);
CREATE INDEX IF NOT EXISTS leads_found ON leads(found_at);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------- llm (OpenRouter)
class AI:
    """Second-opinion classifier. Returns a dict or None when disabled/failed."""

    def __init__(self, cfg: dict):
        a = cfg.get("ai", {})
        self.enabled = bool(a.get("enabled", True) and OR_KEY)
        self.min_confidence = float(a.get("min_confidence", 0.6))
        self.notify_on_error = bool(a.get("notify_without_ai_on_error", True))
        self.system_prompt = a["system_prompt"]
        self.spent_usd = 0.0

    def classify(self, p: Post):
        if not self.enabled:
            return None
        user = f"{p.where}, {p.handle}\nTitle: {p.title}\n\n{p.body[:3000]}"
        body = {
            "model": OR_MODEL, "temperature": 0, "max_tokens": 1000, "reasoning": {"enabled": False},
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user}],
        }
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://bimedis.com", "X-Title": "bimedis-reddit-monitor"},
        )
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    data = json.load(r)
                self.spent_usd += float(data.get("usage", {}).get("cost", 0) or 0)
                content = (data["choices"][0]["message"].get("content") or "").strip()
                if not content:
                    raise ValueError("empty completion: " + json.dumps(data.get("choices", [{}])[0].get("finish_reason")))
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
                v = json.loads(content)
                if not isinstance(v, dict):
                    raise ValueError(f"unexpected JSON shape: {type(v).__name__}")
                return {
                    "is_buyer": bool(v.get("is_buyer")),
                    "confidence": float(v.get("confidence") or 0),
                    "equipment": ", ".join(str(x) for x in v.get("equipment") or []) if isinstance(v.get("equipment"), list) else str(v.get("equipment") or ""),
                    "location": v.get("location") or None,
                    "buyer_type": v.get("buyer_type") or None,
                    "summary": (v.get("summary") or "")[:400],
                }
            except (urllib.error.URLError, OSError, KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
                log.warning("openrouter attempt %d failed: %s", attempt, e)
                time.sleep(3)
        return None

    def accept(self, verdict) -> bool:
        if verdict is None:
            return self.notify_on_error
        return verdict["is_buyer"] and verdict["confidence"] >= self.min_confidence


# --------------------------------------------------------------------------- telegram
def tg_send(text: str) -> bool:
    if not (TG_TOKEN and TG_CHAT):
        return False
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError) as e:
        log.warning("telegram send failed: %s", e)
        return False


# --------------------------------------------------------------------------- health alerts
class Alerts:
    """Ping a human in Telegram when the pipeline degrades; one message per problem per cooldown, plus recovery."""

    def __init__(self):
        self.active: dict[str, float] = {}   # key -> last sent ts

    @staticmethod
    def mention() -> str:
        m = TG_ALERT_MENTION.strip()
        if not m:
            return ""
        if m.lstrip("-").isdigit():
            return f'<a href="tg://user?id={m}">⚠️ увага</a> '
        return html.escape(m if m.startswith("@") else "@" + m) + " "

    @staticmethod
    def muted(key: str) -> bool:
        return "all" in ALERTS_MUTED or key in ALERTS_MUTED

    def fire(self, key: str, text: str) -> None:
        if self.muted(key):
            log.error("ALERT (muted) %s: %s", key, text)
            return
        now = time.time()
        last = self.active.get(key)
        if last is not None and now - last < ALERT_COOLDOWN_SEC:
            return
        self.active[key] = now
        log.error("ALERT %s: %s", key, text)
        tg_send(f"{self.mention()}🚨 <b>reddit-monitor: {html.escape(key)}</b>\n{html.escape(text)}")

    def resolve(self, key: str) -> None:
        if key in self.active and not self.muted(key):
            del self.active[key]
            log.info("RECOVERED %s", key)
            tg_send(f"✅ <b>reddit-monitor: {html.escape(key)} — відновилось</b>")


alerts = Alerts()


def format_lead(p: Post, s: Score, ai=None) -> str:
    body = p.body[:400] + ("…" if len(p.body) > 400 else "")
    icon = "🧵" if p.platform == "threads" else "🩺"
    head = f"{icon} <b>{html.escape(p.title)}</b>\n" \
           f"{html.escape(p.where)} · {html.escape(p.handle)}" + (f" · {p.created[:10]}" if p.created else "") + "\n"
    if ai:
        head += (f"🤖 {html.escape(ai['summary'])}\n"
                 f"📍 {html.escape(ai['location'] or '?')} · 👤 {html.escape(ai['buyer_type'] or '?')} · {int(ai['confidence']*100)}%\n"
                 f"🔧 {html.escape(ai['equipment'] or ', '.join(s.equipment))}\n")
    else:
        head += f"🔧 {html.escape(', '.join(s.equipment))}\n🎯 {html.escape(', '.join(s.intent))} · score {s.total}\n"
    return head + f"\n{html.escape(body)}\n\n{p.url}"


# --------------------------------------------------------------------------- per-post pipeline (all sources)
@dataclass
class Handled:
    lead: int = 0
    ai_called: int = 0
    ai_failed: int = 0


def handle_post(p: Post, scorer: Scorer, ai: AI, con: sqlite3.Connection, now: str, cutoff: float) -> Handled:
    """seen-dedupe -> age cut -> regex score -> LLM -> SQLite -> Telegram. Same for every source."""
    r = Handled()
    if con.execute("SELECT 1 FROM seen WHERE id=?", (p.id,)).fetchone():
        return r
    con.execute("INSERT INTO seen VALUES (?,?)", (p.id, now))
    if cutoff and p.created and datetime.fromisoformat(p.created).timestamp() < cutoff:
        return r  # old post surfaced by search backfill
    s = scorer.score(p.text)
    if s.total < scorer.min_score:
        return r
    verdict = ai.classify(p)
    if ai.enabled:
        r.ai_called = 1
        r.ai_failed = int(verdict is None)
    accepted = ai.accept(verdict)
    v = verdict or {}
    con.execute(
        "INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)",
        (p.id, now, p.created, p.subreddit, p.author, p.title, p.body, p.url, s.total,
         ", ".join(s.equipment), ", ".join(s.intent), ", ".join(s.negative), p.source,
         None if verdict is None else int(v["is_buyer"]), v.get("confidence"), v.get("equipment"),
         v.get("location"), v.get("buyer_type"), v.get("summary")),
    )
    if not accepted:
        log.info("skip  %2d %s — %s  (ai: %s %.0f%%)", s.total, p.where, p.title[:70],
                 v.get("is_buyer"), v.get("confidence", 0) * 100)
        return r
    r.lead = 1
    log.info("LEAD %2d %s %s — %s  %s%s", s.total, p.where, p.handle, p.title[:80], p.url,
             f"  [{v.get('buyer_type')}, {v.get('location')}]" if verdict else "")
    if tg_send(format_lead(p, s, verdict)):
        con.execute("UPDATE leads SET notified=1 WHERE id=?", (p.id,))
    return r


# --------------------------------------------------------------------------- threads
def threads_post(raw: dict, query: str) -> Post:
    text = raw["text"]
    title = text.split("\n", 1)[0].strip()[:120] or f"@{raw['author']}"
    return Post(
        raw["id"], "threads", raw["author"], title, text, "",
        threads_source.timeago_to_iso(raw.get("timeago", "")), f"threads:{query}",
        platform="threads", link=f"https://www.threads.com/@{raw['author']}",
    )


def threads_due(cfg: dict, con: sqlite3.Connection) -> bool:
    t = cfg.get("threads", {})
    if not t.get("enabled") or threads_source is None:
        return False
    row = con.execute("SELECT value FROM state WHERE key='threads_last_run'").fetchone()
    if not row:
        return True
    return time.time() - float(row[0]) >= t.get("interval_minutes", 180) * 60


def crawl_threads(cfg: dict, scorer: Scorer, ai: AI, con: sqlite3.Connection) -> int:
    """One pass over `threads.queries` on the phone. A missing phone is an alert, not a crash."""
    now = datetime.now(timezone.utc).isoformat()
    max_age = cfg.get("max_age_days", 0) * 86400
    cutoff = time.time() - max_age if max_age else 0
    con.execute("INSERT OR REPLACE INTO state VALUES ('threads_last_run', ?)", (str(time.time()),))
    con.commit()
    new_leads = fetched = 0
    ai_calls = ai_failures = 0
    try:
        for p in threads_source.collect(cfg, threads_post):
            fetched += 1
            r = handle_post(p, scorer, ai, con, now, cutoff)
            ai_calls += r.ai_called
            ai_failures += r.ai_failed
            new_leads += r.lead
            if r.lead or r.ai_called:
                con.commit()
    except threads_source.AdbError as e:
        alerts.fire("Threads: телефон недоступний",
                    f"{e}. Перевір USB/Wi-Fi ADB (ADB_CONNECT={os.environ.get('ADB_CONNECT') or 'not set'}) "
                    "і що екран розблоковано.")
        return 0
    finally:
        con.commit()
    alerts.resolve("Threads: телефон недоступний")
    if fetched == 0:
        alerts.fire("Threads: порожні результати",
                    "Жоден запит не дав постів. Threads оновив розмітку або пошук не відкрився — перевір телефон.")
    else:
        alerts.resolve("Threads: порожні результати")
    if ai_calls >= 3 and ai_failures == ai_calls:
        alerts.fire("OpenRouter не відповідає", f"Всі {ai_calls} AI-запити провалились (модель {OR_MODEL}).")
    log.info("threads done: %d posts fetched, %d new leads, ai spend $%.4f", fetched, new_leads, ai.spent_usd)
    return new_leads


# --------------------------------------------------------------------------- daily digest
DIGEST_HOUR_UTC = int(os.environ.get("DIGEST_HOUR_UTC", "6"))  # 09:00 Kyiv


def daily_digest(con: sqlite3.Connection) -> None:
    """Once a day, post a one-line health summary so a quiet day is visibly different from a dead monitor."""
    now = datetime.now(timezone.utc)
    if now.hour < DIGEST_HOUR_UTC:
        return
    row = con.execute("SELECT value FROM state WHERE key='last_digest'").fetchone()
    if row and row[0][:10] == now.date().isoformat():
        return
    since = (now - timedelta(hours=24)).isoformat()
    seen = con.execute("SELECT COUNT(*) FROM seen WHERE first_seen>=?", (since,)).fetchone()[0]
    cand = con.execute("SELECT COUNT(*) FROM leads WHERE found_at>=?", (since,)).fetchone()[0]
    leads = con.execute("SELECT COUNT(*) FROM leads WHERE found_at>=? AND notified=1", (since,)).fetchone()[0]
    rejected = con.execute("SELECT title, subreddit FROM leads WHERE found_at>=? AND notified=0 ORDER BY score DESC LIMIT 3", (since,)).fetchall()
    text = (f"📊 <b>reddit-monitor за 24 год</b>\n"
            f"нових постів переглянуто: {seen}\nкандидатів після фільтра: {cand}\nвідправлено лідів: {leads}")
    if rejected:
        text += "\nAI відхилив, напр.: " + "; ".join(f"{html.escape(t[:50])} (r/{html.escape(sub)})" for t, sub in rejected)
    if tg_send(text):
        con.execute("INSERT OR REPLACE INTO state VALUES ('last_digest', ?)", (now.isoformat(),))
        con.commit()


# --------------------------------------------------------------------------- crawl
def crawl(cfg: dict, scorer: Scorer, ai: AI, con: sqlite3.Connection, reddit: Reddit) -> int:
    delay = cfg.get("request_delay_sec", 1.0)
    jobs = [("search", q) for q in cfg.get("search_queries", [])] + [("new", s) for s in cfg.get("subreddits", [])]
    new_leads = 0
    fetched = 0
    failed_jobs = 0
    ai_calls = ai_failures = 0
    spent_before = ai.spent_usd
    now = datetime.now(timezone.utc).isoformat()
    max_age = cfg.get("max_age_days", 0) * 86400
    cutoff = time.time() - max_age if max_age else 0
    last_error = ""

    for kind, arg in jobs:
        try:
            posts = reddit.search(arg) if kind == "search" else reddit.new_posts(arg)
        except RedditError as e:
            log.warning("%s %s failed: %s", kind, arg, e)
            failed_jobs += 1
            last_error = str(e)
            time.sleep(delay)
            continue
        fetched += len(posts)
        for p in posts:
            r = handle_post(p, scorer, ai, con, now, cutoff)
            ai_calls += r.ai_called
            ai_failures += r.ai_failed
            new_leads += r.lead
        con.commit()
        time.sleep(delay)

    new_posts = con.execute("SELECT COUNT(*) FROM seen WHERE first_seen=?", (now,)).fetchone()[0]
    log.info("cycle done: %d jobs, %d posts fetched, %d new, %d candidates, %d new leads, ai spend $%.4f",
             len(jobs), fetched, new_posts, ai_calls, new_leads, ai.spent_usd - spent_before)
    daily_digest(con)

    # ---- health verdicts for this cycle
    if jobs and failed_jobs / len(jobs) >= 0.5:
        alerts.fire("Reddit API не відповідає",
                    f"{failed_jobs} з {len(jobs)} запитів провалились. Остання помилка: {last_error[:200]}. "
                    "Перевір REDDIT_CLIENT_ID/SECRET і статус app на reddit.com/prefs/apps.")
    else:
        alerts.resolve("Reddit API не відповідає")
    if ai_calls >= 3 and ai_failures == ai_calls:
        alerts.fire("OpenRouter не відповідає", f"Всі {ai_calls} AI-запити провалились (модель {OR_MODEL}). Ліди йдуть без AI-фільтра — перевір ключ/баланс.")
    elif ai_calls:
        alerts.resolve("OpenRouter не відповідає")

    if threads_due(cfg, con):
        new_leads += crawl_threads(cfg, scorer, ai, con)
    return new_leads


def print_leads(con: sqlite3.Connection, limit: int) -> None:
    rows = con.execute(
        "SELECT score, subreddit, author, title, url, equipment, intent, created, ai_is_buyer, ai_confidence, "
        "ai_location, ai_buyer_type, ai_summary FROM leads WHERE ai_is_buyer=1 OR ai_is_buyer IS NULL "
        "ORDER BY found_at DESC, score DESC LIMIT ?", (limit,),
    ).fetchall()
    if not rows:
        print("no leads yet")
        return
    for score, sub, author, title, url, eq, intent, created, aib, conf, loc, btype, summ in rows:
        ai_line = f"     🤖 {summ}\n     📍 {loc or '?'} · 👤 {btype or '?'} · {int((conf or 0)*100)}%\n" if aib is not None else ""
        who = f"threads @{author}" if sub == "threads" else f"r/{sub} u/{author}"
        print(f"[{score:2d}] {who} {created[:10] if created else ''}\n     {title}\n{ai_line}     🔧 {eq}\n     {url}\n")


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = json.loads(CONFIG_PATH.read_text())
    scorer = Scorer(cfg)
    ai = AI(cfg)
    cmd = argv[1] if len(argv) > 1 else "run"

    if cmd == "test":
        s = scorer.score(" ".join(argv[2:]))
        print(json.dumps(s.__dict__, indent=2, ensure_ascii=False))
        return 0
    if cmd == "ai":
        text = " ".join(argv[2:])
        print(json.dumps(ai.classify(Post("test", "test", "test", text.split("\n")[0][:120], text, "/")), indent=2, ensure_ascii=False))
        return 0

    con = db_connect()
    if cmd == "leads":
        print_leads(con, int(argv[2]) if len(argv) > 2 else 30)
    elif cmd == "run":
        crawl(cfg, scorer, ai, con, Reddit())
    elif cmd == "threads":
        if threads_source is None:
            print("threads_source.py is not installed")
            return 2
        cfg.setdefault("threads", {})["enabled"] = True
        crawl_threads(cfg, scorer, ai, con)
    elif cmd == "loop":
        interval = cfg.get("interval_minutes", 10) * 60
        t = cfg.get("threads", {})
        log.info("reddit=oauth db=%s telegram=%s ai=%s interval=%ss threads=%s", DB_PATH,
                 "on" if TG_TOKEN and TG_CHAT else "off", OR_MODEL if ai.enabled else "off", interval,
                 f"every {t.get('interval_minutes', 180)}m, {len(t.get('queries', []))} queries"
                 if t.get("enabled") and threads_source else "off")
        reddit = Reddit()
        tg_send(f"🟢 reddit-monitor запущено · ai={OR_MODEL if ai.enabled else 'off'} · кожні {interval // 60} хв")
        while True:
            try:
                crawl(cfg, scorer, ai, con, reddit)
            except Exception as e:  # keep the loop alive on unexpected errors
                log.exception("cycle crashed")
                alerts.fire("цикл впав", f"{type(e).__name__}: {e}"[:500])
            time.sleep(interval)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
