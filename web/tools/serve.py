"""Serve web/public for local testing, telling the browser never to cache.

    python web/tools/serve.py [port]

Plain ``python -m http.server`` lets the browser keep old copies of the
JavaScript modules, so a reload can run yesterday's code.
"""

import functools
import http.server
import sys
from pathlib import Path

PUBLIC = Path(__file__).resolve().parents[1] / "public"


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    handler = functools.partial(NoCacheHandler, directory=str(PUBLIC))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Serving {PUBLIC} on http://localhost:{port}/")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
