from __future__ import annotations

import os
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run() -> None:
    port = int(os.environ.get("CULLUMI_DOM_PORT", "4173"))
    token = os.environ.get("CULLUMI_DOM_TOKEN", "cullumi-dom-test")
    with tempfile.TemporaryDirectory(prefix="Cullumi-dom-") as temporary:
        import cullumi.config as config_module

        config_module.app_data_dir = lambda: Path(temporary)

        from cullumi import http_api

        http_api.TOKEN = token
        server = ThreadingHTTPServer(("127.0.0.1", port), http_api.Handler)
        print(f"DOM test server ready on {port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    run()
