"""Live location sharing for the app: friends in one group see each other's position and route.

No accounts. A group is a long random id made by the app and sent as a link; whoever has the link can join.
Each phone also makes its own random member id, which works as its write key and is never shown to others.
Data is deleted 24 hours after a member last sent anything.

  POST /live/<room>/<member>      {"name": "...", "points": [[t_ms, lat, lon, acc, speed, bearing], ...], "ended": false}
  GET  /live/<room>/data?after=N  -> {"cursor": N2, "members": [{"id", "name", "ended", "updated", "points": [...]}]}
  GET  /live/<room>               -> redirect to the app on Google Play (someone tapped the link without the app)

Coolify may strip the "/live" path before passing the request on, so both forms are accepted.
Standard library only.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_PATH = os.environ.get("LIVE_DB", "/livedata/live.sqlite")
# The app's privacy policy (Google Play needs a public link): https://<host>/live/privacy
_PRIVACY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "privacy.html")
PRIVACY = open(_PRIVACY_FILE, encoding="utf-8").read() if os.path.exists(_PRIVACY_FILE) else "Privacy policy unavailable."
PLAY_URL = os.environ.get("PLAY_URL", "https://play.google.com/store/apps/details?id=com.aljarra.app")
ASSETLINKS = os.environ.get("ASSETLINKS", "")  # Android App Links file, so links open the app directly
KEEP_MS = 24 * 3600 * 1000
MAX_BODY = 512 * 1024
MAX_POINTS_PER_POST = 2000
MAX_POINTS_PER_MEMBER = 20000
MAX_MEMBERS = 20
MAX_POINTS_PER_REPLY = 5000
ID = re.compile(r"^[A-Za-z0-9_-]{22,64}$")


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.last_cleanup = 0
        with self.lock:
            self.db.executescript(
                """
                pragma journal_mode=wal;
                create table if not exists members(
                    room text, member text, name text, ended integer, updated integer,
                    primary key(room, member));
                create table if not exists points(
                    seq integer primary key autoincrement, room text, member text,
                    t integer, lat real, lon real, acc real, spd real, brg real);
                create index if not exists points_room on points(room, seq);
                create index if not exists points_member on points(room, member, seq);
                """
            )

    def post(self, room: str, member: str, name: str, points: list, ended: bool, now: int) -> str | None:
        """Stores one update. Returns an error text, or None when stored."""
        with self.lock:
            self._cleanup(now)
            known = self.db.execute("select 1 from members where room=? and member=?", (room, member)).fetchone()
            if not known:
                count = self.db.execute("select count(*) from members where room=?", (room,)).fetchone()[0]
                if count >= MAX_MEMBERS:
                    return "group is full"
            self.db.execute(
                "insert into members values(?,?,?,?,?) on conflict(room, member) "
                "do update set name=excluded.name, ended=excluded.ended, updated=excluded.updated",
                (room, member, name, int(ended), now),
            )
            self.db.executemany(
                "insert into points(room, member, t, lat, lon, acc, spd, brg) values(?,?,?,?,?,?,?,?)",
                [(room, member, *p) for p in points],
            )
            # Keep only the newest points of a member, so one phone cannot fill the disk.
            self.db.execute(
                "delete from points where room=? and member=? and seq <= ("
                "select seq from points where room=? and member=? order by seq desc limit 1 offset ?)",
                (room, member, room, member, MAX_POINTS_PER_MEMBER),
            )
            self.db.commit()
            return None

    def read(self, room: str, after: int, now: int) -> dict:
        with self.lock:
            self._cleanup(now)
            rows = self.db.execute(
                "select seq, member, t, lat, lon, acc, spd, brg from points where room=? and seq>? order by seq limit ?",
                (room, after, MAX_POINTS_PER_REPLY),
            ).fetchall()
            members = self.db.execute("select member, name, ended, updated from members where room=?", (room,)).fetchall()
        by_member = {m[0]: {"id": public_id(m[0]), "name": m[1], "ended": bool(m[2]), "updated": m[3], "points": []} for m in members}
        for seq, member, *point in rows:
            if member in by_member:
                by_member[member]["points"].append(point)
        cursor = rows[-1][0] if rows else after
        return {"cursor": cursor, "members": list(by_member.values())}

    def _cleanup(self, now: int):
        if now - self.last_cleanup < 10 * 60 * 1000:
            return
        self.last_cleanup = now
        old = now - KEEP_MS
        self.db.execute(
            "delete from points where exists (select 1 from members m where m.room=points.room "
            "and m.member=points.member and m.updated < ?)",
            (old,),
        )
        self.db.execute("delete from members where updated < ?", (old,))
        self.db.commit()


def public_id(member: str) -> str:
    """Others see this short id, never the member id itself (which lets a phone write)."""
    return hashlib.sha256(member.encode()).hexdigest()[:12]


def clean_points(raw, now: int) -> list | None:
    """Checks the points a phone sent. Returns None when anything is off."""
    if not isinstance(raw, list) or len(raw) > MAX_POINTS_PER_POST:
        return None
    out = []
    for p in raw:
        if not isinstance(p, list) or len(p) != 6:
            return None
        t, lat, lon, acc, spd, brg = p
        if not isinstance(t, int) or not (now - 48 * 3600 * 1000 <= t <= now + 10 * 60 * 1000):
            return None
        if not all(isinstance(v, (int, float)) for v in (lat, lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        extras = []
        for v in (acc, spd, brg):
            if v is not None and not isinstance(v, (int, float)):
                return None
            extras.append(v)
        out.append([t, float(lat), float(lon), *extras])
    return out


STORE: Store | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "live"

    def _parts(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if parts and parts[0] == "live":
            parts = parts[1:]
        return parts, parse_qs(url.query)

    def _json(self, code: int, body):
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parts, query = self._parts()
        # Coolify strips the route path, so /.well-known/assetlinks.json may arrive here as "/".
        if parts in ([], [".well-known", "assetlinks.json"]) and ASSETLINKS:
            data = ASSETLINKS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parts == ["health"]:
            return self._json(200, {"ok": True})
        if parts == ["privacy"]:
            data = PRIVACY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if len(parts) == 2 and parts[1] == "data" and ID.match(parts[0]):
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                return self._json(400, {"error": "bad after"})
            return self._json(200, STORE.read(parts[0], after, now_ms()))
        if len(parts) == 1 and ID.match(parts[0]):
            # Someone tapped the link without the app installed: send them to Google Play.
            self.send_response(302)
            self.send_header("Location", PLAY_URL)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        parts, _ = self._parts()
        if len(parts) != 2 or not ID.match(parts[0]) or not ID.match(parts[1]):
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "bad json"})
        now = now_ms()
        name = body.get("name") if isinstance(body, dict) else None
        points = clean_points(body.get("points", []), now) if isinstance(body, dict) else None
        if not isinstance(name, str) or not name.strip() or len(name) > 40 or points is None:
            return self._json(400, {"error": "bad update"})
        error = STORE.post(parts[0], parts[1], name.strip(), points, bool(body.get("ended")), now)
        if error:
            return self._json(403, {"error": error})
        self._json(200, {"ok": True})

    def log_message(self, *_):
        pass  # No access log: it would keep group links and addresses on disk.


def main():
    global STORE
    STORE = Store(DB_PATH)
    port = int(os.environ.get("PORT", "8090"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
