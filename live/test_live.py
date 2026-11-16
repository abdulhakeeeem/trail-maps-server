"""Runs the live service on this PC and checks it.  python -m unittest test_live  (inside server/live)"""
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import live

ROOM = "R" * 22
ANNA = "a" * 22
BADR = "b" * 22


class LiveTest(unittest.TestCase):
    def setUp(self):
        live.STORE = live.Store(":memory:")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), live.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, (json.loads(raw) if raw and response.getheader("Content-Type") == "application/json" else None), response

    def send(self, member, name="Anna", points=None, ended=False, prefix="/live"):
        now = live.now_ms()
        points = points if points is not None else [[now, 24.7, 46.6, 5.0, 12.0, 90.0]]
        return self.call("POST", f"{prefix}/{ROOM}/{member}", {"name": name, "points": points, "ended": ended})

    def test_friends_see_each_other_and_only_new_points_after_the_cursor(self):
        self.assertEqual(200, self.send(ANNA)[0])
        self.assertEqual(200, self.send(BADR, name="Badr", prefix="")[0])  # Coolify may strip "/live"
        status, body, _ = self.call("GET", f"/live/{ROOM}/data?after=0")
        self.assertEqual(200, status)
        self.assertEqual({"Anna", "Badr"}, {m["name"] for m in body["members"]})
        self.assertTrue(all(len(m["points"]) == 1 for m in body["members"]))
        # The member id is the write key: others only ever see a short public id.
        self.assertNotIn(ANNA, json.dumps(body))
        self.send(ANNA)
        _, later, _ = self.call("GET", f"/{ROOM}/data?after={body['cursor']}")
        anna = next(m for m in later["members"] if m["name"] == "Anna")
        self.assertEqual(1, len(anna["points"]))

    def test_offline_backlog_arriving_late_is_still_delivered(self):
        self.send(ANNA)
        _, first, _ = self.call("GET", f"/live/{ROOM}/data?after=0")
        old = live.now_ms() - 40 * 60 * 1000  # recorded 40 minutes ago with no signal
        self.send(BADR, name="Badr", points=[[old, 24.0, 46.0, None, None, None]])
        _, later, _ = self.call("GET", f"/live/{ROOM}/data?after={first['cursor']}")
        badr = next(m for m in later["members"] if m["name"] == "Badr")
        self.assertEqual(old, badr["points"][0][0])

    def test_bad_updates_are_refused(self):
        now = live.now_ms()
        self.assertEqual(400, self.send(ANNA, name="")[0])
        self.assertEqual(400, self.send(ANNA, points=[[now, 95.0, 46.0, None, None, None]])[0])
        self.assertEqual(400, self.send(ANNA, points=[[now, 24.0, 46.0]])[0])
        self.assertEqual(400, self.send(ANNA, points=[[now - 3 * 24 * 3600 * 1000, 24.0, 46.0, None, None, None]])[0])
        self.assertEqual(404, self.call("POST", f"/live/short/{ANNA}", {"name": "x", "points": []})[0])

    def test_a_group_holds_at_most_twenty_phones(self):
        for i in range(live.MAX_MEMBERS):
            self.assertEqual(200, self.send(f"{i:022d}")[0])
        self.assertEqual(403, self.send("z" * 22)[0])
        self.assertEqual(200, self.send(f"{0:022d}")[0])  # an existing member can still send

    def test_link_without_the_app_goes_to_google_play(self):
        status, _, response = self.call("GET", f"/live/{ROOM}")
        self.assertEqual(302, status)
        self.assertEqual(live.PLAY_URL, response.getheader("Location"))

    def test_data_is_deleted_a_day_after_the_last_update(self):
        store = live.Store(":memory:")
        now = live.now_ms()
        store.post(ROOM, ANNA, "Anna", [[now, 24.0, 46.0, None, None, None]], True, now)
        later = now + live.KEEP_MS + 11 * 60 * 1000
        self.assertEqual([], store.read(ROOM, 0, later)["members"])
        self.assertEqual(0, store.db.execute("select count(*) from points").fetchone()[0])

    def test_one_phone_keeps_only_its_newest_points(self):
        store = live.Store(":memory:")
        now = live.now_ms()
        old_cap = live.MAX_POINTS_PER_MEMBER
        live.MAX_POINTS_PER_MEMBER = 3
        try:
            for i in range(5):
                store.post(ROOM, ANNA, "Anna", [[now + i, 24.0, 46.0, None, None, None]], False, now)
        finally:
            live.MAX_POINTS_PER_MEMBER = old_cap
        times = [r[0] for r in store.db.execute("select t from points order by seq")]
        self.assertEqual([now + 2, now + 3, now + 4], times)


if __name__ == "__main__":
    unittest.main()
