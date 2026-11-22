"""Offline tests for running Press Pass on a host such as Render.

Run from the repo root:  python3 -m unittest discover tests
"""

import http.client
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import server  # noqa: E402


class ListenAddressTests(unittest.TestCase):
    def test_local_default_is_this_computer_only(self):
        self.assertEqual(server.listen_address({}), ("127.0.0.1", 8000))

    def test_host_port_opens_to_the_host(self):
        self.assertEqual(server.listen_address({"PORT": "10000"}), ("0.0.0.0", 10000))

    def test_host_can_be_overridden(self):
        self.assertEqual(server.listen_address({"PORT": "10000", "HOST": "127.0.0.1"}), ("127.0.0.1", 10000))


class PrivateFilesTests(unittest.TestCase):
    """Once the site is public, secrets and notes must never be downloadable."""

    @classmethod
    def setUpClass(cls):
        server.Handler.log_message = lambda *args: None  # keep test output quiet
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def status(self, raw_path):
        # http.client sends the path exactly as written (no "../" clean-up), like an attacker would.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", raw_path)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def test_env_file_cannot_be_downloaded(self):
        for path in ("/.env", "/../.env", "/%2e%2e/.env", "/..%2f.env", "/app/../.env"):
            status, body = self.status(path)
            self.assertNotEqual(status, 200, path)
            self.assertNotIn(b"API_KEY", body, path)

    def test_feedback_notes_cannot_be_downloaded(self):
        status, _ = self.status("/data/feedback.json")
        self.assertEqual(status, 404)

    def test_app_pages_are_served(self):
        for path in ("/", "/?v=123", "/index.html", "/signin.html", "/auth.js"):
            self.assertEqual(self.status(path)[0], 200, path)

    def test_nothing_else_in_app_is_served(self):
        # Found on the live site: /server.py was downloadable. Only the three public files are now.
        for path in ("/server.py", "/feeds.json", "/outlets.json", "/data/", "/data/feedback.json",
                     "/SERVER.PY", "/server.py?x=1", "//server.py", "/index.html/", "/nope.html"):
            status, body = self.status(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"def ", body, path)

    def test_security_headers(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(response.getheader("Content-Security-Policy"), "frame-ancestors 'none'")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")

    def test_pages_pin_supabase_js_with_an_integrity_hash(self):
        # The library comes from a CDN; the hash makes browsers refuse any changed copy.
        for page in ("index.html", "signin.html"):
            with open(os.path.join(server.APP_DIR, page), encoding="utf-8") as f:
                html = f.read()
            self.assertIn('supabase-js@2.119.0/dist/umd/supabase.js" integrity="sha384-', html, page)
            self.assertNotIn("supabase.min.js", html, page)  # jsDelivr generates .min.js on the fly: unsafe with a hash

    def test_head_requests_follow_the_same_rule(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("HEAD", "/server.py")
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()


if __name__ == "__main__":
    unittest.main()
