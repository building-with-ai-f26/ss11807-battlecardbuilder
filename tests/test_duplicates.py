"""Offline tests for removing duplicate stories (Phase 4) in app/server.py.

Run from the repo root:  python3 -m unittest discover tests
Claude is replaced by a fake answer, so these cost nothing.
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


def art(headline, source, domain, priority=False, description="", hours_old=1):
    return {"headline": headline, "source": source, "domain": domain, "url": f"https://{domain}/{abs(hash(headline))}",
            "description": description, "priority": priority, "date": NOW - timedelta(hours=hours_old)}


def claude_answer(links, score=8):
    """A fake Claude reply: every article gets the same rubric scores; links maps id -> same_story_as."""
    items = [{"id": i, "topic_fit": 4, "client_impact": 2, "newsworthiness": 1, "source_format": 1,
              "excluded_format": False, "same_story_as": links.get(i, i), "summary": f"Summary {i}."}
             for i in range(max(links) + 1)]
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"articles": items})}],
            "usage": {"input_tokens": 1000, "output_tokens": 300}}


class StoryGroupTests(unittest.TestCase):
    def test_coinbase_example_becomes_one_story(self):
        articles = [
            art("Coinbase Keeps Over 99% of Customer Assets in Cold Storage", "CoinDesk", "coindesk.com",
                description="Coinbase disclosed that over 99% of customer assets are held offline."),
            art("Ledger launches a new wallet", "The Block", "theblock.co"),
            art("Coinbase CEO Brian Armstrong says over 99% of customer crypto sits offline", "Bloomberg",
                "bloomberg.com", priority=True, description="Short."),
        ]
        for a, link in zip(articles, (0, 1, 0)):
            a["_same_story_as"] = link
        kept, removed = server.collapse_duplicates(articles)
        self.assertEqual(removed, 1)
        self.assertEqual(len(kept), 2)
        coinbase = [a for a in kept if "Coinbase" in a["headline"]][0]
        self.assertEqual(coinbase["source"], "Bloomberg")  # priority outlet wins, even with less text
        self.assertEqual(coinbase["also_covered"], [{"source": "CoinDesk", "url": articles[0]["url"], "domain": "coindesk.com"}])
        self.assertNotIn("also_covered", kept[1] if kept[1] is not coinbase else kept[0])
        self.assertFalse(any("_same_story_as" in a for a in articles))  # internal field cleaned up

    def test_without_a_priority_outlet_the_most_complete_then_earliest_wins(self):
        a = art("Quantus unveils offline smartphone wallet", "Decrypt", "decrypt.co", description="Short.", hours_old=5)
        b = art("Quantus's offline phone wallet keeps keys away from the internet", "CoinDesk", "coindesk.com",
                description="Quantus said its phone has no radios and signs transactions with QR codes.", hours_old=2)
        c = art("Quantus offline wallet phone", "Cointelegraph", "cointelegraph.com",
                description="Quantus said its phone has no radios and signs transactions with QR codes.", hours_old=9)
        for x, link in zip((a, b, c), (0, 0, 0)):
            x["_same_story_as"] = link
        kept, removed = server.collapse_duplicates([a, b, c])
        self.assertEqual((len(kept), removed), (1, 2))
        self.assertEqual(kept[0]["source"], "Cointelegraph")  # ties on length -> earliest published
        self.assertEqual([o["source"] for o in kept[0]["also_covered"]], ["Decrypt", "CoinDesk"])

    def test_chains_are_followed_and_bad_links_ignored(self):
        articles = [art(f"Story {i}", f"Outlet {i}", f"o{i}.com") for i in range(5)]
        for a, link in zip(articles, (0, 0, 1, 7, 4)):  # 2 -> 1 -> 0 is one story; 7 is out of range
            a["_same_story_as"] = link
        groups = server.story_groups(articles)
        self.assertEqual(sorted(len(g) for g in groups), [1, 1, 3])

    def test_forward_or_self_links_mean_no_duplicate(self):
        articles = [art(f"Story {i}", f"Outlet {i}", f"o{i}.com") for i in range(3)]
        for a, link in zip(articles, (2, 1, 2)):  # 0 -> 2 points forward: ignored
            a["_same_story_as"] = link
        self.assertEqual(len(server.story_groups(articles)), 3)

    def test_same_outlet_twice_is_not_listed_as_also_covered(self):
        a = art("Story", "CoinDesk", "coindesk.com", priority=True)
        b = art("Story again", "CoinDesk", "coindesk.com")
        a["_same_story_as"], b["_same_story_as"] = 0, 0
        kept, removed = server.collapse_duplicates([a, b])
        self.assertEqual(removed, 1)
        self.assertNotIn("also_covered", kept[0])


class FetchNewsMergesTests(unittest.TestCase):
    def test_duplicates_are_merged_before_ranking(self):
        cands = [art(f"Story {i}", f"Outlet {i}", f"o{i}.com") for i in range(6)]
        links = {0: 0, 1: 0, 2: 2, 3: 3, 4: 3, 5: 5}  # two pairs of duplicates
        with mock.patch.object(server, "gather_candidates", lambda *a, **k: cands), \
             mock.patch.object(server, "call_claude", lambda prompt: claude_answer(links)), \
             mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}):
            top, ai, _ = server.fetch_news("story", "", [])
        self.assertEqual(ai["duplicates_merged"], 2)
        self.assertEqual(len(top), 4)
        headlines = [a["headline"] for a in top]
        self.assertEqual(len(set(headlines)), 4)
        self.assertTrue(any(a.get("also_covered") for a in top))
        json.dumps(top)  # still plain data for the page

    def test_claude_is_asked_to_mark_same_stories(self):
        schema_item = server.SCORE_SCHEMA["properties"]["articles"]["items"]
        self.assertIn("same_story_as", schema_item["required"])
        self.assertIn("same_story_as", server.CLAUDE_INSTRUCTIONS)
        self.assertIn("Coinbase", server.CLAUDE_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
