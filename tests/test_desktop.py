import tempfile
import unittest
from pathlib import Path

from attention_monitor import desktop
from tests.test_accounts import Client


class DesktopModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.server, self.url = desktop.start(self.data, port=8791)
        self.assertIsNotNone(self.server)
        self.c = Client(self.url.rstrip("/"))

    def tearDown(self):
        self.server.app.stop_all()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_no_login_needed(self):
        self.assertEqual(self.c.call("/")[0], 200)
        self.assertEqual(self.c.call("/login")[0], 302)            # -> /
        info = self.c.call("/api/auth/info")[1]
        self.assertTrue(info["local"])
        self.assertEqual(self.c.call("/api/status")[0], 200)

    def test_data_saved_in_chosen_folder(self):
        self.c.call("/api/timetable", {"subjects": [{"id": "a", "name": "A"}]}, method="PUT")
        self.assertTrue((self.data / "users" / desktop.LOCAL_USER / "timetable.json").exists())

    def test_accounts_disabled(self):
        self.assertEqual(self.c.call("/api/auth/register", {"username": "x", "password": "y" * 8})[0], 404)

    def test_requests_for_other_hosts_refused(self):
        # A website using DNS rebinding would send its own host name.
        code, _ = self.c.call("/api/status", headers={"Host": "evil.example:8791"})
        self.assertEqual(code, 403)
        code, _ = self.c.call("/api/settings", {"volume": 1}, headers={"Host": "evil.example:8791"})
        self.assertEqual(code, 403)
        self.assertEqual(self.c.call("/api/status", headers={"Host": "localhost:8791"})[0], 200)

    def test_second_launch_reuses_running_copy(self):
        server2, url2 = desktop.start(self.data, port=8791)
        self.assertIsNone(server2)
        self.assertEqual(url2, self.url)


if __name__ == "__main__":
    unittest.main()
