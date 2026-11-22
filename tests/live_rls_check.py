"""Live check that each Press Pass account sees only its own data.

Needs the real Supabase project: SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in .env,
supabase/schema.sql already run, and "Confirm email" turned off.
Not part of `python3 -m unittest discover tests` (it uses the internet and makes accounts).

Run from the repo root:
  python3 tests/live_rls_check.py                 # Supabase checks only
  python3 tests/live_rls_check.py --server        # also checks the local server (must be running)

It creates two throwaway accounts (presspass-check-...@mailinator.com), signs in as
each, and tries to read, change and delete the other account's rows. It deletes
the rows it made at the end; delete the two test users afterwards in the dashboard
(Authentication → Users). No Claude or GNews calls are made. Keys are never printed.
"""

import json
import os
import secrets
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import server  # noqa: E402  (only for load_env)

server.load_env()
URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
LOCAL = "http://localhost:8000"
results = []


def call(method, url, body=None, token=None, extra_headers=None):
    headers = {"apikey": KEY, "Content-Type": "application/json", "Prefer": "return=representation"}
    if token:
        headers["Authorization"] = "Bearer " + token
    headers.update(extra_headers or {})
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            text = response.read().decode()
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as error:
        text = error.read().decode()
        try:
            return error.code, json.loads(text)
        except ValueError:
            return error.code, text


def check(name, passed, detail=""):
    results.append(passed)
    print(("PASS  " if passed else "FAIL  ") + name + (f"  ({detail})" if detail and not passed else ""))


def new_account(label):
    email = f"presspass-check-{label}-{secrets.token_hex(4)}@mailinator.com"
    status, data = call("POST", URL + "/auth/v1/signup", {"email": email, "password": secrets.token_urlsafe(16)})
    token = (data or {}).get("access_token") if isinstance(data, dict) else None
    if not token:
        sys.exit(f"Couldn't create test account {label} (HTTP {status}). Is 'Confirm email' turned off? {data}")
    return token, data["user"]["id"]


def rest(table, query=""):
    return f"{URL}/rest/v1/{table}{query}"


def main():
    if not URL or not KEY:
        sys.exit("Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in .env first.")
    token_a, user_a = new_account("a")
    token_b, user_b = new_account("b")
    print(f"Made two test accounts (user ids ending ...{user_a[-6:]} and ...{user_b[-6:]}).\n")

    made = []  # (table, id, token) to clean up
    rows = {}
    for label, token in (("a", token_a), ("b", token_b)):
        status, data = call("POST", rest("client_profiles"), {
            "client_name": f"Client {label.upper()}", "companies": ["X"], "priority_sources": ["reuters.com"],
            "categories": [{"name": "FX", "terms": "", "exclude": ""}]}, token)
        check(f"account {label.upper()} can save a client setup", status == 201, f"HTTP {status} {data}")
        profile = data[0]
        status, data = call("POST", rest("newsletters"), {
            "client_name": f"Client {label.upper()}", "categories": ["FX"], "articles": [],
            "draft_html": "<p>draft</p>", "ai_cost_usd": 0.001}, token)
        check(f"account {label.upper()} can save a newsletter", status == 201, f"HTTP {status} {data}")
        letter = data[0]
        rows[label] = {"client_profiles": profile, "newsletters": letter}
        made += [("client_profiles", profile["id"], token), ("newsletters", letter["id"], token)]

    check("database filled in user_id from the sign-in (A)", rows["a"]["newsletters"]["user_id"] == user_a)
    check("database filled in user_id from the sign-in (B)", rows["b"]["newsletters"]["user_id"] == user_b)

    for table in ("client_profiles", "newsletters"):
        a_id, b_id = rows["a"][table]["id"], rows["b"][table]["id"]

        status, data = call("GET", rest(table, "?select=id,user_id"), token=token_a)
        ids = {r["id"] for r in data} if status == 200 else set()
        check(f"{table}: A's list has A's row and not B's", a_id in ids and b_id not in ids, f"HTTP {status}")
        check(f"{table}: every row A can see belongs to A", status == 200 and all(r["user_id"] == user_a for r in data))

        status, data = call("GET", rest(table, f"?id=eq.{b_id}&select=id"), token=token_a)
        check(f"{table}: A asking for B's row by id gets nothing", status == 200 and data == [], f"HTTP {status} {data}")

        status, data = call("PATCH", rest(table, f"?id=eq.{b_id}"), {"client_name": "hacked"}, token_a)
        check(f"{table}: A can't change B's row", status == 200 and data == [], f"HTTP {status} {data}")

        status, data = call("PATCH", rest(table, f"?id=eq.{a_id}"), {"user_id": user_b}, token_a)
        check(f"{table}: A can't hand A's row over to B", status in (401, 403) or data == [], f"HTTP {status} {data}")

        body = {"client_name": "planted", "user_id": user_b}
        if table == "newsletters":
            body["draft_html"] = "<p>x</p>"
        status, data = call("POST", rest(table), body, token_a)
        check(f"{table}: A can't add a row owned by B", status in (401, 403), f"HTTP {status} {data}")
        if status == 201:
            made.append((table, data[0]["id"], token_b))

        status, data = call("DELETE", rest(table, f"?id=eq.{b_id}"), token=token_a)
        check(f"{table}: A can't delete B's row", status == 200 and data == [], f"HTTP {status} {data}")

        status, data = call("GET", rest(table, f"?id=eq.{b_id}&select=client_name"), token=token_b)
        check(f"{table}: B's row is still there, unchanged", status == 200 and data and data[0]["client_name"] == "Client B",
              f"HTTP {status} {data}")

        status, data = call("GET", rest(table, "?select=id"))  # publishable key only, nobody signed in
        check(f"{table}: a signed-out visitor sees no rows", status in (401, 403) or data == [], f"HTTP {status} {data}")

        status, data = call("PATCH", rest(table, f"?id=eq.{a_id}"), {"client_name": "Client A edited"}, token_a)
        check(f"{table}: A can edit A's own row", status == 200 and len(data) == 1, f"HTTP {status} {data}")

    if "--server" in sys.argv:
        status, _ = call("POST", LOCAL + "/api/feedback", {"headline": "live check"})
        check("local server refuses feedback without a sign-in", status == 401, f"HTTP {status}")
        status, _ = call("POST", LOCAL + "/api/feedback", {"headline": "live check"}, token="not-a-token")
        check("local server refuses a fake token", status == 401, f"HTTP {status}")
        status, _ = call("POST", LOCAL + "/api/feedback", {"headline": "live check (safe to delete)"}, token=token_a)
        check("local server accepts a real sign-in", status == 200, f"HTTP {status}")

    for table, row_id, token in made:
        call("DELETE", rest(table, f"?id=eq.{row_id}"), token=token)
    status, data = call("GET", rest("newsletters", "?select=id"), token=token_a)
    check("A can delete A's own rows (cleanup)", status == 200 and data == [], f"HTTP {status} {data}")

    print(f"\n{sum(results)}/{len(results)} checks passed.")
    print("Delete the two presspass-check-... users in Supabase → Authentication → Users.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
