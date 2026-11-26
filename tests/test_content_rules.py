"""Offline tests for the English-only and no-press-release rules in app/server.py.

Run from the repo root:  python3 -m unittest discover tests
All sources and Claude are faked, so these cost nothing.
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
NO_KEYS = {"EXA_API_KEY": "", "NYT_API_KEY": "", "GNEWS_API_KEY": "", "ANTHROPIC_API_KEY": ""}


def art(headline, domain, description="Excerpt."):
    return {"headline": headline, "description": description, "source": domain, "domain": domain,
            "url": f"https://{domain}/{abs(hash(headline))}", "date": NOW - timedelta(hours=2)}


class LanguageTests(unittest.TestCase):
    def test_non_latin_alphabets_are_caught(self):
        for text in ("比特币冷钱包安全漏洞曝光", "Ledger 发布新款硬件钱包", "Биткойн-кошелёк взломан", "محفظة باردة"):
            self.assertTrue(server.looks_non_english(text), text)

    def test_english_and_names_with_accents_pass(self):
        for text in ("Trezor recalls Safe 5 wallets", "Société Générale's crypto unit picks Ledger",
                     "Coinbase says “99%” is offline — again", ""):
            self.assertFalse(server.looks_non_english(text), text)


class PressReleaseTests(unittest.TestCase):
    def test_wire_services_from_outlets_json(self):
        for domain in ("prnewswire.com", "www.businesswire.com", "globenewswire.com"):
            self.assertTrue(server.is_press_release_wire(domain), domain)
        self.assertFalse(server.is_press_release_wire("coindesk.com"))

    def test_open_exa_search_excludes_wires_and_asks_for_english(self):
        with mock.patch.dict(os.environ, {"EXA_API_KEY": "x"}), \
             mock.patch.object(server, "post_json", return_value={"results": []}) as post:
            server.search_exa("cold wallet", "")
            open_body = post.call_args[0][1]
            server.search_exa("cold wallet", "", sites=["wsj.com"])
            priority_body = post.call_args[0][1]
        self.assertIn("prnewswire.com", open_body["excludeDomains"])
        self.assertNotIn("excludeDomains", priority_body)  # already limited to the priority outlets
        self.assertIn("Only English-language articles", open_body["objective"])
        self.assertIn("press releases", open_body["objective"])
        self.assertIn("launch is fine", open_body["objective"])  # news reports of launches stay

    def test_rubric_excludes_press_releases_but_not_launch_coverage(self):
        self.assertIn("press release", server.CLAUDE_INSTRUCTIONS)
        self.assertIn("product launch (for example a competitor's new wallet) is NOT excluded", server.CLAUDE_INSTRUCTIONS)
        self.assertIn("english", server.SCORE_SCHEMA["properties"]["articles"]["items"]["required"])

    def test_excluded_format_gets_its_own_cutoff_reason(self):
        a = {"relevance": 2, "relevance_parts": {"topic_fit": 4, "excluded_format": True}}
        self.assertIn("press release", server.cutoff_reason(a))


class GatherFilterTests(unittest.TestCase):
    def test_every_source_drops_non_english_and_wires(self):
        feed = [art("Trezor recalls Safe 5 wallets", "coindesk.com"),
                art("比特币冷钱包安全漏洞曝光", "sina.com.cn"),
                art("Acme Launches Vault X Hardware Wallet", "prnewswire.com")]
        with mock.patch.dict(os.environ, NO_KEYS), \
             mock.patch.object(server, "search_google_news", return_value=[]), \
             mock.patch.object(server, "search_gnews", return_value=[]), \
             mock.patch.object(server, "search_feeds", return_value=feed):
            search = {}
            result = server.gather_candidates("wallet", "", [], "", search)
        self.assertEqual([a["headline"] for a in result], ["Trezor recalls Safe 5 wallets"])
        self.assertEqual((search["non_english_dropped"], search["press_releases_dropped"]), (1, 1))


class ClaudeLanguageFlagTests(unittest.TestCase):
    def test_articles_claude_marks_not_english_are_dropped(self):
        cands = [art("Trezor recalls wallets", "coindesk.com"), art("Ledger lanza una nueva billetera", "elpais.com")]
        for c in cands:
            c["priority"] = False
        items = [{"id": i, "topic_fit": 4, "client_impact": 2, "newsworthiness": 2, "source_format": 2,
                  "excluded_format": False, "english": i == 0, "same_story_as": i, "reason": "r", "summary": "s"}
                 for i in range(2)]
        answer = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"articles": items})}],
                  "usage": {"input_tokens": 100, "output_tokens": 50}}
        with mock.patch.dict(os.environ, {**NO_KEYS, "ANTHROPIC_API_KEY": "test"}), \
             mock.patch.object(server, "gather_candidates", lambda *a, **k: cands), \
             mock.patch.object(server, "call_claude", return_value=answer):
            top, ai, _ = server.fetch_news("wallet", "", [], "Cold Wallet", "Ledger")
        self.assertEqual([a["headline"] for a in top], ["Trezor recalls wallets"])
        self.assertEqual(ai["non_english_dropped"], 1)
        self.assertFalse(any("_english" in a for a in top))


if __name__ == "__main__":
    unittest.main()
