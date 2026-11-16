"""Tiny local server for the Press Pass prototype.

Serves the files in app/ and adds two routes:
  GET  /api/news      gathers this week's articles for one category from
                      three kinds of sources, removes duplicates, ranks them
                      (priority sources first, then newest) and returns the
                      top 5 as JSON:
                        1. Google News RSS search (no key needed)
                        2. Publisher RSS feeds listed in app/feeds.json (no key)
                        3. GNews API search (only if GNEWS_API_KEY is set)
  POST /api/feedback  appends one removal note to app/data/feedback.json

Uses only the Python standard library. API keys are read from a .env file in
the repo root (see .env.example); it is never committed.

Run from the repo root:  python3 app/server.py
Then open:               http://localhost:8000
"""

import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(APP_DIR, "..", ".env")
FEEDS_FILE = os.path.join(APP_DIR, "feeds.json")
FEEDBACK_FILE = os.path.join(APP_DIR, "data", "feedback.json")
MAX_ARTICLES = 5
# Google returns ~100 loosely matching results in relevance order. Only the most
# relevant few from each search are ranked, so "newest" can't promote weak matches.
CANDIDATES_PER_SEARCH = 10
DAYS_BACK = 7
FEED_CACHE_SECONDS = 600  # publisher feeds are re-downloaded at most every 10 minutes
MAX_DESCRIPTION = 600     # characters of description kept per article
USER_AGENT = "Mozilla/5.0 (press-pass-prototype)"
RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
GNEWS_URL = "https://gnews.io/api/v4/search"

feedback_lock = threading.Lock()
feed_cache_lock = threading.Lock()
feed_cache = {"fetched_at": 0.0, "articles": []}


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------
def load_env(path=ENV_FILE):
    """Read KEY=VALUE lines from .env into os.environ (real environment variables win)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def clean_text(text):
    """Turn an RSS description (often HTML) into short plain text."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:MAX_DESCRIPTION]


def domain_of(url):
    domain = urllib.parse.urlparse(url).netloc.lower()
    return domain[4:] if domain.startswith("www.") else domain


def parse_date(text):
    """Parse an RSS (RFC 822) or ISO 8601 date; return None if it can't be read."""
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_url(url, timeout=10):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# ---------------------------------------------------------------------------
# Search terms
# ---------------------------------------------------------------------------
def split_terms(text):
    """Split search text into words and "quoted phrases"."""
    return [t.strip('"') if t.startswith('"') else t for t in re.findall(r'"[^"]+"|\S+', text)]


def build_query(terms, exclude, sites=None):
    """Combine search terms, excluded words and optional site: filters into one Google query."""
    # Parentheses keep a user's OR from merging with the site: filters.
    parts = ["(" + terms + ")"]
    # Excluded words can be single words or "quoted phrases"; each gets a leading "-".
    for word in re.findall(r'"[^"]+"|\S+', exclude):
        parts.append(word if word.startswith("-") else "-" + word)
    if sites:
        parts.append("(" + " OR ".join("site:" + site for site in sites) + ")")
    parts.append(f"when:{DAYS_BACK}d")
    return " ".join(parts)


def build_gnews_query(terms, exclude):
    """GNews uses AND / OR / NOT instead of Google's minus sign."""
    parts = []
    for token in re.findall(r'"[^"]+"|\S+', terms):
        if token.startswith("-") and len(token) > 1:
            parts.append("NOT " + token[1:])
        else:
            parts.append(token)
    query = "(" + " ".join(parts) + ")"
    for word in re.findall(r'"[^"]+"|\S+', exclude):
        query += " NOT " + word.lstrip("-")
    return query


def has_term(text, term):
    """Whole-word, case-insensitive match; also accepts a plural ending ("rate" matches "rates")."""
    return re.search(r"(?<!\w)" + re.escape(term) + r"(s|es)?(?!\w)", text, re.IGNORECASE) is not None


def matches_terms(text, terms, exclude):
    """Match text the way Google reads the search terms.

    Words separated by spaces must all appear; OR separates alternatives;
    a leading minus (or the exclude box) removes articles containing that word.
    Example: '"foreign exchange" OR currency' matches either phrase.
    """
    blocked = [t.lstrip("-") for t in split_terms(exclude)]
    groups, current = [], []
    for token in re.findall(r'"[^"]+"|\S+', terms):
        if token == "OR":
            groups.append(current)
            current = []
        elif token.startswith("-") and len(token) > 1:
            blocked.append(token[1:].strip('"'))
        elif token != "AND":
            current.append(token.strip('"').strip("()"))
    groups.append(current)

    if any(word and has_term(text, word) for word in blocked):
        return False
    return any(group and all(has_term(text, word) for word in group if word) for group in groups)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def parse_rss(xml_bytes, default_source=""):
    """Return one dict per <item> in an RSS 2.0 feed."""
    root = ET.fromstring(xml_bytes)
    articles = []
    for item in root.iter("item"):
        url = item.findtext("link", default="").strip()
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        source_url = source_el.get("url", "") if source_el is not None else ""
        articles.append({
            "headline": clean_text(item.findtext("title", default="")),
            "description": clean_text(item.findtext("description", default="")),
            "source": source or default_source,
            "domain": domain_of(source_url or url),
            "url": url,
            "date": parse_date(item.findtext("pubDate")),
        })
    return articles


def search_google_news(query):
    """Return every article in one Google News RSS search."""
    articles = parse_rss(fetch_url(RSS_URL.format(query=urllib.parse.quote_plus(query))))
    for article in articles:
        # Google appends " - Source" to every title; drop it since source is shown separately.
        suffix = " - " + article["source"]
        if article["source"] and article["headline"].endswith(suffix):
            article["headline"] = article["headline"][: -len(suffix)]
        # Google's description is just the headline again as a link, so it adds nothing.
        article["description"] = ""
        article["source"] = article["source"] or article["domain"] or "Unknown source"
        article["via"] = "Google News"
    return articles


def load_feed_list():
    with open(FEEDS_FILE, encoding="utf-8") as f:
        return json.load(f).get("feeds", [])


def fetch_one_feed(feed):
    try:
        articles = parse_rss(fetch_url(feed["url"]), default_source=feed["source"])
    except Exception as error:  # one broken feed shouldn't stop the others
        print(f"  feed skipped: {feed['source']} {feed.get('name', '')} ({error.__class__.__name__})")
        return []
    for article in articles:
        article["source"] = feed["source"]
        article["via"] = "Publisher feed"
    return articles


def publisher_feed_articles():
    """All articles from the feeds in feeds.json, downloaded in parallel and cached for 10 minutes."""
    with feed_cache_lock:
        if time.time() - feed_cache["fetched_at"] > FEED_CACHE_SECONDS:
            feeds = load_feed_list()
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = pool.map(fetch_one_feed, feeds)
            feed_cache["articles"] = [a for feed_articles in results for a in feed_articles]
            feed_cache["fetched_at"] = time.time()
        return [dict(a) for a in feed_cache["articles"]]  # copies, so callers can't change the cache


def search_feeds(terms, exclude):
    """Publisher-feed articles from the last week that match the search terms."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    matches = [
        a for a in publisher_feed_articles()
        if a["date"] and a["date"] >= cutoff
        and matches_terms(a["headline"] + " " + a["description"], terms, exclude)
    ]
    matches.sort(key=lambda a: a["date"], reverse=True)
    return matches[:CANDIDATES_PER_SEARCH]


def search_gnews(terms, exclude):
    """GNews API search. Returns [] when no GNEWS_API_KEY is set."""
    key = os.environ.get("GNEWS_API_KEY", "").strip()
    if not key:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = urllib.parse.urlencode({
        "q": build_gnews_query(terms, exclude)[:200],  # GNews rejects queries over 200 characters
        "lang": "en",
        "max": CANDIDATES_PER_SEARCH,
        "from": since,
        "apikey": key,
    })
    data = json.loads(fetch_url(f"{GNEWS_URL}?{params}"))
    articles = []
    for item in data.get("articles", []):
        source = item.get("source") or {}
        url = item.get("url", "")
        articles.append({
            "headline": clean_text(item.get("title", "")),
            "description": clean_text(item.get("description", "")),
            "source": source.get("name") or domain_of(url),
            "domain": domain_of(source.get("url") or url),
            "url": url,
            "date": parse_date(item.get("publishedAt")),
            "via": "GNews",
        })
    return articles


# ---------------------------------------------------------------------------
# Combine and rank
# ---------------------------------------------------------------------------
def is_priority(domain, priority_sites):
    return any(domain == site or domain.endswith("." + site) for site in priority_sites)


def headline_key(headline):
    """Lowercase letters and digits only, so the same story from two sources is spotted."""
    return re.sub(r"[^a-z0-9]", "", headline.lower())


def merge_and_rank(results, priority_sites, limit=MAX_ARTICLES):
    """Remove duplicates, mark priority sources, sort, and keep the top few."""
    seen = set()
    articles = []
    for article in results:
        keys = {article["url"], headline_key(article["headline"])} - {""}
        if not keys or keys & seen:
            continue
        seen |= keys
        article["priority"] = is_priority(article["domain"], priority_sites)
        articles.append(article)

    # Priority sources first, then newest. Undated articles go last within each group.
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    articles.sort(key=lambda a: (not a["priority"], -(a["date"] or oldest).timestamp()))

    top = articles[:limit]
    for article in top:
        article["date"] = article["date"].isoformat() if article["date"] else ""
    return top


def fetch_news(terms, exclude, priority_sites):
    """Search every source for one category and return the top 5 articles."""
    searches = []
    if priority_sites:
        searches.append(("Google News (priority sources)",
                         lambda: search_google_news(build_query(terms, exclude, priority_sites))[:CANDIDATES_PER_SEARCH]))
    searches.append(("Google News", lambda: search_google_news(build_query(terms, exclude))[:CANDIDATES_PER_SEARCH]))
    searches.append(("Publisher feeds", lambda: search_feeds(terms, exclude)))
    searches.append(("GNews", lambda: search_gnews(terms, exclude)))

    # Run the searches at the same time; if one source fails, keep the rest.
    results, failures = [], []
    with ThreadPoolExecutor(max_workers=len(searches)) as pool:
        futures = [(name, pool.submit(search)) for name, search in searches]
        for name, future in futures:
            try:
                results += future.result()
            except Exception as error:
                failures.append(f"{name} ({error.__class__.__name__})")
                print(f"  source failed: {name} ({error.__class__.__name__}: {error})")
    if len(failures) == len(searches):
        raise RuntimeError("every news source failed: " + ", ".join(failures))
    return merge_and_rank(results, priority_sites)


def save_feedback(entry):
    """Append one feedback entry to app/data/feedback.json."""
    with feedback_lock:
        os.makedirs(os.path.dirname(FEEDBACK_FILE), exist_ok=True)
        entries = []
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, encoding="utf-8") as f:
                entries = json.load(f)
        entries.append(entry)
        with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=APP_DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/data/"):
            return self.send_json(404, {"error": "Not found."})  # keep feedback notes off the web page
        if parsed.path != "/api/news":
            return super().do_GET()

        params = urllib.parse.parse_qs(parsed.query)
        terms = params.get("terms", [""])[0].strip()
        exclude = params.get("exclude", [""])[0].strip()
        priority = [s for s in params.get("priority", [""])[0].split(",") if s]
        if not terms:
            return self.send_json(400, {"error": "Missing search terms."})
        try:
            self.send_json(200, {"articles": fetch_news(terms, exclude, priority)})
        except Exception as error:  # network failure, timeout, bad XML, etc.
            self.send_json(502, {"error": f"Could not reach any news source ({error.__class__.__name__})."})

    def do_POST(self):
        if self.path != "/api/feedback":
            return self.send_json(404, {"error": "Not found."})
        try:
            length = int(self.headers.get("Content-Length", 0))
            entry = json.loads(self.rfile.read(min(length, 20000)))
            if not isinstance(entry, dict):
                raise ValueError("Feedback must be a JSON object.")
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {"error": "Invalid feedback."})

        entry["saved_at"] = datetime.now(timezone.utc).isoformat()
        try:
            save_feedback(entry)
        except (OSError, ValueError) as error:
            return self.send_json(500, {"error": f"Could not save feedback ({error.__class__.__name__})."})
        self.send_json(200, {"ok": True})

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    load_env()
    print("GNews:", "on" if os.environ.get("GNEWS_API_KEY") else "off (add GNEWS_API_KEY to .env to turn it on)")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Press Pass running at http://localhost:{PORT}  (press Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
