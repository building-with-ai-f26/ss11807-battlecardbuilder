"""Offline tests for transparent, stricter scoring (Phase 5) in app/server.py.

Run from the repo root:  python3 -m unittest discover tests
Claude and Supabase are replaced by fakes, so these cost nothing.
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import server  # noqa: E402

NOW = datetime.now(timezone.utc)
FAKE_ENV = {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
            "ANTHROPIC_API_KEY": "test"}


def art(headline, priority=False, hours_old=5):
    return {"headline": headline, "source": "CoinDesk", "domain": "coindesk.com", "url": f"https://coindesk.com/{headline}",
            "description": "Some excerpt.", "priority": priority, "date": NOW - timedelta(hours=hours_old)}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


NOTES = [
    {"client_name": "Convera", "category": "FX", "headline": "Other client note", "source": "FT", "reasons": ["Off-topic"], "note": ""},
    {"client_name": "Ledger", "category": "Digital Privacy", "headline": "Spirit Airlines data sale", "source": "Reuters", "reasons": ["Off-topic"], "note": "not crypto"},
    {"client_name": "ledger", "category": "Hardware Security", "headline": "Windows 11 zero-day unlocks BitLocker", "source": "BleepingComputer", "reasons": ["Off-topic"], "note": "PC security, not wallets"},
    {"client_name": "Ledger", "category": "Hardware Security", "headline": "China's AI firms addicted to Nvidia chips", "source": "FT", "reasons": [], "note": ""},
]


class PromptTests(unittest.TestCase):
    def test_marks_priority_outlets_and_age(self):
        prompt = server.build_claude_prompt("Ledger", "Cold Wallet", "cold wallet",
                                            [art("A", priority=True, hours_old=3), art("B", hours_old=50)])
        self.assertIn("[0] A (CoinDesk [priority outlet], 3 hours ago)", prompt)
        self.assertIn("[1] B (CoinDesk, 2 days ago)", prompt)

    def test_lists_past_removals_with_reasons(self):
        prompt = server.build_claude_prompt("Ledger", "Hardware Security", "hardware security", [art("A")],
                                            removed=NOTES[2:])
        self.assertIn("Articles this user removed before, and why (apply each reason as explained above)", prompt)
        self.assertIn('"Windows 11 zero-day unlocks BitLocker" (BleepingComputer), Hardware Security: Off-topic; PC security, not wallets', prompt)
        self.assertIn("no reason given", prompt)

    def test_no_removals_section_when_there_are_none(self):
        self.assertNotIn("removed before", server.build_claude_prompt("Ledger", "X", "x", [art("A")]))

    def test_claude_must_give_a_reason(self):
        item = server.SCORE_SCHEMA["properties"]["articles"]["items"]
        self.assertIn("reason", item["required"])
        self.assertIn("reason:", server.CLAUDE_INSTRUCTIONS)


class RemovedExamplesTests(unittest.TestCase):
    def fetch(self, notes, client="Ledger", category="Hardware Security", token="tok", **kwargs):
        with mock.patch.dict(os.environ, FAKE_ENV), \
             mock.patch.object(server.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(notes).encode())) as urlopen:
            result = server.fetch_removed_examples(token, client, category, **kwargs)
        return result, urlopen

    def test_reads_own_notes_with_the_users_sign_in(self):
        _, urlopen = self.fetch(NOTES)
        request = urlopen.call_args[0][0]
        self.assertTrue(request.full_url.startswith("https://example.supabase.co/rest/v1/feedback?"))
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")  # RLS: only this user's notes
        self.assertEqual(request.get_header("Apikey"), "sb_publishable_test")

    def test_same_category_first_then_same_client_only(self):
        result, _ = self.fetch(NOTES)
        self.assertEqual([n["headline"] for n in result],
                         ["Windows 11 zero-day unlocks BitLocker", "China's AI firms addicted to Nvidia chips",
                          "Spirit Airlines data sale"])  # Convera's note is left out

    def test_limit(self):
        result, _ = self.fetch(NOTES, limit=1)
        self.assertEqual(len(result), 1)

    def test_nothing_without_a_token_or_client(self):
        with mock.patch.object(server.urllib.request, "urlopen") as urlopen:
            self.assertEqual(server.fetch_removed_examples("", "Ledger", "X"), [])
            self.assertEqual(server.fetch_removed_examples("tok", "", "X"), [])
        urlopen.assert_not_called()

    def test_errors_mean_scoring_without_examples(self):
        with mock.patch.dict(os.environ, FAKE_ENV), \
             mock.patch.object(server.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")):
            self.assertEqual(server.fetch_removed_examples("tok", "Ledger", "X"), [])


class FetchNewsScoringTests(unittest.TestCase):
    def test_reason_cutoff_and_examples_reach_the_page(self):
        cands = [art(f"Story {i}") for i in range(3)]
        items = [  # one strong, one off-topic, one weak
            {"id": 0, "topic_fit": 4, "client_impact": 2, "newsworthiness": 2, "source_format": 1, "excluded_format": False,
             "same_story_as": 0, "reason": "Squarely about cold storage and names Coinbase.", "summary": "S0."},
            {"id": 1, "topic_fit": 1, "client_impact": 2, "newsworthiness": 2, "source_format": 2, "excluded_format": False,
             "same_story_as": 1, "reason": "About PC security, not crypto wallets.", "summary": "S1."},
            {"id": 2, "topic_fit": 3, "client_impact": 1, "newsworthiness": 1, "source_format": 1, "excluded_format": False,
             "same_story_as": 2, "reason": "On topic but a routine recap.", "summary": "S2."},
        ]
        answer = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"articles": items})}],
                  "usage": {"input_tokens": 1000, "output_tokens": 300}}
        prompts = []
        with mock.patch.dict(os.environ, FAKE_ENV), \
             mock.patch.object(server, "gather_candidates", lambda *a, **k: cands), \
             mock.patch.object(server, "fetch_removed_examples", return_value=NOTES[2:3]) as removed, \
             mock.patch.object(server, "call_claude", lambda prompt: prompts.append(prompt) or answer):
            top, ai, _ = server.fetch_news("cold wallet", "", [], "Cold Wallet", "Ledger", token="tok")
        removed.assert_called_once_with("tok", "Ledger", "Cold Wallet")
        self.assertIn("BitLocker", prompts[0])
        self.assertEqual(ai["feedback_examples"], 1)
        by_headline = {a["headline"]: a for a in top}
        self.assertFalse(by_headline["Story 0"]["low_relevance"])
        self.assertEqual(by_headline["Story 0"]["reason"], "Squarely about cold storage and names Coinbase.")
        self.assertEqual(by_headline["Story 1"]["cutoff_reason"], "not mainly about this category (topic 1/4)")
        self.assertEqual(by_headline["Story 2"]["cutoff_reason"], "below the 7/10 cutoff (6/10)")


if __name__ == "__main__":
    unittest.main()
