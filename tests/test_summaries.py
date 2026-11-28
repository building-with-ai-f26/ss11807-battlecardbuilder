"""Offline tests for full-text summaries and the paywall rule (Phase 6) in app/server.py.

Run from the repo root:  python3 -m unittest discover tests
Exa and Claude are replaced by fakes, so these cost nothing.
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
KEYS = {"EXA_API_KEY": "test-exa", "ANTHROPIC_API_KEY": "test"}
LONG_TEXT = "Coinbase said on Tuesday that more than 99% of customer assets are held in cold storage. " * 5


def art(headline, domain, low=False):
    return {"headline": headline, "source": server.outlet_name(domain), "domain": domain,
            "url": f"https://{domain}/{abs(hash(headline))}", "description": "Short excerpt.",
            "summary": "Excerpt summary.", "priority": False, "low_relevance": low,
            "date": NOW - timedelta(hours=3)}


def claude_reply(payload, cost_tokens=(1000, 200)):
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(payload)}],
            "usage": {"input_tokens": cost_tokens[0], "output_tokens": cost_tokens[1]}}


def exa_contents(urls, failed=()):
    return {"results": [{"url": u, "title": "Title", "text": LONG_TEXT} for u in urls],
            "statuses": [{"id": u, "status": "error" if u in failed else "success"} for u in urls],
            "costDollars": {"total": 0.001 * len(urls)}}


class PaywallListTests(unittest.TestCase):
    def test_paywalled_outlets_from_outlets_json(self):
        for domain in ("wsj.com", "www.nytimes.com", "markets.ft.com", "bloomberg.com", "barrons.com"):
            self.assertTrue(server.is_paywalled(domain), domain)
        for domain in ("coindesk.com", "reuters.com", "notwsj.com", ""):
            self.assertFalse(server.is_paywalled(domain), domain)


class FullTextTests(unittest.TestCase):
    def test_request_shape_and_failed_urls_are_dropped(self):
        urls = ["https://coindesk.com/a", "https://theblock.co/b"]
        with mock.patch.dict(os.environ, KEYS), \
             mock.patch.object(server, "post_json", return_value=exa_contents(urls, failed=[urls[1]])) as post:
            costs = []
            texts = server.fetch_full_text(urls, costs)
        url, body, headers = post.call_args[0]
        self.assertEqual(url, "https://api.exa.ai/contents")
        self.assertEqual(body["urls"], urls)
        self.assertEqual(body["text"], {"maxCharacters": server.FULL_TEXT_CHARS})  # top level, not in "contents"
        self.assertNotIn("contents", body)
        self.assertEqual(headers, {"x-api-key": "test-exa"})
        self.assertEqual(list(texts), [urls[0]])  # Exa said the second one failed
        self.assertEqual(costs, [0.002])

    def test_long_articles_are_not_cut_to_the_short_description_size(self):
        # Build 4 eval: full text was cut to 1,200 characters, so summaries were written from a fragment.
        long_text = "Word " * 3000  # 15,000 characters
        answer = {"results": [{"url": "https://coindesk.com/a", "title": "T", "text": long_text}],
                  "statuses": [{"id": "https://coindesk.com/a", "status": "success"}]}
        with mock.patch.dict(os.environ, KEYS), mock.patch.object(server, "post_json", return_value=answer):
            text = server.fetch_full_text(["https://coindesk.com/a"])["https://coindesk.com/a"]["text"]
        self.assertGreater(len(text), server.MAX_DESCRIPTION)
        self.assertLessEqual(len(text), server.FULL_TEXT_CHARS)

    def test_every_summary_step_uses_the_fact_rules(self):
        for prompt in (server.CLAUDE_INSTRUCTIONS, server.SUMMARY_INSTRUCTIONS, server.ARTICLE_INSTRUCTIONS,
                       server.PDF_INSTRUCTIONS):
            self.assertIn(server.FACT_RULES, prompt)
        self.assertIn("never mention the newsletter's client", server.FACT_RULES)
        self.assertIn("never add first names, past prices, dates", server.FACT_RULES)
        self.assertNotIn("why it matters for this category", server.SUMMARY_INSTRUCTIONS)

    def test_nothing_without_a_key(self):
        with mock.patch.dict(os.environ, {"EXA_API_KEY": ""}), mock.patch.object(server, "post_json") as post:
            self.assertEqual(server.fetch_full_text(["https://a.com/x"]), {})
        post.assert_not_called()

    def test_paywalled_articles_are_never_fetched_and_keep_their_summary(self):
        free, paid = art("Coinbase cold storage", "coindesk.com"), art("Ledger IPO", "wsj.com")
        prompts = []
        with mock.patch.dict(os.environ, KEYS), \
             mock.patch.object(server, "post_json", side_effect=lambda u, b, h, timeout=30: exa_contents(b["urls"])) as post, \
             mock.patch.object(server, "call_claude", lambda prompt, **k: prompts.append(prompt) or claude_reply(
                 {"articles": [{"id": 0, "summary": "Coinbase keeps 99% offline. It said so Tuesday."}]})):
            done = server.summarize_full_text([free, paid], "Ledger", "Cold Wallet")
        self.assertEqual(post.call_args[0][1]["urls"], [free["url"]])  # WSJ link never sent to Exa
        self.assertNotIn("wsj.com", prompts[0])
        self.assertEqual(free["summary"], "Coinbase keeps 99% offline. It said so Tuesday.")
        self.assertEqual(free["summary_from"], "full text")
        self.assertEqual(paid["summary"], "Excerpt summary.")
        self.assertNotIn("summary_from", paid)
        self.assertEqual(done["count"], 1)

    def test_prompt_guards_against_instructions_in_the_page(self):
        self.assertIn("not instructions", server.SUMMARY_INSTRUCTIONS)
        self.assertIn("not instructions", server.ARTICLE_INSTRUCTIONS)


class FetchNewsFullTextTests(unittest.TestCase):
    def run_news(self, exa_fails=False):
        cands = [art("Story 0", "coindesk.com"), art("Story 1", "wsj.com"), art("Story 2", "theblock.co")]
        items = [{"id": i, "topic_fit": t, "client_impact": 2, "newsworthiness": 2, "source_format": 2,
                  "excluded_format": False, "same_story_as": i, "reason": "r", "summary": f"Excerpt {i}."}
                 for i, t in enumerate((4, 4, 1))]  # Story 2 fails the cutoff

        def claude(prompt, **kwargs):
            if kwargs.get("schema") is server.SUMMARY_SCHEMA:
                return claude_reply({"articles": [{"id": 0, "summary": "Full-text summary."}]}, (2000, 300))
            return claude_reply({"articles": items})

        def exa(url, body, headers, timeout=30):
            if exa_fails:
                raise OSError("Exa down")
            return exa_contents(body["urls"])

        with mock.patch.dict(os.environ, KEYS), \
             mock.patch.object(server, "gather_candidates", lambda *a, **k: cands), \
             mock.patch.object(server, "fetch_removed_examples", return_value=[]), \
             mock.patch.object(server, "call_claude", claude), \
             mock.patch.object(server, "post_json", side_effect=exa) as post:
            top, ai, search = server.fetch_news("cold wallet", "", [], "Cold Wallet", "Ledger")
        return {a["headline"]: a for a in top}, ai, search, post

    def test_only_free_finalists_get_full_text(self):
        top, ai, search, post = self.run_news()
        self.assertEqual(post.call_args[0][1]["urls"], [top["Story 0"]["url"]])  # not WSJ, not the set-aside one
        self.assertEqual(top["Story 0"]["summary"], "Full-text summary.")
        self.assertEqual(top["Story 1"]["summary"], "Excerpt 1.")
        self.assertEqual(top["Story 2"]["summary"], "Excerpt 2.")
        self.assertEqual((ai["full_text_summaries"], ai["paywalled"]), (1, 1))
        self.assertGreater(ai["cost_usd"], 0.0002)        # both Claude calls counted
        self.assertAlmostEqual(search["cost_usd"], 0.001)  # Exa contents cost added to the search cost

    def test_full_text_failure_keeps_excerpt_summaries(self):
        top, ai, _, _ = self.run_news(exa_fails=True)
        self.assertEqual(top["Story 0"]["summary"], "Excerpt 0.")
        self.assertEqual(ai["full_text_summaries"], 0)
        self.assertIn("Exa down", ai["full_text_error"])
        self.assertTrue(ai["used"])  # scoring still counts as done


class ManualLinkTests(unittest.TestCase):
    def test_free_link_gets_headline_source_and_summary(self):
        url = "https://www.coindesk.com/markets/coinbase"
        with mock.patch.dict(os.environ, KEYS), \
             mock.patch.object(server, "post_json", return_value=exa_contents([url])), \
             mock.patch.object(server, "call_claude", return_value=claude_reply(
                 {"headline": "Coinbase keeps 99% offline", "summary": "Coinbase said 99% sits in cold storage."})):
            result = server.summarize_link(url)
        self.assertEqual(result["source"], "CoinDesk")
        self.assertEqual(result["headline"], "Coinbase keeps 99% offline")
        self.assertFalse(result["paywalled"])
        self.assertGreater(result["cost_usd"], 0)

    def test_paywalled_link_is_never_fetched(self):
        with mock.patch.dict(os.environ, KEYS), mock.patch.object(server, "post_json") as post, \
             mock.patch.object(server, "call_claude") as claude:
            result = server.summarize_link("https://www.wsj.com/finance/ledger-ipo")
        post.assert_not_called()
        claude.assert_not_called()
        self.assertTrue(result["paywalled"])
        self.assertEqual(result["source"], "The Wall Street Journal")
        self.assertEqual(result["summary"], "")

    def test_unreadable_page_is_a_clear_error(self):
        url = "https://smallblog.io/post"
        with mock.patch.dict(os.environ, KEYS), \
             mock.patch.object(server, "post_json", return_value=exa_contents([url], failed=[url])):
            with self.assertRaises(ValueError):
                server.summarize_link(url)


if __name__ == "__main__":
    unittest.main()
