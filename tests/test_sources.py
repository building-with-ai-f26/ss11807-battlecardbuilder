"""Offline tests for the news-source code in app/server.py.

Run from the repo root:  python3 -m unittest discover tests
No internet or API keys needed: every test uses the sample data below.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import server  # noqa: E402

SAMPLE_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Dollar slips as &lt;b&gt;currency&lt;/b&gt; traders eye Fed</title>
    <link>https://www.bloomberg.com/news/articles/dollar-slips</link>
    <description><![CDATA[<p>The greenback fell against the euro &amp; yen.</p>]]></description>
    <pubDate>Mon, 16 Nov 2026 03:21:26 GMT</pubDate>
  </item>
  <item>
    <title>No date here</title>
    <link>https://www.bloomberg.com/news/articles/undated</link>
  </item>
</channel></rss>"""


def article(headline, url, domain="example.com", days_old=0, via="Publisher feed"):
    return {
        "headline": headline, "description": "", "source": domain, "domain": domain,
        "url": url, "via": via,
        "date": datetime.now(timezone.utc) - timedelta(days=days_old),
    }


class ParseRssTests(unittest.TestCase):
    def test_reads_items_and_cleans_html(self):
        items = server.parse_rss(SAMPLE_FEED, default_source="Bloomberg")
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["headline"], "Dollar slips as currency traders eye Fed")
        self.assertEqual(first["description"], "The greenback fell against the euro & yen.")
        self.assertEqual(first["domain"], "bloomberg.com")
        self.assertEqual(first["source"], "Bloomberg")
        self.assertEqual(first["date"].year, 2026)

    def test_missing_date_is_none(self):
        items = server.parse_rss(SAMPLE_FEED)
        self.assertIsNone(items[1]["date"])


class MatchTermsTests(unittest.TestCase):
    def test_all_words_must_appear(self):
        self.assertTrue(server.matches_terms("Card fraud rises at banks", "card fraud", ""))
        self.assertFalse(server.matches_terms("Fraud rises at banks", "card fraud", ""))

    def test_or_and_quoted_phrases(self):
        terms = '"foreign exchange" OR currency'
        self.assertTrue(server.matches_terms("Foreign exchange desks get busy", terms, ""))
        self.assertTrue(server.matches_terms("Currency markets calm", terms, ""))
        self.assertFalse(server.matches_terms("Foreign policy and exchange students", terms, ""))

    def test_whole_words_and_plurals(self):
        self.assertTrue(server.matches_terms("Central banks cut rates", "rate", ""))
        self.assertFalse(server.matches_terms("Investors were frustrated", "rate", ""))

    def test_excluded_words(self):
        self.assertFalse(server.matches_terms("Currency podcast: what's next", "currency", "podcast"))
        self.assertFalse(server.matches_terms("Currency podcast: what's next", "currency -podcast", ""))
        self.assertTrue(server.matches_terms("Currency outlook", "currency", "podcast"))


class QueryTests(unittest.TestCase):
    def test_google_query(self):
        query = server.build_query("fraud", 'podcast "op-ed"', ["reuters.com", "ft.com"])
        self.assertEqual(query, '(fraud) -podcast -"op-ed" (site:reuters.com OR site:ft.com) when:7d')

    def test_gnews_query_uses_not(self):
        self.assertEqual(server.build_gnews_query("currency -crypto", "podcast"),
                         "(currency NOT crypto) NOT podcast")


class MergeAndRankTests(unittest.TestCase):
    def test_removes_same_story_from_two_sources(self):
        results = [
            article("Fed holds rates steady", "https://news.google.com/abc", via="Google News"),
            article("Fed Holds Rates Steady!", "https://gnews.example/xyz", via="GNews"),
            article("Different story", "https://example.com/other"),
        ]
        top = server.merge_and_rank(results, [])
        self.assertEqual(sorted(a["headline"] for a in top), ["Different story", "Fed holds rates steady"])

    def test_priority_first_then_newest(self):
        results = [
            article("Old priority", "https://reuters.com/1", domain="reuters.com", days_old=3),
            article("New other", "https://example.com/2", days_old=0),
            article("New priority", "https://reuters.com/3", domain="reuters.com", days_old=1),
        ]
        top = server.merge_and_rank(results, ["reuters.com"])
        self.assertEqual([a["headline"] for a in top], ["New priority", "Old priority", "New other"])
        self.assertTrue(top[0]["priority"])

    def test_drops_articles_older_than_7_days_or_undated(self):
        undated = article("No date", "https://example.com/3")
        undated["date"] = None
        results = [
            article("This week", "https://example.com/1", days_old=6),
            article("From 2015", "https://example.com/2", days_old=3950),
            article("Eight days old", "https://example.com/4", days_old=8),
            undated,
        ]
        self.assertEqual([a["headline"] for a in server.merge_and_rank(results, [])], ["This week"])

    def test_keeps_only_top_five(self):
        results = [article(f"Story {i}", f"https://example.com/{i}") for i in range(8)]
        self.assertEqual(len(server.merge_and_rank(results, [])), 5)


class FeedSearchTests(unittest.TestCase):
    def test_filters_by_terms_and_week(self):
        cached = [
            article("Currency traders brace", "https://ft.com/1"),
            article("Currency news from last month", "https://ft.com/2", days_old=30),
            article("Sports results", "https://ft.com/3"),
        ]
        original = server.publisher_feed_articles
        server.publisher_feed_articles = lambda: [dict(a) for a in cached]
        try:
            found = server.search_feeds("currency", "")
        finally:
            server.publisher_feed_articles = original
        self.assertEqual([a["headline"] for a in found], ["Currency traders brace"])


class ClaudeStepTests(unittest.TestCase):
    """Claude is replaced by a fake response, so these tests cost nothing."""

    def fake_response(self, items, stop_reason="end_turn"):
        import json
        return {
            "stop_reason": stop_reason,
            "content": [{"type": "thinking", "thinking": ""},
                        {"type": "text", "text": json.dumps({"articles": items})}],
            "usage": {"input_tokens": 2000, "output_tokens": 1000},
        }

    def run_with(self, response, articles):
        original = server.call_claude
        server.call_claude = lambda prompt: response
        try:
            return server.score_with_claude("Acme Payments", "FX", "currency", articles)
        finally:
            server.call_claude = original

    def test_scores_summaries_and_cost(self):
        articles = [article("Dollar falls", "https://ft.com/1"), article("Recipe of the week", "https://ft.com/2")]
        articles[0]["description"] = "The dollar fell 1% against the euro."
        info = self.run_with(self.fake_response([
            {"id": 0, "topic_fit": 4, "client_impact": 1, "newsworthiness": 2, "source_format": 2,
             "excluded_format": False, "summary": "The dollar fell 1% against the euro."},
            {"id": 1, "topic_fit": 0, "client_impact": 0, "newsworthiness": 1, "source_format": 2,
             "excluded_format": True, "summary": "A recipe."},
        ]), articles)
        self.assertEqual(articles[0]["relevance"], 9)
        self.assertEqual(articles[0]["relevance_parts"]["topic_fit"], 4)
        self.assertEqual(articles[1]["relevance"], 2)  # excluded format: capped at 2
        self.assertEqual(articles[0]["summary"], "The dollar fell 1% against the euro.")
        # 2000 input tokens x $0.10/M + 1000 output tokens x $0.50/M = $0.0007
        self.assertAlmostEqual(info["cost_usd"], 0.0007)

    def test_rubric_adds_parts_and_keeps_ranges(self):
        total, parts = server.rubric_score({"topic_fit": 4, "client_impact": 2, "newsworthiness": 1,
                                            "source_format": 2, "excluded_format": False})
        self.assertEqual(total, 9)
        self.assertEqual(parts["newsworthiness"], 1)
        # Out-of-range parts are pulled back into range (topic_fit max 4, others max 2, min 0).
        total, _ = server.rubric_score({"topic_fit": 9, "client_impact": 5, "newsworthiness": -1,
                                        "source_format": 2, "excluded_format": False})
        self.assertEqual(total, 8)

    def test_opinion_and_podcasts_capped_at_two(self):
        total, parts = server.rubric_score({"topic_fit": 4, "client_impact": 2, "newsworthiness": 2,
                                            "source_format": 2, "excluded_format": True})
        self.assertEqual(total, 2)
        self.assertTrue(parts["excluded_format"])

    def test_prompt_lists_companies_to_watch(self):
        prompt = server.build_claude_prompt("Acme Payments", "FX", "currency",
                                            [article("Dollar falls", "https://ft.com/1")], ["Stripe", "Adyen"])
        self.assertIn("Companies to watch: Stripe, Adyen", prompt)
        self.assertIn("[0] Dollar falls", prompt)

    def test_rank_puts_best_score_first_and_flags_low(self):
        articles = [article("Weak", "https://a.com/1"), article("Strong", "https://a.com/2")]
        for a in articles:
            a["priority"] = False
        articles[0]["relevance"], articles[1]["relevance"] = 2, 8
        top = server.rank_by_relevance(articles)
        self.assertEqual([a["headline"] for a in top], ["Strong", "Weak"])
        self.assertEqual([a["low_relevance"] for a in top], [False, True])

    def test_refusal_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_with(self.fake_response([], stop_reason="refusal"), [article("x", "https://a.com")])

    def test_falls_back_when_claude_fails(self):
        cands = [article(f"Story {i}", f"https://a.com/{i}") for i in range(3)]
        for a in cands:
            a["priority"] = False
        saved = (server.gather_candidates, server.call_claude, os.environ.get("ANTHROPIC_API_KEY"))
        server.gather_candidates = lambda *args: cands
        server.call_claude = lambda prompt: (_ for _ in ()).throw(RuntimeError("Claude API error 401"))
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        try:
            top, ai = server.fetch_news("story", "", [])
        finally:
            server.gather_candidates, server.call_claude = saved[0], saved[1]
            if saved[2] is None:
                os.environ.pop("ANTHROPIC_API_KEY")
            else:
                os.environ["ANTHROPIC_API_KEY"] = saved[2]
        self.assertEqual(len(top), 3)
        self.assertFalse(ai["used"])
        self.assertIn("401", ai["error"])
        self.assertIsInstance(top[0]["date"], str)  # dates are turned into text for the web page


class PdfTests(unittest.TestCase):
    """PDF upload: Claude is replaced by a fake that records what it was sent."""

    def test_rejects_bad_uploads(self):
        with self.assertRaises(ValueError):
            server.check_pdf(b"")
        with self.assertRaises(ValueError):
            server.check_pdf(b"<html>not a pdf</html>")
        with self.assertRaises(ValueError):
            server.check_pdf(b"%PDF" + b"0" * (server.MAX_PDF_BYTES + 1))
        server.check_pdf(b"%PDF-1.4 small file")  # a real PDF header passes

    def test_sends_pdf_and_returns_fields(self):
        import json
        sent = {}

        def fake_claude(content, **options):
            sent["content"], sent["options"] = content, options
            answer = {"headline": " Visa buys a fraud startup ", "source": "WSJ",
                      "summary": "Visa agreed to buy FraudCo for $1 billion."}
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(answer)}],
                    "usage": {"input_tokens": 4000, "output_tokens": 200}}

        original = server.call_claude
        server.call_claude = fake_claude
        try:
            result = server.summarize_pdf(b"%PDF-1.4 fake article")
        finally:
            server.call_claude = original
        self.assertEqual(result["headline"], "Visa buys a fraud startup")
        self.assertEqual(result["source"], "WSJ")
        self.assertAlmostEqual(result["cost_usd"], 0.0005)  # 4000 x $0.10/M + 200 x $0.50/M
        document = sent["content"][0]
        self.assertEqual(document["type"], "document")
        self.assertEqual(document["source"]["media_type"], "application/pdf")
        self.assertIs(sent["options"]["schema"], server.PDF_SCHEMA)


class GnewsTests(unittest.TestCase):
    def test_off_without_key(self):
        saved = os.environ.pop("GNEWS_API_KEY", None)
        try:
            self.assertEqual(server.search_gnews("fraud", ""), [])
        finally:
            if saved is not None:
                os.environ["GNEWS_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
