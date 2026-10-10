"""Local dev server for web/ (stdlib only, not deployed).

    python web/dev-serve.py            # http://localhost:8080
    python web/dev-serve.py 9000 8000  # serve on 9000, proxy to the harness on 8000

Serves the static pages and forwards /api/* to the FastAPI dev harness
(cd agent; python -m uvicorn web.server:app --port 8000), so there is no CORS problem. config.js is served with
the harness URL swapped for "" (same origin) so the proxy is used.

No backend needed for these:
  http://localhost:8080/index.html?demo=1                    dashboard replaying fixtures/feed_replay.json
  http://localhost:8080/report.html?fixture=1                (also: decided, declined, expired, race)
  http://localhost:8080/workorder.html?fixture=1             (also: done, expired)
"""
import sys
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
HARNESS = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
ROOT = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _proxy(self):
        body = None
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)
        req = urllib.request.Request(f"http://localhost:{HARNESS}{self.path}", data=body, method=self.command,
                                     headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                data, code, ctype = r.read(), r.status, r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            data, code, ctype = e.read(), e.code, e.headers.get("Content-Type", "application/json")
        except OSError as e:
            data, code, ctype = f'{{"detail":"harness not running on port {HARNESS}: {e}"}}'.encode(), 502, "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith(("/api/", "/sms/")):
            return self._proxy()
        if self.path.split("?")[0] == "/config.js":
            data = (ROOT / "config.js").read_text(encoding="utf-8").replace(f"http://localhost:{HARNESS}", "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return super().do_GET()

    def do_POST(self):
        return self._proxy()


if __name__ == "__main__":
    print(f"web/ on http://localhost:{PORT}  (proxying /api to localhost:{HARNESS})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
