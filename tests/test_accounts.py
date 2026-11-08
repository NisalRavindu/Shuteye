import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from attention_monitor import accounts as acc, web
from attention_monitor.accounts import Accounts, AuthError


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = 1_000_000.0
        self.a = Accounts(self.dir, invite_code="letmein", clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_user_is_owner_without_invite_and_gets_legacy_data(self):
        (self.dir / "timetable.json").write_text('{"subjects": [1]}')
        (self.dir / "days").mkdir()
        (self.dir / "days" / "2026-11-02.json").write_text("{}")
        token = self.a.register("Nina", "correct horse")
        self.assertEqual(self.a.user_for(token), "nina")                 # lower-cased
        self.assertTrue(self.a.users["nina"]["owner"])
        udir = self.dir / "users" / "nina"
        self.assertTrue((udir / "timetable.json").exists())
        self.assertTrue((udir / "days" / "2026-11-02.json").exists())
        self.assertFalse((self.dir / "timetable.json").exists())         # moved, not copied

    def test_later_users_need_the_invite_code(self):
        self.a.register("owner", "password1")
        with self.assertRaises(AuthError):
            self.a.register("friend", "password2")
        with self.assertRaises(AuthError):
            self.a.register("friend", "password2", invite="wrong")
        tok = self.a.register("friend", "password2", invite="letmein")
        self.assertEqual(self.a.user_for(tok), "friend")
        self.assertFalse(self.a.users["friend"]["owner"])

    def test_signups_closed_without_invite_code(self):
        a = Accounts(self.dir / "x")
        a.register("owner", "password1")
        self.assertFalse(a.info()["registration_open"])
        with self.assertRaises(AuthError):
            a.register("friend", "password2", invite="")

    def test_validation(self):
        for bad in ("ab", "has space", "../etc", "x" * 33):
            with self.assertRaises(AuthError):
                self.a.register(bad, "password1")
        with self.assertRaises(AuthError):
            self.a.register("okname", "short")

    def test_passwords_and_tokens_not_stored_in_plain_text(self):
        tok = self.a.register("nina", "correct horse")
        stored = (self.dir / "users.json").read_text() + (self.dir / "sessions.json").read_text()
        self.assertNotIn("correct horse", stored)
        self.assertNotIn(tok, stored)

    def test_login_logout_and_expiry(self):
        self.a.register("nina", "correct horse")
        with self.assertRaises(AuthError):
            self.a.login("nina", "wrong password")
        tok = self.a.login("NINA", "correct horse")
        self.assertEqual(self.a.user_for(tok), "nina")
        self.a.logout(tok)
        self.assertIsNone(self.a.user_for(tok))
        tok = self.a.login("nina", "correct horse")
        self.now += acc.SESSION_SECONDS + 1
        self.assertIsNone(self.a.user_for(tok))

    def test_sessions_survive_restart(self):
        tok = self.a.register("nina", "correct horse")
        again = Accounts(self.dir, clock=lambda: self.now)
        self.assertEqual(again.user_for(tok), "nina")

    def test_lockout_after_repeated_failures(self):
        self.a.register("nina", "correct horse")
        for _ in range(acc.MAX_FAILS):
            with self.assertRaises(AuthError):
                self.a.login("nina", "nope", client="1.2.3.4")
        with self.assertRaisesRegex(AuthError, "Too many"):
            self.a.login("nina", "correct horse", client="1.2.3.4")   # even the right one, for now
        self.now += acc.LOCKOUT_SECONDS + 1
        self.assertTrue(self.a.login("nina", "correct horse", client="1.2.3.4"))

    def test_change_password_signs_out_other_sessions(self):
        t1 = self.a.register("nina", "correct horse")
        self.a.change_password("nina", "correct horse", "battery staple")
        self.assertIsNone(self.a.user_for(t1))
        self.assertTrue(self.a.login("nina", "battery staple"))
        with self.assertRaises(AuthError):
            self.a.login("nina", "correct horse")


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()),
                                                  NoRedirect())

    def call(self, path, body=None, method=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method or ("POST" if body is not None else "GET"),
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self.opener.open(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class HttpAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = web.App(Path(self.tmp.name), invite_code="letmein")
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_everything_private_needs_login(self):
        c = Client(self.base)
        self.assertEqual(c.call("/")[0], 302)                      # -> /login
        for path in ("/api/status", "/api/timetable", "/api/settings", "/api/day", "/api/analysis/week"):
            self.assertEqual(c.call(path)[0], 401, path)
        self.assertEqual(c.call("/api/frames", {"samples": []})[0], 401)
        self.assertEqual(c.call("/api/timetable", {}, method="PUT")[0], 401)
        # public bits still work
        self.assertEqual(c.call("/login")[0], 200)
        self.assertEqual(c.call("/static/camera.js")[0], 200)
        self.assertEqual(c.call("/api/auth/info")[1]["first_user"], True)

    def test_users_cannot_see_each_others_data(self):
        nina, sam = Client(self.base), Client(self.base)
        self.assertEqual(nina.call("/api/auth/register", {"username": "nina", "password": "password1"})[0], 200)
        self.assertEqual(sam.call("/api/auth/register", {"username": "sam", "password": "password2"})[0], 400)  # no invite
        self.assertEqual(sam.call("/api/auth/register", {"username": "sam", "password": "password2",
                                                         "invite": "letmein"})[0], 200)
        nina.call("/api/timetable", {"subjects": [{"id": "a", "name": "Nina's maths"}]}, method="PUT")
        nina.call("/api/settings", {"volume": 10})
        self.assertEqual(nina.call("/api/timetable")[1]["subjects"][0]["name"], "Nina's maths")
        self.assertEqual(sam.call("/api/timetable")[1], {})
        self.assertNotIn("volume", sam.call("/api/settings")[1])
        # live sessions are separate too
        nina.call("/api/start", {})
        self.assertTrue(nina.call("/api/status")[1]["running"])
        self.assertFalse(sam.call("/api/status")[1]["running"])
        nina.call("/api/stop", {})

    def test_login_logout_flow(self):
        c = Client(self.base)
        c.call("/api/auth/register", {"username": "nina", "password": "password1"})
        self.assertEqual(c.call("/api/status")[0], 200)
        c.call("/api/auth/logout", {})
        self.assertEqual(c.call("/api/status")[0], 401)
        self.assertEqual(c.call("/api/auth/login", {"username": "nina", "password": "nope"})[0], 400)
        self.assertEqual(c.call("/api/auth/login", {"username": "nina", "password": "password1"})[0], 200)
        self.assertEqual(c.call("/")[0], 200)

    def test_cross_site_writes_refused(self):
        c = Client(self.base)
        c.call("/api/auth/register", {"username": "nina", "password": "password1"})
        code, _ = c.call("/api/settings", {"volume": 1}, headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        host = self.base.split("//")[1]
        self.assertEqual(c.call("/api/settings", {"volume": 1}, headers={"Origin": f"http://{host}"})[0], 200)


if __name__ == "__main__":
    unittest.main()
