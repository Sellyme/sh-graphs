#!/usr/bin/env python3
"""
SH Graphs - local server.

Run:  python3 server.py      (on Windows: py server.py)
Or in Docker:  docker compose up -d --build

It serves index.html from this folder, relays statistics requests to the
Steam Hunters API (browsers block the page from calling it directly), and
saves your user list, presets and selection to sh-graphs-data.json next to
this file. Uses only the Python standard library. Press Ctrl+C to stop.
"""
import json
import math
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime
import urllib.error
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings come from the environment so the same file runs on a desktop or in Docker.
STORE = os.environ.get("SH_DATA_FILE") or os.path.join(HERE, "sh-graphs-data.json")
# EHP data is large and rarely changes, so it lives in its own file next to the main one.
EHP_STORE = os.environ.get("SH_EHP_FILE") or os.path.join(os.path.dirname(STORE), "sh-graphs-ehp.json")
API = os.environ.get("SH_API_BASE") or "https://steamhunters.com/api/"
PATH_OK = re.compile(r"^steam-users/\d{1,20}(/statistics|/licenses)?$")
HOST = os.environ.get("SH_HOST", "127.0.0.1")
if os.environ.get("SH_PORT"):
    PORTS = [int(os.environ["SH_PORT"])]
else:
    PORTS = range(8765, 8776)
# Host header names this server answers to. Blocks DNS-rebinding from other websites.
# "*" turns the check off.
# ---- EHP automatic scanning + history ------------------------------------------------------------
# A background thread rescans EHP for the leaderboard's top users. Everything is tunable from the
# environment; SH_SCAN_SCALE divides every interval (handy for testing: 3600 turns hours into seconds).
EHP_HISTORY = os.environ.get("SH_EHP_HISTORY") or os.path.join(os.path.dirname(STORE), "sh-graphs-ehp-history.jsonl")
AUTOSCAN = os.environ.get("SH_AUTOSCAN", "1").strip().lower() not in ("0", "false", "no", "off")
SCALE = float(os.environ.get("SH_SCAN_SCALE") or 1)
SCAN_GAP = 300 / SCALE                      # at least 5 minutes between automatic scans
BACKOFF = 1800 / SCALE                      # first delay after a failure, doubling each time
BACKOFF_MAX = 43200 / SCALE                 # ... up to 12 hours
IDLE_CHECK = 60 / SCALE                     # how often to look for work when nothing is due
# (highest rank, rescan interval in seconds): top 25 daily, 26-50 weekly, 51-100 monthly.
TIERS = [(25, 24 * 3600 / SCALE), (50, 7 * 86400 / SCALE), (100, 30 * 86400 / SCALE)]
ARCHIVE_MIN_MS = 20 * 3600 * 1000 / SCALE   # a history point is only added if the last one is older than this
EHP_LOCK = threading.Lock()                 # guards every read-modify-write of the EHP file and the history file
SCAN = {"enabled": AUTOSCAN, "running": None, "next_at": 0, "fails": 0, "last": None, "fail_until": {}}
HIST_LAST = {}                              # steam id -> timestamp (ms) of its newest history snapshot
HIST_SET = {}                               # steam id -> appids completed as of that snapshot


def _read_json(path, default):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except (FileNotFoundError, ValueError):
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    os.replace(tmp, path)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0


def parse_licenses(j, tags):
    """Same compact per-game records the page builds from the licenses API (see ehpParse in index.html)."""
    items = []
    if isinstance(j, list):
        for l in j:
            if isinstance(l, dict) and isinstance(l.get("app"), dict):
                items.append((l.get("appId", l["app"].get("appId")), l))
    elif isinstance(j, dict):
        items = list(j.items())
    else:
        raise ValueError("Unexpected reply from Steam Hunters.")
    games = []
    for k, l in items:
        a = l.get("app") if isinstance(l, dict) else None
        if not isinstance(a, dict):
            continue
        try:
            appid = int(k)
        except (TypeError, ValueError):
            appid = 0
        rd = str(a.get("releaseDate") or "")
        year = int(rd[:4]) if re.match(r"^\d{4}-", rd) else 0
        tl = []
        for t in a.get("tags") or []:
            if isinstance(t, dict) and "tagId" in t:
                tags[str(t["tagId"])] = t.get("name")
                tl.append(t["tagId"])
        p = _num(l.get("playtime"))
        try:                                        # when the last achievement unlocked, i.e. about when it was completed (seconds)
            unlock = int(datetime.fromisoformat(str(l.get("lastUnlockDate")).replace("Z", "+00:00")).timestamp())
        except ValueError:
            unlock = 0
        games.append({
            "a": appid, "n": a.get("name") or str(k),
            "p": p, "m": _num(a.get("medianCompletionTime")),
            "c": l["completionTime"] if isinstance(l.get("completionTime"), (int, float)) and not isinstance(l.get("completionTime"), bool) else p,
            "x": 1 if a.get("isRestricted") else 0,
            "u": unlock,
            "i": 1 if l.get("isPlaytimeInvalid") else 0,
            "y": str(a.get("typeString") or "").lower(), "r": year,
            "f": 1 if a.get("isFree") else 0, "v": 1 if a.get("isVrRequired") else 0,
            "g": tl,
        })
    return games


def merge_games(old, fresh):
    """Games that left the completed list (e.g. the game gained achievements) are kept, flagged o=1,
    so historical snapshots that included them can still be priced."""
    seen = {g["a"] for g in fresh}
    return fresh + [g if g.get("o") else dict(g, o=1) for g in (old or []) if g.get("a") not in seen]


def completed_set(games):
    return {g["a"] for g in games if not g.get("o")}


def hist_load():
    """History is one line per snapshot: the first holds a user's whole completed set ("add"), every later one only
    the appids added and removed since the previous snapshot. Replaying the lines rebuilds the latest set."""
    HIST_LAST.clear()
    HIST_SET.clear()
    try:
        with open(EHP_HISTORY, "rb") as f:
            for line in f:
                try:
                    pt = json.loads(line)
                    sid, cur = pt["id"], HIST_SET.setdefault(pt["id"], set())
                    cur.difference_update(pt.get("rem") or [])
                    cur.update(pt.get("add") or [])
                    HIST_LAST[sid] = max(HIST_LAST.get(sid, 0), pt["t"])
                except (ValueError, KeyError, TypeError):
                    continue
    except FileNotFoundError:
        pass


def archive_if_due(sid, games, t_ms):
    """Append a snapshot unless the user's previous one is newer than ARCHIVE_MIN_MS. Call with EHP_LOCK held."""
    last = HIST_LAST.get(sid)
    if last is not None and t_ms - last <= ARCHIVE_MIN_MS:
        return False
    now_set, prev = completed_set(games), HIST_SET.get(sid)
    pt = {"id": sid, "t": int(t_ms)}
    if prev is None:
        pt["add"] = sorted(now_set)
    else:
        pt["add"], pt["rem"] = sorted(now_set - prev), sorted(prev - now_set)
        if not pt["rem"]:
            del pt["rem"]
    with open(EHP_HISTORY, "ab") as f:
        f.write((json.dumps(pt, separators=(",", ":")) + "\n").encode("utf-8"))
    HIST_SET[sid] = now_set
    HIST_LAST[sid] = int(t_ms)
    return True


def hist_seed():
    """Users saved before history existed get one starting snapshot, dated when their data was downloaded."""
    with EHP_LOCK:
        doc = _read_json(EHP_STORE, {})
        for sid, u in (doc.get("users") or {}).items():
            if isinstance(u, dict) and u.get("games") and sid not in HIST_LAST:
                archive_if_due(sid, u["games"], u.get("updated") or int(time.time() * 1000))


def merge_ehp(body, cur):
    """Combine the page's copy of the EHP data with the file. Automatic scans write the file while the
    page is open, so for each user the newer download wins. Users missing from the page's copy were
    deleted there. Overrides come from the page, tag names are merged."""
    cur_users = (cur.get("users") if isinstance(cur, dict) else None) or {}
    out = {"version": 1, "tags": dict(cur.get("tags") or {}) if isinstance(cur, dict) else {}, "overrides": body.get("overrides") or {}, "users": {}}
    out["tags"].update(body.get("tags") or {})
    scans = []
    for sid, u in (body.get("users") or {}).items():
        if not isinstance(u, dict) or not isinstance(u.get("games"), list):
            continue
        c = cur_users.get(sid)
        c_upd = (c.get("updated") or 0) if isinstance(c, dict) else 0
        if isinstance(c, dict) and isinstance(c.get("games"), list) and c_upd > (u.get("updated") or 0):
            u = dict(u)
            u["games"], u["updated"] = c["games"], c_upd
        elif (u.get("updated") or 0) > c_upd:
            scans.append((sid, u))
        out["users"][sid] = u
    return out, scans


def ranked_users(doc):
    rows = []
    for sid, u in (doc.get("users") or {}).items():
        if isinstance(u, dict) and u.get("games"):
            rows.append((sum(_num(g.get("m")) for g in u["games"] if not g.get("o")) / 60, sid, (u.get("updated") or 0) / 1000))
    rows.sort(key=lambda r: -r[0])
    return rows


def pick_due(doc, now):
    """Most overdue user (age / interval) among ranks 1-100. Users whose saved games have no completion dates yet
    (downloaded before dates were stored) go first, once, so the history can be backfilled."""
    best = None
    users = doc.get("users") or {}
    for rank, (_, sid, upd) in enumerate(ranked_users(doc), 1):
        interval = next((iv for lim, iv in TIERS if rank <= lim), None)
        if interval is None or now < SCAN["fail_until"].get(sid, 0):
            continue
        age = now - upd
        undated = not any("u" in g for g in (users[sid].get("games") or []))
        score = 1e9 + age if undated else age / interval
        if (undated or age > interval) and (best is None or score > best[0]):
            best = (score, sid)
    return best[1] if best else None


def scan_user(sid):
    req = urllib.request.Request(API + "steam-users/%s/licenses?state=Completed" % sid, headers={
        "User-Agent": "sh-graphs/1.0 (personal progress viewer)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        j = json.loads(r.read())
    tags = {}
    games = parse_licenses(j, tags)
    with EHP_LOCK:
        doc = _read_json(EHP_STORE, {})
        u = (doc.get("users") or {}).get(sid)
        if not isinstance(u, dict):
            return                                  # removed from the leaderboard while we were downloading
        now_ms = int(time.time() * 1000)
        u["games"], u["updated"] = merge_games(u.get("games"), games), now_ms
        doc.setdefault("tags", {}).update(tags)
        _write_json(EHP_STORE, doc)
        archive_if_due(sid, u["games"], now_ms)


def scanner_loop():
    SCAN["next_at"] = time.time() + 30 / SCALE      # let the server settle before the first scan
    while True:
        time.sleep(min(5, IDLE_CHECK))
        now = time.time()
        if now < SCAN["next_at"]:
            continue
        sid = pick_due(_read_json(EHP_STORE, {}), now)
        if not sid:
            SCAN["next_at"] = now + IDLE_CHECK
            continue
        SCAN["running"] = sid
        try:
            scan_user(sid)
            SCAN["fails"] = 0
            SCAN["last"] = {"id": sid, "t": time.time(), "ok": True}
            SCAN["next_at"] = time.time() + SCAN_GAP
        except Exception as e:                      # network, HTTP error, bad JSON: back off hard
            SCAN["fails"] += 1
            delay = max(SCAN_GAP, min(BACKOFF * 2 ** (SCAN["fails"] - 1), BACKOFF_MAX))
            SCAN["next_at"] = time.time() + delay
            SCAN["fail_until"][sid] = time.time() + min(delay * 4, 7 * 86400 / SCALE)
            SCAN["last"] = {"id": sid, "t": time.time(), "ok": False, "error": str(e)[:200]}
            sys.stderr.write("EHP auto-scan of %s failed (%s). Next attempt in %d s.\n" % (sid, e, delay))
        finally:
            SCAN["running"] = None


ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("SH_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def log_message(self, fmt, *args):
        if len(args) >= 2 and str(args[1])[:1] in ("4", "5"):
            sys.stderr.write("%s\n" % (fmt % args))

    def _host_ok(self):
        raw = (self.headers.get("Host") or "").strip().lower()
        if raw.startswith("["):                      # [::1]:8765
            host = raw[1:].split("]")[0]
        else:
            host = raw.split(":")[0]
        if "*" in ALLOWED_HOSTS or host in ALLOWED_HOSTS:
            return True
        self.send_error(403)
        return False

    def _json(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json(code, json.dumps({"error": msg}).encode())

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlsplit(self.path)

        if url.path.startswith("/sh/"):
            sub = url.path[4:]
            if not PATH_OK.match(sub):
                return self._err(400, "Only steam-users/<id>, /statistics and /licenses are relayed.")
            target = API + sub + ("?" + url.query if url.query else "")
            req = urllib.request.Request(target, headers={
                "User-Agent": "sh-graphs/1.0 (personal progress viewer)",
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=300 if sub.endswith("/licenses") else 30) as r:
                    return self._json(r.status, r.read())
            except urllib.error.HTTPError as e:
                return self._err(e.code, "Steam Hunters answered HTTP %d." % e.code)
            except Exception as e:  # network failure, timeout, DNS
                return self._err(502, "Could not reach Steam Hunters (%s)." % e)

        if url.path == "/ehp-history":
            pts = []
            try:
                with open(EHP_HISTORY, "rb") as f:
                    for line in f:
                        try:
                            pts.append(json.loads(line))
                        except ValueError:
                            continue
            except FileNotFoundError:
                pass
            return self._json(200, json.dumps({"points": pts}, separators=(",", ":")).encode("utf-8"))

        if url.path == "/ehp-status":
            st = {k: SCAN[k] for k in ("enabled", "running", "next_at", "fails", "last")}
            return self._json(200, json.dumps(st).encode("utf-8"))

        if url.path == "/healthz":
            return self._json(200, b'{"ok":true}')

        if url.path in ("/store", "/ehp-store"):
            try:
                with open(STORE if url.path == "/store" else EHP_STORE, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                data = b"{}"
            return self._json(200, data)

        if url.path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if url.path == "/favicon.ico":          # the only other static file that is served
            self.path = "/favicon.ico"
            return super().do_GET()
        self.send_error(404)

    def do_PUT(self):
        if not self._host_ok():
            return
        path = urlsplit(self.path).path
        if path not in ("/store", "/ehp-store"):
            return self.send_error(404)
        target = STORE if path == "/store" else EHP_STORE
        n = int(self.headers.get("Content-Length") or 0)
        if n > 50_000_000:
            return self._err(413, "Too large.")
        body = self.rfile.read(n)
        try:
            doc = json.loads(body)
        except ValueError:
            return self._err(400, "Not JSON.")
        if path == "/ehp-store":
            if not isinstance(doc, dict):
                return self._err(400, "Not an object.")
            with EHP_LOCK:
                merged, scans = merge_ehp(doc, _read_json(EHP_STORE, {}))
                _write_json(EHP_STORE, merged)
                for sid, u in scans:                # a download the page just made: keep a history point if one is due
                    archive_if_due(sid, u["games"], u.get("updated") or int(time.time() * 1000))
            return self._json(200, b'{"ok":true}')
        tmp = target + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, target)
        self._json(200, b'{"ok":true}')


def main():
    server = None
    for port in PORTS:
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit("No free port between %d and %d." % (PORTS.start, PORTS.stop - 1))
    url = "http://localhost:%d/" % server.server_address[1]
    print("SH Graphs running at %s  (Ctrl+C to stop)" % url, flush=True)
    print("Data file: %s" % STORE, flush=True)
    print("EHP file:  %s" % EHP_STORE, flush=True)
    hist_load()
    hist_seed()
    if AUTOSCAN:
        threading.Thread(target=scanner_loop, daemon=True).start()
        print("EHP auto-scan on: top 25 daily, 26-50 weekly, 51-100 monthly, >=%d s apart." % SCAN_GAP, flush=True)
    else:
        print("EHP auto-scan off (SH_AUTOSCAN=0).", flush=True)
    if "--no-browser" not in sys.argv and not os.environ.get("SH_NO_BROWSER"):
        webbrowser.open(url)
    # Stop cleanly on `docker stop` (SIGTERM) as well as Ctrl+C.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
