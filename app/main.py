"""A tiny service that honours the Azure Container Apps container contract, so the pipeline has something real to ship.

The contract, as I understand Microsoft's documentation: listen on 0.0.0.0 at the port the ingress targets (this app
reads $PORT, default 8080, and containerapp.yaml targets the same port); answer the health probes quickly; treat
SIGTERM as "stop soon" (the platform follows it with SIGKILL after the termination grace period): stop taking new
requests, finish in-flight ones, exit; log JSON to stdout (it ends up in Log Analytics); do not run as root.

FAIL_EVERY=N makes every Nth request to / return 500, to play the part of a bad release in demos.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(severity: str, message: str, **fields) -> None:
    print(json.dumps({"severity": severity, "message": message, **fields}), flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "orders"
    counter = 0
    lock = threading.Lock()

    def _send(self, status: int, body: dict | str, content_type: str = "application/json") -> None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - the name http.server looks for
        if self.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if self.path == "/":
            fail_every = int(os.environ.get("FAIL_EVERY", "0"))
            with Handler.lock:
                Handler.counter += 1
                n = Handler.counter
            if fail_every and n % fail_every == 0:
                log("ERROR", "simulated failure", request=n)
                return self._send(500, {"error": "simulated failure"})
            return self._send(200, {"version": os.environ.get("APP_VERSION", "dev"), "revision": os.environ.get("CONTAINER_APP_REVISION", "local")})
        return self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # replace the default stderr access log with structured JSON
        log("INFO", "request", path=self.path, status=args[1] if len(args) > 1 else "")


def make_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - the ingress reaches the container on all interfaces
    server.daemon_threads = False  # in-flight requests are allowed to finish on shutdown
    return server


def main() -> int:
    port = int(os.environ.get("PORT", "8080"))
    server = make_server(port)

    def on_sigterm(signum, frame):  # noqa: ARG001
        log("INFO", "SIGTERM received: draining")
        # shutdown() must not be called from the thread running serve_forever(), which is where handlers run
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_sigterm)
    log("INFO", "listening", port=port)
    server.serve_forever()
    server.server_close()  # waits for in-flight request threads
    log("INFO", "stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
