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
import os
import re
import signal
import sys
import threading
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
API = "https://steamhunters.com/api/"
PATH_OK = re.compile(r"^steam-users/\d{1,20}(/statistics|/licenses)?$")
HOST = os.environ.get("SH_HOST", "127.0.0.1")
if os.environ.get("SH_PORT"):
    PORTS = [int(os.environ["SH_PORT"])]
else:
    PORTS = range(8765, 8776)
# Host header names this server answers to. Blocks DNS-rebinding from other websites.
# "*" turns the check off.
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
            json.loads(body)
        except ValueError:
            return self._err(400, "Not JSON.")
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
