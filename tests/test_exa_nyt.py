"""Offline tests for the Exa and NYT sources in app/server.py.

Run from the repo root:  python3 -m unittest discover tests
No internet or keys needed: Exa's and NYT's answers are faked, so these cost nothing.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import server  # noqa: E402

NOW = datetime.now(timezone.utc)
RECENT = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
OLD = (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
NO_KEYS = {"EXA_API_KEY": "", "NYT_API_KEY": "", "GNEWS_API_KEY": ""}


def exa_answer(*items, cost=0.017):
    return {"results": list(items), "costDollars": {"total": cost}}


def exa_item(title, url, date=RECENT, highlights=("Ledger shipped a new wallet with a 2-inch screen.",)):
    return {"title": title, "url": url, "publishedDate": date, "highlights": list(highlights)}


class QueryHelperTests(unittest.TestCase):
    def test_plain_query_drops_google_syntax(self):
        self.assertEqual(server.plain_query('"cold wallet" OR "hardware wallet" -ad'), "cold wallet or hardware wallet")

    def test_blocked_words_from_both_boxes(self):
        self.assertEqual(server.blocked_words('wallet -ad', 'TV "horror story"'), ["TV", "horror story", "ad"])

    def test_dates_from_exa_and_nyt(self):
        self.assertEqual(server.parse_date("2026-11-20T00:00:00.000Z"), datetime(2026, 11, 20, tzinfo=timezone.utc))
        self.assertEqual(server.parse_date("2026-11-20T10:30:00+0000"), datetime(2026, 11, 20, 10, 30, tzinfo=timezone.utc))
        self.assertIsNotNone(server.parse_date("2026-11-20T10:30:00.1234567Z"))  # odd fraction lengths

    def test_outlet_names(self):
        self.assertEqual(server.outlet_name("wsj.com"), "The Wall Street Journal")
        self.assertEqual(server.outlet_name("markets.ft.com"), "Financial Times")
        self.assertEqual(server.outlet_name("smallblog.io"), "smallblog.io")


class ExaTests(unittest.TestCase):
    def run_exa(self, answer, **kwargs):
        costs = []
        with mock.patch.dict(os.environ, {"EXA_API_KEY": "test-exa"}), \
             mock.patch.object(server, "post_json", return_value=answer) as post:
            articles = server.search_exa(kwargs.pop("terms", "cold wallet"), kwargs.pop("exclude", ""),
                                         costs=costs, **kwargs)
        return articles, post, costs

    def test_off_without_key(self):
        with mock.patch.dict(os.environ, NO_KEYS), mock.patch.object(server, "post_json") as post:
            self.assertEqual(server.search_exa("cold wallet", ""), [])
        post.assert_not_called()

    def test_request_follows_exa_recommended_shape(self):
        _, post, _ = self.run_exa(exa_answer(), about="crypto hardware wallets, not PC security",
                                  exclude="TV", sites=["wsj.com", "nytimes.com"], category="Hardware Security")
        url, body, headers = post.call_args[0]
        self.assertEqual(url, "https://api.exa.ai/search")
        self.assertEqual(headers, {"x-api-key": "test-exa"})
        self.assertEqual(body["type"], "auto")
        self.assertEqual(body["contents"], {"highlights": True})  # bare highlights; no full text (cost)
        self.assertNotIn("numResults", body)                        # server default (10)
        # What the product asked for: news, last 7 days, priority outlets as a hard list.
        self.assertEqual(body["category"], "news")
        self.assertEqual(body["includeDomains"], ["wsj.com", "nytimes.com"])
        since = server.parse_date(body["startPublishedDate"])
        self.assertAlmostEqual((NOW - since).days, server.DAYS_BACK, delta=1)
        # The query is retrieval intent only; the description and exclusions go in objective.
        self.assertEqual(body["query"], "latest news on cold wallet")
        self.assertIn("crypto hardware wallets, not PC security", body["objective"])
        self.assertIn("Hardware Security", body["objective"])
        self.assertIn("anything about TV", body["objective"])

    def test_open_search_has_no_domain_limit_but_names_preferred_outlets(self):
        _, post, _ = self.run_exa(exa_answer(), priority_sites=["wsj.com", "barrons.com"])
        body = post.call_args[0][1]
        self.assertNotIn("includeDomains", body)
        self.assertIn("Prefer established outlets such as wsj.com, barrons.com", body["objective"])

    def test_results_become_articles_with_highlights_and_cost(self):
        articles, _, costs = self.run_exa(exa_answer(
            exa_item("Ledger unveils new wallet", "https://www.wsj.com/tech/ledger"),
        ), sites=["wsj.com"])
        self.assertEqual(len(articles), 1)
        a = articles[0]
        self.assertEqual(a["source"], "The Wall Street Journal")
        self.assertEqual(a["domain"], "wsj.com")
        self.assertIn("2-inch screen", a["description"])
        self.assertEqual(a["via"], "Exa (priority sources)")
        self.assertEqual(costs, [0.017])

    def test_excluded_words_are_dropped(self):
        articles, _, _ = self.run_exa(exa_answer(
            exa_item("Best TV deals this week", "https://a.com/1", highlights=["Sponsored"]),
            exa_item("Trezor recalls devices", "https://a.com/2"),
        ), exclude="TV")
        self.assertEqual([a["headline"] for a in articles], ["Trezor recalls devices"])


class NytTests(unittest.TestCase):
    ANSWER = {"response": {"docs": [{
        "headline": {"main": "Coinbase Says 99% of Assets Are Offline"},
        "abstract": "The exchange disclosed how it stores customer crypto.",
        "lead_paragraph": "Coinbase said on Tuesday that more than 99 percent of customer assets sit in cold storage.",
        "web_url": "https://www.nytimes.com/2026/11/20/business/coinbase.html",
        "pub_date": "2026-11-20T10:30:00+0000",
    }]}}

    def test_off_without_key(self):
        with mock.patch.dict(os.environ, NO_KEYS), mock.patch.object(server, "fetch_url") as fetch:
            self.assertEqual(server.search_nyt("cold wallet", ""), [])
        fetch.assert_not_called()

    def test_uses_headline_abstract_and_lead_paragraph(self):
        with mock.patch.dict(os.environ, {"NYT_API_KEY": "test-nyt"}), \
             mock.patch.object(server, "fetch_url", return_value=json.dumps(self.ANSWER).encode()) as fetch:
            articles = server.search_nyt('"cold storage" -ad', "")
        url = fetch.call_args[0][0]
        self.assertTrue(url.startswith(server.NYT_URL + "?"))
        self.assertIn("api-key=test-nyt", url)
        self.assertIn("q=cold+storage", url)
        self.assertIn("begin_date=", url)
        a = articles[0]
        self.assertEqual(a["headline"], "Coinbase Says 99% of Assets Are Offline")
        self.assertIn("disclosed how it stores", a["description"])
        self.assertIn("99 percent", a["description"])
        self.assertEqual(a["source"], "The New York Times")
        self.assertEqual(a["date"], datetime(2026, 11, 20, 10, 30, tzinfo=timezone.utc))


class SourceChoiceTests(unittest.TestCase):
    """Which sources run, depending on the keys in .env."""

    def sources_used(self, env):
        calls = []

        def fake(name):
            def run(*args, **kwargs):
                calls.append(name)
                return []
            return run

        with mock.patch.dict(os.environ, {**NO_KEYS, **env}), \
             mock.patch.object(server, "search_exa", side_effect=lambda *a, **k: calls.append(
                 "Exa (priority)" if (a[3] if len(a) > 3 else k.get("sites")) else "Exa") or []), \
             mock.patch.object(server, "search_nyt", fake("NYT")), \
             mock.patch.object(server, "search_google_news", fake("Google News")), \
             mock.patch.object(server, "search_gnews", fake("GNews")), \
             mock.patch.object(server, "search_feeds", fake("Feeds")):
            search = {}
            server.gather_candidates("wallet", "", ["wsj.com"], "", search)
        return sorted(calls), search

    def test_with_exa_google_news_and_gnews_are_skipped(self):
        calls, search = self.sources_used({"EXA_API_KEY": "x", "NYT_API_KEY": "y"})
        self.assertEqual(calls, ["Exa", "Exa (priority)", "Feeds", "NYT"])
        self.assertEqual(search["cost_usd"], 0)
        self.assertIn("NYT", search["sources"])

    def test_without_exa_the_old_sources_run(self):
        calls, _ = self.sources_used({})
        self.assertEqual(calls, ["Feeds", "GNews", "Google News", "Google News"])

    def test_one_failing_source_is_reported_not_fatal(self):
        with mock.patch.dict(os.environ, {**NO_KEYS, "EXA_API_KEY": "x"}), \
             mock.patch.object(server, "search_exa", side_effect=[[], RuntimeError("Exa down")]), \
             mock.patch.object(server, "search_feeds", return_value=[]):
            search = {}
            server.gather_candidates("wallet", "", ["wsj.com"], "", search)
        self.assertEqual(len(search["failed"]), 1)
        self.assertEqual(len(search["sources"]), 2)


class PromptTests(unittest.TestCase):
    def test_category_description_reaches_claude(self):
        prompt = server.build_claude_prompt("Acme", "Hardware Security", "hardware security", [],
                                            about="crypto hardware wallets, not PC or chip security")
        self.assertIn("What this category means here: crypto hardware wallets", prompt)

    def test_no_description_line_when_blank(self):
        self.assertNotIn("What this category means", server.build_claude_prompt("Acme", "FX", "fx", []))


if __name__ == "__main__":
    unittest.main()
