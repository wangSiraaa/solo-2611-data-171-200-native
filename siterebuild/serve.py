"""Local static HTTP server for an exported site.  Binds 127.0.0.1 only;
performs no remote fetching."""

from __future__ import annotations

import functools
import http.server


def serve(site_dir: str, port: int = 8000) -> None:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(site_dir)
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    actual = httpd.server_address[1]
    print(f"Serving {site_dir} at http://127.0.0.1:{actual}/ (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
