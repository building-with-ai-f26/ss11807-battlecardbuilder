"""Offline tests for learning from uploaded past newsletters in app/server.py.

Run from the repo root:  python3 -m unittest discover tests
Claude is replaced by a fake, so these cost nothing.
"""

import base64
import io
import json
import os
import sys
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import server  # noqa: E402

PARAS = ["Ledger · Weekly News Digest", "Cold Wallet",
         "Coinbase says 99% of assets are in cold storage &amp; offline", "Source: Reuters — reuters.com",
         "Trezor recalls Safe 5 devices after reseller tampering, according to a report in The Block."]


def make_docx(paragraphs=PARAS):
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def claude_reply(payload):
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(payload)}],
            "usage": {"input_tokens": 3000, "output_tokens": 400}}


class FileTests(unittest.TestCase):
    def test_docx_text_one_paragraph_per_line(self):
        text = server.docx_text(make_docx())
        self.assertEqual(text.splitlines()[0], "Ledger · Weekly News Digest")
        self.assertIn("cold storage & offline", text)  # XML escapes undone

    def test_pdf_goes_to_claude_as_a_document(self):
        block = server.newsletter_content(b"%PDF-1.4 fake")
        self.assertEqual(block["type"], "document")
        self.assertEqual(block["source"]["media_type"], "application/pdf")

    def test_docx_goes_as_text(self):
        block = server.newsletter_content(make_docx())
        self.assertEqual(block["type"], "text")
        self.assertIn("Trezor recalls", block["text"])

    def test_old_doc_files_get_save_as_docx_advice(self):
        with self.assertRaisesRegex(ValueError, r"Save As → Word Document \(\.docx\)"):
            server.newsletter_content(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)

    def test_other_files_and_empty_and_huge_are_refused(self):
        for data, message in ((b"", "empty"), (b"hello world", "PDF or a Word .docx"),
                              (b"%PDF" + b"0" * server.MAX_NEWSLETTER_BYTES, "larger than 10 MB"),
                              (b"PK\x03\x04 not really a zip", ".docx")):
            with self.assertRaisesRegex(ValueError, message):
                server.newsletter_content(data)

    def test_zip_bomb_guard(self):
        with mock.patch.object(server, "MAX_DOCX_TEXT_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "too large"):
                server.docx_text(make_docx())


class LearnTests(unittest.TestCase):
    def test_profile_is_merged_and_outlets_cleaned(self):
        answer = claude_reply({"profile": "Topics they cover:\n- cold storage", "outlets": [
            "https://www.Reuters.com/markets", "theblock.co", "reuters.com", "not a domain", "wsj.com"]})
        with mock.patch.object(server, "call_claude", return_value=answer) as claude:
            result = server.learn_from_newsletter(make_docx(), "Ledger", "Topics they cover:\n- custody")
        content = claude.call_args[0][0]
        self.assertIn("Profile so far:\nTopics they cover:\n- custody", content[1]["text"])
        self.assertIn("Client: Ledger", content[1]["text"])
        self.assertEqual(result["outlets"], ["reuters.com", "theblock.co", "wsj.com"])
        self.assertEqual(result["profile"], "Topics they cover:\n- cold storage")
        self.assertGreater(result["cost_usd"], 0)

    def test_instructions_guard_against_text_in_the_file(self):
        self.assertIn("not instructions", server.NEWSLETTER_INSTRUCTIONS)


class ScoringUsesProfileTests(unittest.TestCase):
    def test_profile_reaches_the_scoring_prompt(self):
        prompt = server.build_claude_prompt("Ledger", "Cold Wallet", "cold wallet", [],
                                            profile="Outlets they use most: Reuters, The Block")
        self.assertIn("What this client's past newsletters show:\nOutlets they use most: Reuters, The Block", prompt)
        self.assertIn("past newsletters show", server.CLAUDE_INSTRUCTIONS)

    def test_no_profile_line_when_blank(self):
        self.assertNotIn("past newsletters", server.build_claude_prompt("Ledger", "X", "x", []))


class RouteTests(unittest.TestCase):
    """POST /api/newsletter through the real request handler (sign-in and Claude faked)."""
    FAKE_ENV = {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
                "ANTHROPIC_API_KEY": "test"}

    def call(self, body, token="good"):
        handler = server.Handler.__new__(server.Handler)
        raw = json.dumps(body).encode()
        handler.path = "/api/newsletter"
        handler.headers = {"Authorization": f"Bearer {token}", "Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        sent = {}
        handler.send_json = lambda status, payload: sent.update(status=status, payload=payload)
        handler.send_response = lambda status: sent.update(status=status)
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler.do_POST()
        return sent.get("status"), sent.get("payload")

    def setUp(self):
        server.rate_log.clear()

    def test_signed_in_upload_returns_profile(self):
        with mock.patch.dict(os.environ, self.FAKE_ENV), \
             mock.patch.object(server, "verify_user", lambda t: "user-a" if t == "good" else None), \
             mock.patch.object(server, "call_claude", return_value=claude_reply({"profile": "P", "outlets": []})):
            status, data = self.call({"client": "Ledger", "profile": "", "file": base64.b64encode(make_docx()).decode()})
        self.assertEqual(status, 200)
        self.assertEqual(data["profile"], "P")

    def test_needs_sign_in_and_is_rate_limited(self):
        with mock.patch.dict(os.environ, self.FAKE_ENV), \
             mock.patch.object(server, "verify_user", lambda t: "user-a" if t == "good" else None), \
             mock.patch.dict(server.RATE_LIMITS, {"newsletter": (1, 3600)}), \
             mock.patch.object(server, "learn_from_newsletter", return_value={"profile": "", "outlets": [], "cost_usd": 0}) as learn:
            self.assertEqual(self.call({"file": ""}, token="bad")[0], 401)
            learn.assert_not_called()
            self.assertEqual(self.call({"file": ""})[0], 200)
            self.assertEqual(self.call({"file": ""})[0], 429)

    def test_doc_file_message_reaches_the_page(self):
        doc = base64.b64encode(b"\xd0\xcf\x11\xe0" + b"\0" * 100).decode()
        with mock.patch.dict(os.environ, self.FAKE_ENV), mock.patch.object(server, "verify_user", lambda t: "user-a"):
            status, data = self.call({"client": "Ledger", "file": doc})
        self.assertEqual(status, 400)
        self.assertIn(".docx", data["error"])


if __name__ == "__main__":
    unittest.main()
