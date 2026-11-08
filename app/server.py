"""Tiny local server for the Client News Digest prototype.

Serves the files in app/ and adds two routes:
  GET  /api/news      searches Google News RSS (priority sources first) and
                      returns the top 5 articles as JSON
  POST /api/feedback  appends one removal note to app/data/feedback.json
Uses only the Python standard library. No API keys.

Run from the repo root:  python3 app/server.py
Then open:               http://localhost:8000
"""

import json
import os
import re
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
APP_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDBACK_FILE = os.path.join(APP_DIR, "data", "feedback.json")
MAX_ARTICLES = 5
# Google returns ~100 loosely matching results in relevance order. Only the most
# relevant few from each search are ranked, so "newest" can't promote weak matches.
CANDIDATES_PER_SEARCH = 10
RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

feedback_lock = threading.Lock()


def build_query(terms, exclude, sites=None):
    """Combine search terms, excluded words and optional site: filters into one Google query."""
    # Parentheses keep a user's OR from merging with the site: filters.
    parts = ["(" + terms + ")"]
    # Excluded words can be single words or "quoted phrases"; each gets a leading "-".
    for word in re.findall(r'"[^"]+"|\S+', exclude):
        parts.append(word if word.startswith("-") else "-" + word)
    if sites:
        parts.append("(" + " OR ".join("site:" + site for site in sites) + ")")
    parts.append("when:7d")
    return " ".join(parts)


def search_rss(query):
    """Return every article in one Google News RSS search."""
    url = RSS_URL.format(query=urllib.parse.quote_plus(query))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-digest-prototype)"})
    with urllib.request.urlopen(request, timeout=10) as response:
        root = ET.fromstring(response.read())

    articles = []
    for item in root.iter("item"):
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        source_url = source_el.get("url", "") if source_el is not None else ""
        domain = urllib.parse.urlparse(source_url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]

        headline = item.findtext("title", default="").strip()
        # Google appends " - Source" to every title; drop it since source is shown separately.
        if source and headline.endswith(" - " + source):
            headline = headline[: -len(" - " + source)]

        date = None
        pub_date = item.findtext("pubDate")
        if pub_date:
            try:
                date = parsedate_to_datetime(pub_date)
            except (TypeError, ValueError):
                pass

        articles.append({
            "headline": headline,
            "source": source or domain or "Unknown source",
            "domain": domain,
            "url": item.findtext("link", default="").strip(),
            "date": date,
        })
    return articles


def is_priority(domain, priority_sites):
    return any(domain == site or domain.endswith("." + site) for site in priority_sites)


def fetch_news(terms, exclude, priority_sites):
    """Search priority sources first, fill in from all sources, rank, and return the top 5."""
    results = []
    if priority_sites:
        results += search_rss(build_query(terms, exclude, priority_sites))[:CANDIDATES_PER_SEARCH]
    results += search_rss(build_query(terms, exclude))[:CANDIDATES_PER_SEARCH]

    seen = set()
    articles = []
    for article in results:
        key = article["url"] or article["headline"].lower()
        if key in seen:
            continue
        seen.add(key)
        article["priority"] = is_priority(article["domain"], priority_sites)
        articles.append(article)

    # Priority sources first, then newest. Undated articles go last within each group.
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    articles.sort(key=lambda a: (not a["priority"], -(a["date"] or oldest).timestamp()))

    top = articles[:MAX_ARTICLES]
    for article in top:
        article["date"] = article["date"].isoformat() if article["date"] else ""
    return top


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
            self.send_json(502, {"error": f"Could not reach Google News ({error.__class__.__name__})."})

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
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Client News Digest running at http://localhost:{PORT}  (press Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
