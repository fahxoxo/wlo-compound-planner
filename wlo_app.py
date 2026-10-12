#!/usr/bin/env python3
"""
WLO Compound Planner – local web app.

    python wlo_app.py                 # starts the server and opens Chrome
    python wlo_app.py --port 9000 --no-browser
    python wlo_app.py --cloud         # hosting (e.g. Render): listens on 0.0.0.0:$PORT

Only the Python standard library is needed (the data in data/ must already be
scraped with scrape_wlohub.py). Stop the server with Ctrl+C or by closing its window.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import wlo_calc

ROOT = Path(__file__).parent
WEB = ROOT / "web"
ENGINE = None
APP_ID = "wlo-compound-planner"
CLOUD = False  # set by --cloud / WLO_CLOUD / Render: public server, local-only features off


def code_version():
    """Changes whenever the app code changes, so the page can detect an outdated copy."""
    files = [WEB / "index.html", ROOT / "wlo_calc.py", ROOT / "wlo_data.py", Path(__file__),
             ROOT / "knowledge" / "shortcuts.json"]
    newest = max(f.stat().st_mtime for f in files if f.exists())
    return datetime.fromtimestamp(newest).strftime("%Y%m%d-%H%M%S")


VERSION = code_version()
NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0"}


def item_summary(r):
    return {"id": r["id"], "name": r["name"], "rank": r["rank"], "type": r["type"],
            "subtype": r["subtype"], "bases": r["bases"], "icon": r["icon_url"]}


def guide_args(q):
    def num(key, default):
        try:
            return int(q.get(key, [default])[0])
        except ValueError:
            return default
    have = [int(x) for x in q.get("have", [""])[0].split(",") if x.strip().isdigit()]
    return dict(tid=num("id", 0), max_shop_rank=num("max_shop_rank", 14),
                buy_stages=max(1, num("buy_stages", 2)), have=have,
                show_all=q.get("all", ["0"])[0] == "1")


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype, extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in {**NO_CACHE, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                page = (WEB / "index.html").read_text(encoding="utf-8").replace("__APP_VERSION__", VERSION)
                return self.send(200, page, "text/html; charset=utf-8")
            if u.path == "/api/info":
                meta_path = ROOT / "data" / "meta.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
                return self.json({"app": APP_ID, "version": VERSION, "meta": meta,
                                  "items": len(ENGINE.db["items"]) if ENGINE else 0,
                                  "formulas": len(ENGINE.db["compounds"]) if ENGINE else 0})
            if u.path == "/api/search":
                hits = ENGINE.search(q.get("q", [""])[0], 40)
                return self.json([item_summary(r) for r in hits])
            if u.path in ("/api/guide", "/api/guide.md"):
                a = guide_args(q)
                if a["tid"] not in ENGINE.db["items"]:
                    return self.json({"error": "Unknown item"}, 404)
                # the web page loads the slow "unlock" part separately (/api/unlock)
                g = ENGINE.guide(a["tid"], a["max_shop_rank"], a["buy_stages"],
                                 a["have"], a["show_all"], unlock=u.path == "/api/guide.md")
                if u.path == "/api/guide":
                    return self.json(g)
                name = "".join(c if c.isalnum() else "_" for c in g["target"]["name"]).strip("_")
                return self.send(200, wlo_calc.render_markdown(g), "text/markdown; charset=utf-8",
                                 {"Content-Disposition":
                                  f"attachment; filename*=UTF-8''{quote(name)}_{a['tid']}.md"})
            if u.path == "/api/unlock":
                a = guide_args(q)
                if a["tid"] not in ENGINE.db["items"]:
                    return self.json({"error": "Unknown item"}, 404)
                return self.json(ENGINE.unlock(a["tid"], a["max_shop_rank"], a["buy_stages"], a["have"]))
            self.json({"error": "Not found"}, 404)
        except Exception as e:  # report instead of dropping the connection
            self.json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_HEAD(self):  # health checks
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        # lets a newer launch of the app replace an older one still running (local only)
        if (not CLOUD and urlparse(self.path).path == "/api/shutdown"
                and self.client_address[0] == "127.0.0.1"):
            self.json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self.json({"error": "Not found"}, 404)

    def log_message(self, fmt, *args):
        pass


def stop_old_copies(first_port, count=20):
    """Shut down older copies of this app still running on our ports."""
    stopped = False
    for port in range(first_port, first_port + count):
        base = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(base + "/api/info", timeout=0.5) as r:
                info = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        if info.get("app") != APP_ID and "meta" not in info:
            continue  # some other program
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/api/shutdown", data=b"", method="POST"),
                                   timeout=2).read()
            print(f"Stopped an older copy of the app on port {port}.")
            stopped = True
        except Exception:
            print(f"An older copy is running on port {port}; close its window if the page looks outdated.")
    if stopped:
        time.sleep(1.0)


def find_chrome():
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium"):
        path = shutil.which(name)
        if path:
            return path
    return None


def open_browser(url):
    chrome = find_chrome()
    if chrome:
        subprocess.Popen([chrome, url])
        print(f"Opened in Chrome: {url}")
    else:
        webbrowser.open(url)
        print(f"Chrome not found; opened your default browser: {url}")


def serve_cloud():
    """Public hosting: bind 0.0.0.0 on the platform's $PORT, small solver cache."""
    global ENGINE
    port = int(os.environ.get("PORT", "10000"))
    print(f"Loading data ... (app version {VERSION})", flush=True)
    ENGINE = wlo_calc.Engine(max_solvers=6, max_guides=150)  # stay well inside a 512 MB free plan
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"WLO Compound Planner listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


def main():
    global ENGINE, CLOUD
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--cloud", action="store_true", help="run as a public server (hosting)")
    args = ap.parse_args()

    if not (ROOT / "data" / "items.json").exists():
        sys.exit("No data found. Run: python scrape_wlohub.py")
    CLOUD = args.cloud or bool(os.environ.get("WLO_CLOUD") or os.environ.get("RENDER"))
    if CLOUD:
        return serve_cloud()
    stop_old_copies(args.port)
    print(f"Loading data ... (app version {VERSION})")
    ENGINE = wlo_calc.Engine()

    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not server:
        sys.exit("No free port found.")
    url = f"http://127.0.0.1:{server.server_port}/?v={VERSION}"
    print(f"WLO Compound Planner is running at {url}")
    print("Keep this window open while you use the app. Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, open_browser, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
