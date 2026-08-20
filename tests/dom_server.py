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
        import cullumi.core as core

        core.app_data_dir = lambda: Path(temporary)

        import app

        app.TOKEN = token
        server = ThreadingHTTPServer(("127.0.0.1", port), app.Handler)
        print(f"DOM test server ready on {port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    run()
