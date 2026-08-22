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
        from cullumi.config import ConfigStore
        from cullumi.project_store import ProjectManager
        from cullumi.scanner import Scanner
        from cullumi.similarity import SimilarityGroupCache

        config = ConfigStore(Path(temporary) / "config.json")
        manager = ProjectManager(config)
        groups = SimilarityGroupCache()
        scanner = Scanner(config, manager, groups)
        application = http_api.ApplicationContext(
            config, manager, scanner, groups, token, ROOT / "web"
        )
        server = ThreadingHTTPServer(("127.0.0.1", port), http_api.Handler)
        server.application = application
        print(f"DOM test server ready on {port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    run()
