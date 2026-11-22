"""Offline tests for sign-in on the server (app/server.py).

Run from the repo root:  python3 -m unittest discover tests
No internet, Supabase project or keys needed: Supabase's answers are faked.
"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import server  # noqa: E402

FAKE_ENV = {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test"}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class VerifyUserTests(unittest.TestCase):
    def setUp(self):
        server.auth_cache.clear()

    def test_valid_token_returns_user_id_and_sends_publishable_key(self):
        with mock.patch.dict(os.environ, FAKE_ENV), \
             mock.patch.object(server.urllib.request, "urlopen", return_value=FakeResponse(b'{"id": "user-a"}')) as urlopen:
            self.assertEqual(server.verify_user("token-a"), "user-a")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://example.supabase.co/auth/v1/user")
        self.assertEqual(request.get_header("Apikey"), "sb_publishable_test")
        self.assertEqual(request.get_header("Authorization"), "Bearer token-a")

    def test_rejected_token_returns_none(self):
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        with mock.patch.dict(os.environ, FAKE_ENV), \
             mock.patch.object(server.urllib.request, "urlopen", side_effect=error):
            self.assertIsNone(server.verify_user("bad-token"))

    def test_missing_token_or_settings_returns_none_without_calling_supabase(self):
        with mock.patch.object(server.urllib.request, "urlopen") as urlopen:
            with mock.patch.dict(os.environ, FAKE_ENV):
                self.assertIsNone(server.verify_user(""))
            with mock.patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_PUBLISHABLE_KEY": ""}):
                self.assertIsNone(server.verify_user("token-a"))
        urlopen.assert_not_called()

    def test_valid_token_is_cached_briefly(self):
        with mock.patch.dict(os.environ, FAKE_ENV), \
             mock.patch.object(server.urllib.request, "urlopen", return_value=FakeResponse(b'{"id": "user-a"}')) as urlopen:
            server.verify_user("token-a")
            server.verify_user("token-a")
        self.assertEqual(urlopen.call_count, 1)


class ApiRouteTests(unittest.TestCase):
    """Runs the real request handler on a random local port."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, path, method="GET", body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def fake_verify(self, token):
        return {"token-a": "user-a", "token-b": "user-b"}.get(token)

    def test_config_gives_url_and_publishable_key_only(self):
        with mock.patch.dict(os.environ, {**FAKE_ENV, "SUPABASE_SECRET_KEY": "sb_secret_never_sent"}):
            status, data = self.call("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"supabase_url": FAKE_ENV["SUPABASE_URL"],
                                "supabase_publishable_key": FAKE_ENV["SUPABASE_PUBLISHABLE_KEY"],
                                "auth_configured": True})
        self.assertNotIn("sb_secret", json.dumps(data))

    def test_api_routes_need_sign_in(self):
        with mock.patch.dict(os.environ, FAKE_ENV), mock.patch.object(server, "verify_user", self.fake_verify), \
             mock.patch.object(server, "fetch_news") as fetch_news, mock.patch.object(server, "summarize_pdf") as pdf:
            for path, method, body in [("/api/news?terms=fx", "GET", None),
                                       ("/api/feedback", "POST", b"{}"),
                                       ("/api/pdf", "POST", b"%PDF-1.4")]:
                for token in (None, "not-a-real-token"):
                    status, data = self.call(path, method, body, token)
                    self.assertEqual(status, 401, f"{method} {path} token={token}")
            fetch_news.assert_not_called()  # no Claude or GNews spending without a sign-in
            pdf.assert_not_called()

    def test_api_refuses_when_sign_in_not_set_up(self):
        with mock.patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_PUBLISHABLE_KEY": ""}):
            status, data = self.call("/api/news?terms=fx", token="token-a")
        self.assertEqual(status, 503)
        self.assertIn("SUPABASE_URL", data["error"])

    def test_signed_in_news_request_goes_through(self):
        with mock.patch.dict(os.environ, FAKE_ENV), mock.patch.object(server, "verify_user", self.fake_verify), \
             mock.patch.object(server, "fetch_news", return_value=([], {"used": False}, {})) as fetch_news:
            status, data = self.call("/api/news?terms=fx", token="token-a")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"articles": [], "ai": {"used": False}, "search": {}})
        fetch_news.assert_called_once()

    def test_feedback_is_stamped_with_the_token_owner_not_the_body(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "feedback.json")
            body = json.dumps({"headline": "x", "user_id": "user-b"}).encode()
            with mock.patch.dict(os.environ, FAKE_ENV), mock.patch.object(server, "verify_user", self.fake_verify), \
                 mock.patch.object(server, "FEEDBACK_FILE", path):
                status, _ = self.call("/api/feedback", "POST", body, token="token-a")
                with open(path, encoding="utf-8") as f:
                    saved = json.load(f)
        self.assertEqual(status, 200)
        self.assertEqual(saved[0]["user_id"], "user-a")


if __name__ == "__main__":
    unittest.main()
